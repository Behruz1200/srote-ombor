"""ARCH-2 — kassa (POS) domeni.

Bu tizimning eng ko'p tegiladigan qismi: sotuv, qaytarish, almashtirish,
to'lov, parked chek, offline katalog. Avval bularning hammasi views.py ning
6000–7900-qatorlari orasida, boshqa domenlar bilan aralashib yotardi —
pos_checkout bilan pos_refund o'rtasida payment_qr_list va payments_webhook
turardi. Aynan shu aralashuv tufayli bir xil qulflash xatosi ikki xil
view'da alohida-alohida prodga chiqdi.

Endi kassa bilan bog'liq hamma narsa bitta faylda. Bu modul views.py ni
IMPORT QILMAYDI — faqat .access, .money va .models dan oladi, shuning uchun
aylanma import xavfi yo'q. views.py esa bu yerdan bir nechta yordamchini
qaytarib oladi (urls.py hali ham views.pos_checkout deb murojaat qiladi).
"""
import csv
import io
import json as _json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import qrcode
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import (
    Avg, Case, Count, DecimalField, Exists, ExpressionWrapper, F, FloatField,
    Max, Min, OuterRef, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .access import (
    POS_BRANCH_SESSION_KEY, _open_shift_for, _user_branch_or_403,
    admin_required, get_user_branch, normalize_code,
)
from .money import (
    DISCOUNT_REASONS, ROUNDING_MAX, ROUNDING_STEP, _is_rounding,
    _lock_stocks, _order_discount_share, _ret_group_value,
    _returns_adjustment, _valid_discount_reason,
)
from .models import (
    AuditLog, Branch, BranchStock, Category, Customer, EmployeeDebt,
    EmployeeDebtItem, ParkedSale, PaymentIntent, PaymentQR, PosDevice,
    Product, ProductVariant, Promotion, QuickSellItem, Return, Sale,
    SaleTransaction, Shift, User, _dec, _norm_pay_method, split_breakdown,
    weighted_cost,
)

logger = logging.getLogger(__name__)


@login_required
def pos_customer_display(request):
    """Mijoz tomonidagi ekran — POS bilan BroadcastChannel orqali sinxron.
    Faqat o'qish (interaktiv emas). Iste'mol uchun: 2-monitor yoki ikkinchi tab."""
    branch = _user_branch_or_403(request)
    return render(request, 'inventory/pos_display.html', {
        'branch': branch,
        'shop_name': 'yurit',  # TODO: shop name from settings/branding
    })


def _open_price_product():
    """Yashirin 'ochiq narx' mahsuloti (kodsiz kiyim/poyabzal sotuvi)."""
    product = Product.objects.filter(is_open_price=True).first()
    if product is None:
        product = Product.objects.create(
            name='Kiyim / Poyabzal', is_open_price=True,
            default_sale_price=0, markup_percent=0)
    return product


def _open_price_stock(branch):
    """Qo'lda summa uchun BranchStock. Ombor tekshirilmaydi.

    DIQQAT: tezkor sotuv toifalari ham shu mahsulotning turlari bo'lgani uchun
    bo'sh turni ANIQ tanlaymiz (.first() toifa turini qaytarib yuborishi mumkin).
    """
    product = _open_price_product()
    variant, _ = ProductVariant.objects.get_or_create(
        product=product, size='', color='')
    stock, _ = BranchStock.objects.get_or_create(
        variant=variant, branch=branch,
        defaults={'stock_count': 0, 'sale_price': 0, 'cost_price': 0})
    return stock


QUICK_SELL_CATEGORY = 'Kiyim Kechak'


def _quick_sell_product(item):
    """Toifaning ombor yuritiladigan mahsuloti (Kiyim Kechak kategoriyasida)."""
    if item.product_id:
        return item.product
    cat, _ = Category.objects.get_or_create(
        name=QUICK_SELL_CATEGORY, defaults={'prefix': 'KIY'})
    product = (Product.objects.filter(name__iexact=item.name, category=cat).first()
               or Product.objects.create(name=item.name, category=cat))
    item.product = product
    item.save(update_fields=['product'])
    return product


def _quick_sell_sync(item, branch=None):
    """Har NARX uchun alohida tur + zaxira yozuvi bo'lishini ta'minlaymiz.

    2500 so'mlik paypoq va 5000 so'mlik paypoq — boshqa-boshqa tovar, shuning
    uchun har narx alohida tur. Shunda qabul (miqdor/tannarx/marja) va ombor
    oddiy mahsulotdek ishlaydi.
    """
    product = _quick_sell_product(item)
    branches = ([branch] if branch is not None
                else list(Branch.objects.filter(is_active=True)))
    for price in item.price_list:
        variant, _ = ProductVariant.objects.get_or_create(
            product=product, size=str(price), color='')
        for b in branches:
            BranchStock.objects.get_or_create(
                variant=variant, branch=b,
                defaults={'stock_count': 0, 'sale_price': price,
                          'cost_price': 0, 'wholesale_price': 0})
    return product


def _quick_sell_items(branch):
    """Faol toifalar + har narx uchun tur, zaxira va stock id.

    Toifalar bazadan o'qiladi (Narxlar -> Tezkor sotuv sahifasida
    tahrirlanadi), narxlar esa endi haqiqiy tur bo'lgani uchun ombor
    hisobga olinadi.
    """
    out = []
    for item in (QuickSellItem.objects.filter(is_active=True)
                 .select_related('product')):
        product = _quick_sell_sync(item, branch)
        # Tugmalar HAQIQIY turlardan quriladi — sozlamadagi ro'yxatdan emas.
        # Shunda mahsulot sahifasida yangi narx (tur) qo'shilsa, u shu yerda
        # ham darrov ko'rinadi. _quick_sell_sync sozlamadagi narxlar uchun
        # turlarni yaratib qo'yadi, qolganini ombor hal qiladi.
        by_price = {}
        for st in (BranchStock.objects
                   .filter(variant__product=product, branch=branch)
                   .select_related('variant')):
            price = st.sale_price or 0
            if price <= 0:
                continue
            key = int(price)
            prev = by_price.get(key)
            # Bir narxda bir nechta tur bo'lsa — zaxirasi ko'pini olamiz
            if prev is None or st.stock_count > prev['stock']:
                by_price[key] = {'price': key, 'sid': st.id,
                                 'stock': st.stock_count}
        rows = sorted(by_price.values(), key=lambda r: r['price'])
        out.append({
            'key': f'qs{item.pk}',
            'name': item.name,
            'icon': item.icon or 'bi-bag',
            'code': product.code,
            'prices': rows,
        })
    return out


@login_required
def pos_terminal(request):
    """Single-page POS UI. Browser maintains the cart, posts via AJAX."""
    # If admin clicked the branch dropdown, persist the choice
    if request.user.is_admin():
        q_id = request.GET.get('branch_id')
        if q_id:
            try:
                b = Branch.objects.filter(pk=int(q_id), is_active=True).first()
                if b:
                    request.session[POS_BRANCH_SESSION_KEY] = b.pk
            except ValueError:
                pass

    branch = _user_branch_or_403(request)
    if branch is None:
        messages.error(request, "Filial biriktirilmagan. Administrator bilan bog'laning.")
        return redirect('lookup')

    open_shift = _open_shift_for(branch)
    if not open_shift:
        return redirect('shift_open')

    # POS pastida — FAQAT bugungi sotuvlar (kalendar sana bo'yicha, smenadan
    # qat'i nazar). Kassir bugun sotgan har qanday chekni qaytarish/almashtirish
    # qilishi mumkin.
    # Patch 1e: yangi qatorlar jonli kelib turgani uchun boshlang'ich ro'yxat
    # qisqa (12 ta) bo'lsa yetadi — 200 ta chek + har chekning barcha `lines`
    # prefetch'i (item_count hisoblash uchun) POS yuklanishini sekinlashtirardi.
    # To'liq tarix "Hammasi →" (sales_list) orqali bir bosishда ochiladi.
    recent_txns = (SaleTransaction.objects.filter(
                       branch=branch, sold_at__date=timezone.localdate())
                   .select_related('sold_by')
                   .prefetch_related('lines')
                   .order_by('-sold_at')[:12])

    # Top 12 most-sold products in this branch over the last 30 days.
    # Used to populate the "Tez sotiluvchilar" quick-tap grid on the POS.
    since = timezone.now() - timedelta(days=30)
    top_rows = (Sale.objects
                .filter(branch=branch, sold_at__gte=since)
                .values('variant__product')
                .annotate(sold_qty=Sum('quantity'))
                .order_by('-sold_qty')[:12])
    top_ids = [r['variant__product'] for r in top_rows]
    qty_by_id = {r['variant__product']: r['sold_qty'] for r in top_rows}
    products_by_id = {p.id: p for p in Product.objects.filter(id__in=top_ids)}
    favorites = []
    for pid in top_ids:
        p = products_by_id.get(pid)
        if not p:
            continue
        favorites.append({
            'code': p.code,
            'name': p.name,
            'price': float(p.default_sale_price),
            'image': p.image.url if p.image else '',
            'sold_qty': qty_by_id.get(pid, 0),
        })

    branches_list = []
    if request.user.is_admin():
        branches_list = list(Branch.objects.filter(is_active=True).order_by('name'))

    parked_sales = list(ParkedSale.objects
                        .filter(branch=branch)
                        .select_related('parked_by')
                        .order_by('-created_at')[:20])

    # Static QR codes for this branch — user uploads once per provider.
    # Mijoz QR'ni telefon ilovasidan scan qiladi, summa kiritadi, to'laydi.
    # Kassir manual ravishda chekni yakunlaydi.
    static_qrs = list(PaymentQR.objects
                      .filter(branch=branch, is_active=True)
                      .order_by('provider'))
    # Map provider -> QR for fast lookup in template
    qr_by_provider = {}
    for q in static_qrs:
        qr_by_provider.setdefault(q.provider, q)

    # Build the provider list shown on the POS. Combine: providers that
    # have a static QR uploaded (manual confirm flow) + the in-code
    # provider abstraction (currently stubbed, future merchant API).
    from .payments import available_providers, _REGISTRY
    payment_providers = []
    for p in available_providers():
        static = qr_by_provider.get(p.name)
        payment_providers.append({
            'name': p.name,
            'display_name': p.display_name,
            'icon': p.icon,
            'is_installment': p.is_installment,
            'has_static_qr': bool(static),
            'static_qr_id': static.id if static else None,
        })
    # Also include providers that have a static QR but aren't in the
    # in-code registry (e.g. "humo" or "other")
    in_registry = {p['name'] for p in payment_providers}
    for q in static_qrs:
        if q.provider in in_registry:
            continue
        payment_providers.append({
            'name': q.provider,
            'display_name': q.get_provider_display(),
            'icon': 'bi-qr-code',
            'is_installment': q.provider in ('anor', 'alif', 'iman', 'zoodpay'),
            'has_static_qr': True,
            'static_qr_id': q.id,
        })

    return render(request, 'inventory/pos.html', {
        'branch': branch,
        'open_price_sid': _open_price_stock(branch).id,
        'quick_sell': _quick_sell_items(branch),
        'shift': open_shift,
        'recent_txns': recent_txns,
        'favorites': favorites,
        'parked_sales': parked_sales,
        'payment_methods': SaleTransaction.PaymentMethod.choices,
        'payment_providers': payment_providers,
        'branches_list': branches_list,
        'discount_reasons': DISCOUNT_REASONS,      # DISC-7
    })


@login_required
def pos_device_sync(request):
    """POST /pos/device-sync/ — OFF-10: qurilmaning katalog holati.

    ATAYLAB juda arzon: bitta upsert, hech qanday hisob-kitob yo'q. Mijoz
    tomonda ham bo'sh vaqtda va "yuborib unutamiz" tarzda chaqiriladi —
    SOTUVGA hech qachon xalaqit bermasligi kerak.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False}, status=400)
    did = str(data.get('device_id') or '').strip()[:64]
    if not did:
        return JsonResponse({'ok': False, 'error': 'device_id kerak'}, status=400)
    try:
        cnt = int(data.get('catalog_count') or 0)
    except (TypeError, ValueError):
        cnt = 0
    at = None
    _at = data.get('catalog_at')
    if _at:
        try:
            from datetime import timezone as _dt_tz
            at = datetime.fromtimestamp(int(_at) / 1000.0, tz=_dt_tz.utc)
        except (TypeError, ValueError, OSError):
            at = None
    PosDevice.objects.update_or_create(
        device_id=did,
        defaults={
            'branch': getattr(request.user, 'branch', None),
            'last_user': request.user,
            'user_agent': (request.META.get('HTTP_USER_AGENT') or '')[:200],
            'catalog_count': max(0, cnt),
            'catalog_at': at,
            'last_seen': timezone.now(),
        })
    return JsonResponse({'ok': True})


@login_required
def pos_catalog(request):
    """GET /pos/catalog/ — OFF-8: butun katalog (offline skanerlash uchun).

    NEGA KERAK: service worker /pos/lookup/ javoblarini TO'LIQ URL bo'yicha
    keshlaydi, ya'ni offline faqat SHU qurilmada ILGARI skanerlangan tovar
    topilardi. Yangi tovar — "topilmadi". Kassir uni "bunday tovar yo'q" deb
    o'qiydi. Endi butun katalog oldindan yuklanadi va offline HAR QANDAY
    tovar topiladi.

    Javob /pos/lookup/ bilan BIR XIL maydon nomlarini ishlatadi — mijoz
    tomonda tarjima qilish shart emas, demak ikki shakl bir-biridan
    ajralib ketmaydi.

    `cost_price` ATAYLAB yuborilmaydi: POS uni ishlatmaydi, butun tannarx
    kitobini brauzerga tushirishning hojati yo'q.
    """
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)

    rows = (BranchStock.objects
            .filter(branch=branch)
            .select_related('variant__product')
            .order_by('variant__product__code', 'variant__size', 'variant__color'))

    by_code = {}
    for st in rows:
        prod = st.variant.product
        # Ochiq narxli yashirin mahsulot — u "Tezkor sotuv" panelidan sotiladi,
        # skanerlanmaydi (pos_lookup ham uni nom qidiruvidan chiqaradi).
        if prod.is_open_price:
            continue
        item = by_code.get(prod.code)
        if item is None:
            item = by_code[prod.code] = {
                'code': prod.code,
                'name': prod.name,
                'external_barcode': prod.external_barcode or '',
                'default_sale_price': float(prod.default_sale_price or 0),
                'variants': [],
            }
        item['variants'].append({
            'stock_id': st.id,
            'variant_id': st.variant_id,
            'size': st.variant.size,
            'color': st.variant.color,
            'barcode': st.variant.barcode or '',
            'stock_count': st.stock_count,
            'sale_price': float(st.sale_price or prod.default_sale_price or 0),
            'wholesale_price': float(st.wholesale_price or 0),
        })

    quick = [{'name': q.name, 'prices': q.price_list}
             for q in QuickSellItem.objects.filter(is_active=True)]

    return JsonResponse({
        'ok': True,
        'branch_id': branch.id,
        'generated_at': timezone.now().isoformat(),
        'count': len(by_code),
        'products': list(by_code.values()),
        'quick_sell': quick,
    })


@login_required
def pos_lookup(request):
    """GET /pos/lookup/?q=OYO-0001 [&branch=ID]
    Admins may pass &branch= to query a different branch (used by transfers).
    Returns JSON:
      { found: bool,
        product: {code, name, default_sale_price},
        variants: [{variant_id, stock_id, size, color, stock_count, sale_price, cost_price}],
        suggestions: [{code, name}] }
    """
    # Admins may override the branch context (e.g. transfer create form)
    branch_override = request.GET.get('branch')
    if branch_override and request.user.is_admin():
        try:
            branch = Branch.objects.get(pk=int(branch_override))
        except (Branch.DoesNotExist, ValueError):
            branch = _user_branch_or_403(request)
    else:
        branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'error': 'no branch'}, status=403)

    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({'found': False})

    # Exact code first — match either internal code or manufacturer barcode
    code = normalize_code(q.upper())
    product = Product.objects.filter(
        Q(code=code) | Q(external_barcode=q.strip())
    ).first()

    # Tur (variant) shtrix-kodi skanerlangan bo'lishi mumkin — aniq turga moslaymiz
    matched_variant_id = None
    _matched_variant = None
    if not product:
        _vm = (ProductVariant.objects.filter(barcode=q.strip())
               .select_related('product').first())
        if _vm:
            product = _vm.product
            matched_variant_id = _vm.id
            _matched_variant = _vm

    if not product:
        # "Paypoq", "Ich kiyim" kabi tezkor sotuv toifasi yozilgan bo'lishi
        # mumkin — u oddiy mahsulot emas, shuning uchun panelga yo'naltiramiz.
        # DIQQAT: icontains juda ochko'z — "Oq" rangi "Payp[oq]"ga tushib
        # ketardi. Shuning uchun aniq moslik yoki (3+ harfda) boshlanishi.
        qs_q = Q(name__iexact=q)
        if len(q) >= 3:
            qs_q |= Q(name__istartswith=q)
        qs_item = (QuickSellItem.objects.filter(is_active=True)
                   .filter(qs_q).first())
        if qs_item:
            variant, _ = ProductVariant.objects.get_or_create(
                product=_open_price_product(), size='', color=qs_item.name)
            stock, _ = BranchStock.objects.get_or_create(
                variant=variant, branch=branch,
                defaults={'stock_count': 0, 'sale_price': 0, 'cost_price': 0})
            return JsonResponse({
                'found': False,
                'quick_sell': {
                    'name': qs_item.name,
                    'prices': qs_item.price_list,
                    'sid': stock.id,
                },
            })

        # Nom bo'yicha qidiruv — tur (rang/o'lcham) matni ham hisobga olinadi,
        # masalan "Fresh" yoki "1.8 L". Ochiq narxli yashirin mahsulot chiqmaydi.
        matches = list(Product.objects.filter(
            Q(name__icontains=q) | Q(brand__icontains=q) |
            Q(category__name__icontains=q) |
            Q(variants__color__icontains=q) | Q(variants__size__icontains=q)
        ).exclude(is_open_price=True).distinct()[:8])
        if len(matches) == 1:
            product = matches[0]
        elif matches:
            return JsonResponse({
                'found': False,
                'suggestions': [{'code': p.code, 'name': p.name} for p in matches],
            })
        else:
            return JsonResponse({'found': False, 'suggestions': []})

    stocks = (BranchStock.objects.filter(variant__product=product, branch=branch)
              .select_related('variant')
              .order_by('variant__size', 'variant__color'))

    variants = [{
        'stock_id': s.id,
        'variant_id': s.variant_id,
        'size': s.variant.size,
        'color': s.variant.color,
        'barcode': s.variant.barcode or '',
        'stock_count': s.stock_count,
        'sale_price': float(s.sale_price or product.default_sale_price),
        'wholesale_price': float(s.wholesale_price or 0),
        'cost_price': float(s.cost_price),
    } for s in stocks]

    # Skanerlangan barcode turi BO'SH (0), lekin AYNAN SHU RANG+O'LCHAMDAGI
    # (ya'ni bir xil tovar, boshqa barcode bilan qayta qabul qilingan) birodарда
    # qoldiq bor bo'lsa — o'shanga yo'naltiramiz. Shunda skaner "omborda yo'q"
    # deб dead-end bo'lmaydi. Faqat rang BO'SH BO'LMAGANда (aniq bir xil kod).
    if matched_variant_id and _matched_variant is not None:
        mv_row = next((v for v in variants if v['variant_id'] == matched_variant_id), None)
        if (mv_row is None or mv_row['stock_count'] <= 0):
            mc = (_matched_variant.color or '').strip().lower()
            ms = (_matched_variant.size or '').strip().lower()
            if mc:  # faqat aniq (bo'sh bo'lmagan) rang bo'yicha — xavfsiz
                cands = [v for v in variants if v['stock_count'] > 0
                         and (v['color'] or '').strip().lower() == mc
                         and (v['size'] or '').strip().lower() == ms]
                if cands:
                    cands.sort(key=lambda v: -v['stock_count'])
                    matched_variant_id = cands[0]['variant_id']

    # If the product exists but has no variants in this branch, tell the
    # user explicitly — the silent "topilmadi" is misleading.
    other_branches_with_stock = []
    if not variants:
        other_ids = (BranchStock.objects
                     .filter(variant__product=product, stock_count__gt=0)
                     .exclude(branch=branch)
                     .values_list('branch_id', flat=True).distinct())
        for b in Branch.objects.filter(pk__in=other_ids):
            other_branches_with_stock.append({'name': b.name, 'id': b.id})

    return JsonResponse({
        'found': True,
        'product': {
            'code': product.code,
            'external_barcode': product.external_barcode or '',
            'name': product.name,
            'default_sale_price': float(product.default_sale_price),
        },
        'branch_name': branch.name,
        'variants': variants,
        'matched_variant_id': matched_variant_id,
        'other_branches': other_branches_with_stock,
    })


PRICE_OVERRIDE_PCT = Decimal('5')       # 5%


PRICE_OVERRIDE_MIN_ABS = Decimal('1000')  # 1000 so'm


@login_required
def pos_checkout(request):
    """POST /pos/checkout/ with JSON body:
      { lines: [{stock_id, qty, sale_price}],
        payment_method: 'cash'|'card'|'transfer'|'mixed',
        customer_name, customer_phone, note }
    Creates SaleTransaction + Sales atomically. Returns {ok, txn_id, receipt_url}.
    """
    # TG-1: SOTUV yo'lidan Telegram xabarlari OLIB TASHLANDI (do'kon egasi
    # so'rovi, 2026-08). Ilgari bu yerda uchta send_telegram bor edi: kech
    # offline sotuv, rad etilgan offline replay va tannarxdan past sotuv.
    #
    # Hech qanday yozuv YO'QOLMADI — uchalasi ham AuditLog'ga yoziladi va
    # audit sahifasida ko'rinadi. Telegram faqat DUBLIKAT bildirishnoma edi.
    #
    # Yon foyda: send_telegram har chat_id uchun 10 s time-out bilan tashqi
    # so'rov qiladi va ulardan biri transaction.atomic() ICHIDA edi — ya'ni
    # tarmoq sekinlashsa BranchStock qatorlari qulfda turib, boshqa kassirning
    # sotuvi ham kutib qolardi. Sotuv yo'lida tashqi chaqiruv qolmadi.

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)

    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    lines = data.get('lines') or []
    if not lines:
        return JsonResponse({'ok': False, 'error': 'savat boʻsh'}, status=400)

    payment_method = (data.get('payment_method') or 'cash').strip()
    # Mixed payment: client sends [{method, amount}]. Validate sum after we
    # know the total. Keep as list of dicts for storage.
    payment_breakdown = data.get('payment_breakdown') or []
    if not isinstance(payment_breakdown, list):
        payment_breakdown = []

    # PAY-2 / MON-11: QR provider nomlari ('payme','click'...) 'transfer'ga
    # moslashtiriladi; NOMA'LUM usul RAD etiladi (jimgina 'cash'ga aylantirilmaydi
    # — aks holda karta/QR pul kassaga tushган kabi ko'rinib, kamomad bo'lardi).
    _VALID_METHODS = {'cash', 'card', 'transfer'}
    _QR_PROVIDERS = {'payme', 'click', 'uzum', 'humo', 'anor', 'alif',
                     'iman', 'zoodpay', 'other', 'noop'}

    def _norm_method(m):
        m = (m or '').strip().lower()
        return 'transfer' if m in _QR_PROVIDERS else m

    if payment_method != 'mixed':
        _pm = _norm_method(payment_method)
        if _pm not in _VALID_METHODS:
            return JsonResponse({'ok': False,
                'error': f"Noma'lum to'lov turi: {payment_method}"}, status=400)
        payment_method = _pm

    customer_name = (data.get('customer_name') or '').strip()[:120]
    customer_phone = (data.get('customer_phone') or '').strip()[:40]
    note = (data.get('note') or '').strip()[:200]
    # OFF-2: offline replay bir sotuvni ikki marta yozmasin. Mijoz bir martalik
    # kalit yuboradi; o'sha kalit bilan chek allaqachon bo'lsa — yangi
    # yaratmaymiz, mavjudini qaytaramiz.
    idem_key = (data.get('idempotency_key') or '').strip()[:64] or None
    # Pul qiymatlari Decimal bo'lishi SHART: modeldagi maydonlar DecimalField,
    # float bilan aralashsa `Decimal + float` -> TypeError (chek yig'indisi
    # hisoblanganda 500 xato). Shuning uchun hammasini Decimal'ga o'giramiz.
    from decimal import Decimal, InvalidOperation

    # Pul maydonlari DB'da Decimal(12,2) — mutlaq qiymati 10^10 dan kichik
    # bo'lishi SHART, aks holda "numeric field overflow" (500). Xato terilgan
    # ulkan raqam (masalan numpad'дa qo'shimcha nol) 500 emas, tushunarli
    # ogohlantirish berishi kerak.
    MAX_MONEY = Decimal('9999999999.99')

    def _money(v, allow_zero=True):
        try:
            d = Decimal(str(v if v not in (None, '') else 0))
        except (InvalidOperation, ValueError, TypeError):
            d = Decimal('0')
        if d < 0:
            d = Decimal('0')
        return d

    order_discount = _money(data.get('order_discount'))
    if order_discount > MAX_MONEY:
        return JsonResponse({'ok': False,
            'error': "Chegirma juda katta. Iltimos, to'g'ri summa kiriting."}, status=400)
    discount_reason = (data.get('discount_reason') or '').strip()[:200]

    # Parse line shape without touching the DB (input validation only).
    # The stock check that *can* race is done inside the atomic block below
    # with select_for_update, so two concurrent kassirs cannot both pass
    # the qty check against the same row.
    parsed_lines = []
    for ln in lines:
        try:
            sid = int(ln['stock_id'])
            qty = int(ln['qty'])
            line_discount = _money(ln.get('line_discount'))
            # MON-13: narx QAT'IY tekshiriladi — "abc" kabi axlat jimgina 0 ga
            # aylanib, bepul tovarga aylanmasin. Noto'g'ri narx = xato.
            _raw_price = ln['sale_price']
            price = Decimal(str(_raw_price if _raw_price not in (None, '') else 0))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return JsonResponse({'ok': False, 'error': "noto'g'ri narx yoki qator"}, status=400)
        if qty <= 0 or price < 0:
            return JsonResponse({'ok': False, 'error': 'qty/narx noto\'g\'ri'}, status=400)
        if price > MAX_MONEY or line_discount > MAX_MONEY:
            return JsonResponse({'ok': False,
                'error': "Narx juda katta. Iltimos, to'g'ri narx kiriting."}, status=400)
        parsed_lines.append({'sid': sid, 'qty': qty, 'price': price, 'ld': line_discount})

    # STK-16 / R1: bir xil stock_id li qatorlarni BIRLASHTIRMAYMIZ (ochiq
    # narxli tovarlar bitta stock_id ostida, ammo HAR XIL qo'lда kiritilgan
    # narxда bo'ladi — birlashtirish oxirgi narxда IKKALASINI yozib, kam pul
    # olardi). Buning o'rniga QOLDIQ tekshiruvini stock_id bo'yicha JAMLAB
    # bajaramiz (quyida), narx esa har qatorда o'ziniki bo'lib qoladi.
    _qty_by_sid = {}
    for _l in parsed_lines:
        _qty_by_sid[_l['sid']] = _qty_by_sid.get(_l['sid'], 0) + _l['qty']

    # MON-18: chegirma savat summasidan OSHMASLIGI kerak. Ilgari chegirma faqat
    # MAX_MONEY ga tekshirilardi — 1 000 000 chegirma 1 000 so'mlik sotuvга
    # qo'yilsa, jami −999 000 bo'lib, cash_sales()/expected_cash() minusга
    # tushib, kassadan yashirin pul olish yo'li ochilardi. Endi cheklaymiz.
    _subtotal = Decimal('0')
    for _l in parsed_lines:
        _line_gross = Decimal(_l['qty']) * _l['price']
        if _l['ld'] > _line_gross:      # qator chegirmasi qatordan oshmasin
            _l['ld'] = _line_gross
        _subtotal += _line_gross - _l['ld']
    if order_discount > _subtotal:      # chek chegirmasi savatdan oshmasin
        order_discount = _subtotal

    # V5/S2: aksiya chegirmasini SERVERда qayta hisoblaymiz — mijoz yuborган
    # applied_promos ga ishonmaymiz (aks holда soxta aksiya nomi bilan butun
    # savatni 0 ga tushirish mumkin edi). Aksiyадан tashqari qo'lда chegirма
    # esa SABAB talab qiladi (auditда ko'rinsin).
    _promo_stocks = {s.id: s for s in BranchStock.objects
                     .filter(id__in=list(_qty_by_sid.keys()))
                     .select_related('variant__product')}
    _cart_lines = []
    for _l in parsed_lines:
        _s = _promo_stocks.get(_l['sid'])
        if not _s:
            continue
        _cart_lines.append({
            'stock_id': _l['sid'], 'qty': _l['qty'], 'price': float(_l['price']),
            'product_id': _s.variant.product_id,
            'category_id': _s.variant.product.category_id,
        })
    _server_applied, _server_promo_f = _evaluate_promotions(_cart_lines)
    _server_promo = Decimal(str(_server_promo_f))
    _claimed_promo = Decimal('0')
    for _p in (data.get('applied_promos') or []):
        try:
            _claimed_promo += Decimal(str(_p.get('discount') or 0))
        except (InvalidOperation, TypeError):
            pass
    if _claimed_promo > _server_promo + Decimal('1'):
        return JsonResponse({'ok': False,
            'error': "Aksiya chegirmasi tekshiruvdan o'tmadi — savatni yangilang."},
            status=400)
    _manual_disc = order_discount - _claimed_promo
    if _manual_disc < Decimal('-1'):
        return JsonResponse({'ok': False,
            'error': "Chegirma summasi mos emas — qayta urinib ko'ring."}, status=400)
    if _manual_disc < 0:
        _manual_disc = Decimal('0')
    if _manual_disc > Decimal('0.5'):
        if not discount_reason:
            return JsonResponse({'ok': False,
                'error': "Qo'lda chegirma uchun sabab kiriting."}, status=400)
        # DISC-7: sabab bor, lekin "s" yoki "3000" bo'lsa — audit uchun foydasiz.
        if not _valid_discount_reason(discount_reason):
            return JsonResponse({'ok': False,
                'error': "Sababni ro'yxatdan tanlang yoki qisqacha yozing "
                         "(kamida 3 harf)."}, status=400)
    # Klient sonига emas, SERVER hisoblаган aksiyaga + qo'lда qismga ishonamiz.
    order_discount = _server_promo + _manual_disc
    # DISC-1: aksiya ulushini ALOHIDA saqlaymiz. Ilgari ikkalasi bitta songa
    # qo'shilib ketardi va hisobotlarда kassir bergan chegirma bilan egasi
    # sozlagan aksiya farqlanmasdi. Kassir ixtiyoridagi qism =
    # order_discount − promo.
    # DISC-2 tuzatishi: bu yerda ilgari "sababsiz 289 chek — aslida aksiya"
    # deb yozilgan edi. NOTO'G'RI: Promotion jadvali bo'sh, sabab majburiyati
    # esa 2026-08-26 da qo'shilgan. O'sha cheklar QO'LDA berilган chegirma —
    # shunчaki sababi yozilmagan davrdan qolgan (0061 migratsiyasi tuzatdi).
    _promo_part = _server_promo
    if order_discount > _subtotal:
        order_discount = _subtotal
    if _promo_part > order_discount:      # savat cheklovi aksiyani ham qirqsa
        _promo_part = order_discount

    # Resolve / auto-create Customer by phone (most reliable key)
    customer = None
    if customer_phone:
        cleaned_phone = ''.join(c for c in customer_phone if c.isdigit() or c == '+')
        _digits = sum(c.isdigit() for c in cleaned_phone)
        if cleaned_phone:
            customer = Customer.objects.filter(phone=cleaned_phone).first()
            # SEC-8: juda qisqa/soxta raqamдан YANGI mijoz yaratmaymiz (baza
            # ifloslanishi va enumeratsiya oldini oladi). O'zbek raqami kamida
            # 9 raqam. Mavjud mijozni topsak bog'laymiz; aks holda mijozsiz sotuv.
            if not customer and _digits >= 9:
                customer = Customer.objects.create(
                    phone=cleaned_phone, name=customer_name,
                )
            elif customer and customer_name and not customer.name:
                customer.name = customer_name
                customer.save(update_fields=['name'])

    open_shift = _open_shift_for(branch)

    # OFF-7: sotuvning ASL vaqti (client_ts). Offline navbatдan kech kelsa ham
    # chek shu vaqtни ko'rsatadi va smen shu vaqt bo'yicha aniqlanadi — sinxron
    # vaqti bo'yicha emas (aks holda ertalabki savdo kechki smenга tushardi).
    target_sold_at = None
    target_shift = open_shift
    # R4: client_ts'ga FAQAT offline replayда ishonamiz. Oddiy onlayn sotuvда
    # pos.html client_ts yuborса ham, sotuv vaqti = SERVER vaqti bo'lsin (qurilma
    # soati adashса ertalabki savdo kechki smenга tushib ketmasin). Backdating
    # faqat haqiqiy navbat-replay uchun.
    _client_ts = (data.get('client_ts') or '').strip()
    if _client_ts and data.get('is_offline_replay'):
        from django.utils.dateparse import parse_datetime
        _dt = parse_datetime(_client_ts)
        if _dt is not None:
            if timezone.is_naive(_dt):
                _dt = timezone.make_aware(_dt, timezone.get_current_timezone())
            _now = timezone.now()
            # kelajak yoki >7 kun eski vaqt — qurilma soati xato, e'tiborsiz
            if _now - timedelta(days=7) <= _dt <= _now + timedelta(minutes=5):
                target_sold_at = _dt
                # O'sha payt ochiq bo'lgan smenni topamiz (hozir yopilgan bo'lsa
                # ham). ARCH-5 tufayli FK bo'yicha o'sha smen hisobotiga tushadi.
                _hist = (Shift.objects.filter(branch=branch, opened_at__lte=_dt)
                         .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=_dt))
                         .order_by('-opened_at').first())
                if _hist:
                    target_shift = _hist
                    # R3: agar bu smen ALLAQACHON yopilган bo'lса, uning
                    # closing_expected_cash'i (MON-22) QOTIRILган — kech kelган
                    # bu sotuv o'sha raqamга kirmaydi. Jimgina buzмaslik uchun
                    # egaга ko'rinadigan qilib belgilaymiz (audit + xabar).
                    if _hist.closed_at is not None:
                        try:
                            AuditLog.objects.create(
                                user=request.user,
                                username_snapshot=request.user.username,
                                action=AuditLog.Action.CREATE,
                                model_name='SaleTransaction',
                                object_id='',
                                object_repr=(f"Kech offline sotuv YOPILGAN smen #{_hist.id} "
                                             f"ga tushdi ({branch.name}, {_dt:%Y-%m-%d %H:%M}) "
                                             f"— yopilish naqd hisobiga kirmaydi")[:300],
                                changes={'shift_id': _hist.id, 'client_ts': _client_ts},
                            )
                        except Exception:
                            logger.exception('late-replay flag failed (shift %s)', _hist.id)

    # MON-9: smensiz sotuv YO'Q. Aks holda shift=None bilan yozilib, hech qaysi
    # Z-hisobotга tushmaydi — kassa "ortiq" chiqadi. (Offline replay o'zining
    # tarixiy smenига tushishi mumkin; oddiy sotuv hozirgi ochiq smenга.)
    if not target_shift:
        return JsonResponse({'ok': False,
            'error': "Smen ochilmagan. Sotuv faqat ochiq smen davomida amalga oshiriladi."},
            status=400)

    def _receipt_response(txn):
        return JsonResponse({
            'ok': True,
            'txn_id': txn.pk,
            'receipt_url': f'/transaction/{txn.public_id}/?autoprint=1',
            'total': float(txn.total),
            'item_count': txn.item_count,
            'sms': None,
            'duplicate': True,
        })

    # OFF-2: bu kalit bilan chek allaqachon yozilgan bo'lsa — takror EMAS,
    # o'sha chekni qaytaramiz (ombor ikki marta kamaymaydi, tushum ikki
    # barobar bo'lmaydi).
    if idem_key:
        dup = SaleTransaction.objects.filter(idempotency_key=idem_key).first()
        if dup:
            return _receipt_response(dup)

    # Sanitize breakdown: QR provider -> 'transfer'; NOMA'LUM usul RAD etiladi.
    clean_breakdown = []
    for entry in payment_breakdown:
        try:
            m = _norm_method(entry.get('method'))
            a = float(entry.get('amount') or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if not m or a <= 0:
            continue
        if m not in _VALID_METHODS:
            return JsonResponse({'ok': False,
                'error': f"Noma'lum to'lov turi (aralash): {entry.get('method')}"}, status=400)
        clean_breakdown.append({'method': m, 'amount': round(a, 2)})

    # If mixed but breakdown empty, fallback to single-method
    if payment_method == 'mixed' and not clean_breakdown:
        payment_method = 'cash'

    # MON-10: ARALASH to'lov summasi chekdan KAM bo'lmasin. Brauzer buni
    # tekshiradi, lekin devtools yoki offline-replay chetlab o'tishi mumkin —
    # shuning uchun serverда ham tekshiramiz.
    if payment_method == 'mixed':
        _order_total = Decimal('0')
        for _l in parsed_lines:
            _order_total += Decimal(_l['qty']) * _l['price'] - _l['ld']
        _order_total -= order_discount
        if _order_total < 0:
            _order_total = Decimal('0')
        _paid = sum((Decimal(str(e['amount'])) for e in clean_breakdown), Decimal('0'))
        # MON-16: IKKI TOMONLAMA tekshiruv. Ilgari faqat KAM to'lov rad etilardi;
        # ORTIQ to'lov ham xuddi shu nolinchi-farq ekspluatatsiyasига olib
        # kelardi (split_breakdown qoldiqni oxirgi usulga tashlab, kutilgan
        # naqdни shishiradi). Aralashда summa chekка AYNAN teng bo'lishi kerak
        # (ortiqcha naqd = qaytim, u alohida maydon — to'lov legi emas).
        if abs(_paid - _order_total) > Decimal('0.5'):
            _diff = _order_total - _paid
            _msg = (f"Aralash to'lov yetishmaydi: {int(_diff)} so'm kam."
                    if _diff > 0 else
                    f"Aralash to'lov ortiqcha: {int(-_diff)} so'm. Summa chekка teng bo'lsin.")
            return JsonResponse({'ok': False, 'error': _msg}, status=400)

    class _CheckoutAbort(Exception):
        def __init__(self, payload, status=400):
            self.payload = payload
            self.status = status

    try:
        with transaction.atomic():
            # Lock all referenced stocks first (sort by pk to avoid deadlocks
            # when two checkouts overlap on the same items in different order).
            sids = sorted({l['sid'] for l in parsed_lines})
            locked = {
                s.pk: s
                for s in BranchStock.objects
                    .select_for_update()
                    .select_related('variant__product', 'branch')
                    .filter(pk__in=sids, branch=branch)
            }
            for sid in sids:
                if sid not in locked:
                    raise _CheckoutAbort({'ok': False, 'error': f'stock {sid} topilmadi'})

            # Re-validate quantities against the FRESHLY locked stock_count.
            # No concurrent checkout can change it between this check and the
            # F() deduction below — they queue on the row lock instead.
            for _sid in sids:
                stock = locked[_sid]
                # R1/STK-16: shu stock_id bo'yicha JAMI soni (bir nechta qator
                # bir tovarga tegishli bo'lса) qoldiqдан oshmasin.
                _need = _qty_by_sid.get(_sid, 0)
                if (not stock.variant.product.is_open_price
                        and _need > stock.stock_count):
                    err = (f"{stock.variant.product.code} {stock.variant.size}/{stock.variant.color}: "
                           f"omborda faqat {stock.stock_count} ta bor, soʻrov {_need}")
                    # C2: if this is an offline-queue replay, alert admin via Telegram
                    # and log to AuditLog — kassir's offline sale was rejected at sync time.
                    if data.get('is_offline_replay'):
                        try:
                            # OFF-6: TO'LIQ payload — sotuv yo'qolib ketmasin.
                            # Barcha qatorlar, narxlar, to'lov turi, mijoz.
                            _payload = {
                                'lines': [{'sid': _l['sid'], 'qty': _l['qty'],
                                           'price': float(_l['price'])}
                                          for _l in parsed_lines],
                                'payment_method': payment_method,
                                'payment_breakdown': clean_breakdown,
                                'customer_phone': customer_phone,
                                'idempotency_key': idem_key,
                            }
                            AuditLog.objects.create(
                                user=request.user,
                                username_snapshot=request.user.username,
                                action=AuditLog.Action.UPDATE,
                                model_name='OfflineConflict',
                                object_repr=err[:200],
                                changes={'rejected_sale': _payload, 'reason': err[:200]},
                            )
                        except Exception:
                            logger.exception('offline-conflict alert failed')
                    raise _CheckoutAbort({'ok': False, 'error': err})

            # MON-5: smen checkout davomida yopilib qolmasin (poyga —
            # _open_shift_for atomic blokдан oldin chaqirilgan). Offline replay
            # tarixiy yopiq smenга ATAYLAB tushadi, uni tekshirmaymiz.
            if not data.get('is_offline_replay'):
                _st = (Shift.objects.filter(pk=target_shift.pk)
                       .values_list('status', flat=True).first())
                if _st != Shift.Status.OPEN:
                    raise _CheckoutAbort({'ok': False,
                        'error': "Smen yopildi. Sotuvni qayta boshlang."})

            txn = SaleTransaction.objects.create(
                branch=branch,
                sold_by=request.user,
                payment_method=payment_method,
                payment_breakdown=clean_breakdown,
                customer=customer,
                customer_name=customer_name,
                customer_phone=customer_phone,
                note=note,
                order_discount=order_discount,
                promo_discount=_promo_part,   # DISC-1
                discount_reason=discount_reason,
                shift=target_shift,                      # OFF-7 / ARCH-5
                sold_at=target_sold_at or timezone.now(),  # OFF-7
                idempotency_key=idem_key,
            )
            price_overrides = []  # MON-8: qo'lda kiritilgan narx auditи
            for ln in parsed_lines:
                stock = locked[ln['sid']]
                prod = stock.variant.product
                if not prod.is_open_price:
                    stock.stock_count = F('stock_count') - ln['qty']
                    stock.save(update_fields=['stock_count'])  # STK-7
                Sale.objects.create(
                    transaction=txn,
                    variant=stock.variant, branch=stock.branch,
                    quantity=ln['qty'],
                    sale_price=ln['price'],
                    cost_at_sale=stock.cost_price,
                    line_discount=ln['ld'],
                    sold_by=request.user,
                )
                # MON-8 (flag-and-audit): qo'lda kiritilgan narxni QABUL qilamiz
                # (kelishilgan/ulgurji narx — ish jarayoni buzilmaydi), LEKIN
                # katalog narxidan sezilarli farq qilsa yoki TANNARXDAN past
                # bo'lsa — audit uchun belgilaymiz (kim, qachon, qancha).
                if not prod.is_open_price:
                    catalog = stock.sale_price or Decimal('0')
                    entered = ln['price']
                    cost = stock.cost_price or Decimal('0')
                    below_cost = bool(cost > 0 and entered < cost)
                    material = False
                    pct = Decimal('0')
                    if catalog > 0:
                        pct = abs(entered - catalog) / catalog * 100
                        # rounding shovqinidan qochish: kamida 5% VA 1000 so'm farq
                        material = (pct >= PRICE_OVERRIDE_PCT
                                    and abs(entered - catalog) >= PRICE_OVERRIDE_MIN_ABS)
                    if material or below_cost:
                        price_overrides.append({
                            'code': prod.code, 'name': prod.name,
                            'variant': ('/'.join(p for p in
                                        (stock.variant.size, stock.variant.color) if p)
                                        or (stock.variant.barcode or '')),
                            'catalog': float(catalog), 'entered': float(entered),
                            'cost': float(cost), 'qty': ln['qty'],
                            'diff_pct': round(float(pct), 1),
                            'below_cost': below_cost,
                        })
    except _CheckoutAbort as e:
        return JsonResponse(e.payload, status=e.status)
    except IntegrityError:
        # OFF-2 poyga: bir vaqtning o'zida ikki replay bir xil kalit bilan
        # kelsa, ikkinchi create unique cheklovга urилadi. Mavjud chekни
        # qaytaramiz — sotuv baribir bir marta yozilgan.
        if idem_key:
            dup = SaleTransaction.objects.filter(idempotency_key=idem_key).first()
            if dup:
                return _receipt_response(dup)
        raise

    # OFF-2: chek ALLAQACHON saqlangan (atomic commit bo'ldi). Bundan keyingi
    # "best-effort" chaqiruvlar (fiskal, SMS) XATO bersa ham 500 qaytmasligi
    # kerak — aks holda offline navbat chekни ikkinchi marta yuborib, sotuv
    # ikki marta yozilardi (ikki barobar tushum, ombor ikki marta kamayadi).
    # MON-8: narx o'zgartirishlarini audit logga yozamiz (best-effort).
    if price_overrides:
        try:
            for ov in price_overrides:
                flag = ', TANNARXDAN PAST!' if ov['below_cost'] else ''
                AuditLog.objects.create(
                    user=request.user,
                    username_snapshot=request.user.username,
                    action=AuditLog.Action.UPDATE,
                    model_name='PriceOverride',
                    object_id=str(txn.pk),
                    object_repr=(f"{ov['code']} {ov['variant']}: katalog "
                                 f"{int(ov['catalog'])} → kiritilgan "
                                 f"{int(ov['entered'])} ({ov['diff_pct']}%{flag})")[:300],
                    changes={'price_override': ov, 'txn_id': txn.pk},
                )
        except Exception:
            logger.exception('price-override audit failed for txn %s', txn.pk)

    try:
        # OPS-15: fiskal chek va SMS endi FON ishlari. Ilgari ikkalasi ham
        # sotuv so'rovi ichida sinxron ketardi — tashqi xizmat sekinlashsa
        # kassir mijoz oldida kutib qolardi. Chek allaqachon yozilgan;
        # ular kechiksa ham sotuvga ta'sir qilmaydi va navbat qayta uriniб
        # ko'radi (5 martagacha, o'suvchi kechikish bilan).
        from .jobs import enqueue
        enqueue('fiscal_submit', txn_id=txn.pk)
    except Exception:
        logger.exception('fiskal ishni navbatga qo`yib bo`lmadi (txn %s)', txn.pk)

    sms_queued = False
    if data.get('send_sms') and customer_phone:
        try:
            from .jobs import enqueue
            enqueue('sms_receipt', txn_id=txn.pk, phone=customer_phone)
            sms_queued = True
        except Exception:
            logger.exception('SMS ishni navbatga qo`yib bo`lmadi (txn %s)', txn.pk)

    return JsonResponse({
        'ok': True,
        'txn_id': txn.pk,
        'receipt_url': f'/transaction/{txn.public_id}/?autoprint=1',
        'total': float(txn.total),
        'item_count': txn.item_count,
        # Mijoz tomoni bu maydonni o'qimaydi; endi u "navbatga olindi" degani.
        'sms': {'queued': sms_queued} if sms_queued else None,
    })


@login_required
def pos_park(request):
    """POST /pos/park/ — savatni vaqtincha saqlash.
    Body JSON: {label, lines, customer_name, customer_phone, order_discount, discount_reason}
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    label = (data.get('label') or '').strip()[:80]
    if not label:
        return JsonResponse({'ok': False, 'error': 'yorliq kerak'}, status=400)
    lines = data.get('lines') or []
    if not lines:
        return JsonResponse({'ok': False, 'error': 'savat bo\'sh'}, status=400)

    try:
        order_discount = max(0, float(data.get('order_discount') or 0))
    except (ValueError, TypeError):
        order_discount = 0

    parked = ParkedSale.objects.create(
        branch=branch,
        parked_by=request.user,
        label=label,
        cart_json=_json.dumps(lines),
        customer_name=(data.get('customer_name') or '').strip()[:120],
        customer_phone=(data.get('customer_phone') or '').strip()[:30],
        order_discount=order_discount,
        discount_reason=(data.get('discount_reason') or '').strip()[:200],
    )
    return JsonResponse({'ok': True, 'id': parked.pk, 'label': parked.label})


@login_required
def pos_parked_resume(request, pk):
    """POST /pos/parked/<pk>/resume/ — saqlangan savatni qaytarish va o'chirish."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    parked = ParkedSale.objects.filter(pk=pk, branch=branch).first()
    if not parked:
        return JsonResponse({'ok': False, 'error': 'topilmadi'}, status=404)
    try:
        lines = _json.loads(parked.cart_json)
    except ValueError:
        lines = []
    payload = {
        'ok': True,
        'lines': lines,
        'customer_name': parked.customer_name,
        'customer_phone': parked.customer_phone,
        'order_discount': float(parked.order_discount),
        'discount_reason': parked.discount_reason,
    }
    parked.delete()
    return JsonResponse(payload)


@admin_required
def payment_qr_list(request):
    """Admin: barcha filiallardagi static QR'lar ro'yxati + yangi qo'shish formasi."""
    if request.method == 'POST':
        action = request.POST.get('action') or 'create'
        if action == 'delete':
            try:
                pk = int(request.POST.get('pk') or 0)
            except ValueError:
                pk = 0
            PaymentQR.objects.filter(pk=pk).delete()
            messages.success(request, "QR o'chirildi.")
            return redirect('payment_qr_list')
        # create
        try:
            branch_id = int(request.POST.get('branch') or 0)
            branch_obj = Branch.objects.filter(pk=branch_id, is_active=True).first()
            provider = (request.POST.get('provider') or '').strip()
            if not branch_obj or not provider:
                messages.error(request, "Filial va provider tanlang.")
                return redirect('payment_qr_list')
            qr = PaymentQR(
                branch=branch_obj,
                provider=provider,
                label=(request.POST.get('label') or '').strip()[:120],
                qr_payload=(request.POST.get('qr_payload') or '').strip()[:500],
                instructions=(request.POST.get('instructions') or '').strip()[:200],
                is_active=True,
            )
            if 'qr_image' in request.FILES:
                qr.qr_image = request.FILES['qr_image']
            if not qr.qr_image and not qr.qr_payload:
                messages.error(request,
                    "QR rasm yuklang yoki QR ichidagi matn/URL kiriting.")
                return redirect('payment_qr_list')
            qr.save()
            messages.success(request,
                f"{branch_obj.name} uchun {qr.get_provider_display()} QR saqlandi.")
        except (ValueError, TypeError) as e:
            messages.error(request, f"Xatolik: {e}")
        return redirect('payment_qr_list')

    qrs = (PaymentQR.objects.select_related('branch')
           .order_by('branch__name', 'provider'))
    branches = Branch.objects.filter(is_active=True).order_by('name')
    return render(request, 'inventory/payment_qr_list.html', {
        'qrs': qrs, 'branches': branches,
        'providers': PaymentQR.Provider.choices,
    })


@login_required
def pos_static_qr(request, pk):
    """GET /pos/qr/<pk>/ — joriy filialdagi static QR ma'lumotini qaytaradi.
    Image URL (yoki qr_payload'dan generatsiya qilingan data URL) + ko'rsatma."""
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    qr = PaymentQR.objects.filter(pk=pk, branch=branch, is_active=True).first()
    if not qr:
        return JsonResponse({'ok': False, 'error': 'topilmadi'}, status=404)

    image_url = ''
    if qr.qr_image:
        image_url = qr.qr_image.url
    elif qr.qr_payload:
        # Generate QR on the fly into a data: URL
        import io, base64
        img = qrcode.make(qr.qr_payload)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        image_url = f'data:image/png;base64,{b64}'

    return JsonResponse({
        'ok': True,
        'provider': qr.provider,
        'provider_display': qr.get_provider_display(),
        'label': qr.label,
        'image_url': image_url,
        'instructions': qr.instructions,
    })


@login_required
def pos_payment_create(request):
    """POST /pos/payment/create/ JSON: {provider, amount, lines}
    PaymentIntent yaratadi va ref_code qaytaradi. POS modal'i shu ref_code'ni
    mijozga ko'rsatadi (mijoz to'lov izohi sifatida kiritadi)."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)
    provider = (data.get('provider') or '').strip()
    try:
        amount = float(data.get('amount') or 0)
    except (TypeError, ValueError):
        amount = 0
    if not provider or amount <= 0:
        return JsonResponse({'ok': False, 'error': 'parametr yetishmaydi'}, status=400)
    import secrets as _secrets
    ref_code = _secrets.token_hex(3).upper()  # 6 belgili kod
    intent = PaymentIntent.objects.create(
        branch=branch, initiated_by=request.user,
        provider=provider, amount=amount, ref_code=ref_code,
        cart_snapshot=_json.dumps(data.get('lines') or []),
    )
    return JsonResponse({
        'ok': True,
        'intent_id': intent.id,
        'ref_code': ref_code,
        'status': intent.status,
    })


@login_required
def pos_payment_check(request, pk):
    """GET /pos/payment/check/<id>/ — intent holatini qaytaradi.

    Real merchant API yo'q paytda DEMO_AUTO_PAY_SECONDS o'tgan bo'lsa
    avtomatik 'paid' deb belgilanadi (sinov uchun)."""
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    intent = PaymentIntent.objects.filter(pk=pk, branch=branch).first()
    if not intent:
        return JsonResponse({'ok': False, 'error': 'topilmadi'}, status=404)

    # Demo auto-pay: ma'lum vaqt o'tgach 'paid' bo'lib turadi
    demo_seconds = getattr(settings, 'DEMO_AUTO_PAY_SECONDS', 0)
    if (intent.status == PaymentIntent.Status.PENDING and demo_seconds > 0):
        elapsed = (timezone.now() - intent.created_at).total_seconds()
        if elapsed >= demo_seconds:
            intent.status = PaymentIntent.Status.PAID
            intent.paid_at = timezone.now()
            intent.provider_txn_id = f'demo-{intent.id}'
            intent.save(update_fields=['status', 'paid_at', 'provider_txn_id'])

    return JsonResponse({
        'ok': True,
        'intent_id': intent.id,
        'status': intent.status,
        'ref_code': intent.ref_code,
        'provider_txn_id': intent.provider_txn_id,
        'paid_at': intent.paid_at.isoformat() if intent.paid_at else None,
    })


@csrf_exempt
def payments_webhook(request, provider):
    """POST /payments/webhook/<provider>/ — real merchant API callback.

    PAY-1 XAVFSIZLIK: bu endpoint IMZO (signature) bilan himoyalangan.
      1. PAYMENTS_WEBHOOK_SECRET sozlanmagan bo'lsa — HAMMA chaqiruv RAD etiladi
         (hech qanday to'lov 'paid' bo'lmaydi). Hozir shунday — QR real ulanмaган.
      2. Sozlanган bo'lsa — 'X-Signature' sarlavhasi tananing HMAC-SHA256 imzosiga
         AYNAN mos kelishi shart (hmac.compare_digest).
      3. Faqat ANIQ ref_code bo'yicha topiladi — 'summa + oxirgi 30 daqiqa'
         fallback OLIB TASHLANDI (mijoz summani bilса, boshqa chekни paid
         qilиб qo'ymasin).
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    body_raw = request.body

    # 1) Imzo tekshiruvi — sirsiz endpoint hech narsani paid qilмaydi
    import hmac as _hmac, hashlib as _hashlib
    sig = (request.headers.get('X-Signature') or request.headers.get('X-Signature-Sha256') or '').strip()
    secret = getattr(settings, 'PAYMENTS_WEBHOOK_SECRET', '') or ''
    # Imzosiz chaqiruv — HAR DOIM rad (403). Mijoz kassada turib telefonidan
    # "to'ladim" deb yuborgan xabar hech qachon o'tmasligi SHART.
    if not sig:
        logger.warning('[payments webhook %s] REJECTED — missing signature', provider)
        return JsonResponse({'ok': False, 'error': 'signature required'}, status=403)
    # Imzo bor, lekin server siri sozlanmagan — tekshirib bo'lmaydi, RAD (503).
    if not secret:
        logger.warning('[payments webhook %s] REJECTED — no PAYMENTS_WEBHOOK_SECRET configured', provider)
        return JsonResponse({'ok': False, 'error': 'webhook not configured'}, status=503)
    expected = _hmac.new(secret.encode('utf-8'), body_raw, _hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expected):
        logger.warning('[payments webhook %s] REJECTED — bad signature', provider)
        return JsonResponse({'ok': False, 'error': 'invalid signature'}, status=403)

    try:
        payload = _json.loads(body_raw.decode('utf-8')) if body_raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}

    logger.info('[payments webhook %s] %s', provider, payload)

    # 2) FAQAT aniq ref_code bo'yicha (amount-window fallback yo'q)
    ref_code = (payload.get('ref_code') or payload.get('comment')
                or payload.get('memo') or '').strip().upper()
    intent = None
    if ref_code:
        intent = (PaymentIntent.objects
                  .filter(provider=provider, ref_code=ref_code,
                          status=PaymentIntent.Status.PENDING)
                  .order_by('-created_at').first())
    if not intent:
        return JsonResponse({'ok': False, 'error': 'mos intent topilmadi'},
                            status=404)

    intent.status = PaymentIntent.Status.PAID
    intent.paid_at = timezone.now()
    intent.provider_txn_id = str(payload.get('txn_id') or payload.get('id') or '')[:120]
    intent.save(update_fields=['status', 'paid_at', 'provider_txn_id'])
    return JsonResponse({'ok': True, 'intent_id': intent.id})


@login_required
def pos_promo_eval(request):
    """POST /pos/promo-eval/ {lines: [{stock_id, qty, sale_price}]}
    Aktiv aksiyalarni tekshiradi va qo'llab bo'ladigan chegirmalar
    ro'yxatini qaytaradi. Hech narsa o'zgartirmaydi — faqat ko'rsatish."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    lines_raw = data.get('lines') or []
    if not lines_raw:
        return JsonResponse({'ok': True, 'promotions': [], 'total_discount': 0})

    # Stock IDs -> product/category lookup
    stock_ids = [int(l['stock_id']) for l in lines_raw if 'stock_id' in l]
    stocks = {s.id: s for s in BranchStock.objects
              .filter(id__in=stock_ids)
              .select_related('variant__product__category')}

    # Build a per-line view: stock_id, qty, price, product_id, category_id
    cart_lines = []
    for ln in lines_raw:
        try:
            sid = int(ln['stock_id'])
            qty = int(ln['qty'])
            price = float(ln['sale_price'])
        except (KeyError, ValueError, TypeError):
            continue
        s = stocks.get(sid)
        if not s:
            continue
        cart_lines.append({
            'stock_id': sid,
            'qty': qty,
            'price': price,
            'product_id': s.variant.product_id,
            'category_id': s.variant.product.category_id,
        })

    applied, total_discount = _evaluate_promotions(cart_lines)
    return JsonResponse({
        'ok': True,
        'promotions': applied,
        'total_discount': round(total_discount, 2),
    })


def _evaluate_promotions(cart_lines):
    """V5: aktiv aksiyalardan kelib chiqadigan chegirmani SERVERда hisoblaydi.

    cart_lines: [{stock_id, qty, price, product_id, category_id}]. Bu yagona
    manba — pos_promo_eval (ko'rsatish) ham, pos_checkout (tekshirish) ham shuni
    chaqiradi, shunда mijoz o'ylab topган aksiya chegirmasi qabul qilinmaydi.
    (applied_list, total_discount_float) qaytaradi.
    """
    now = timezone.now()
    promos = (Promotion.objects
              .filter(is_active=True, valid_from__lte=now)
              .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=now))
              .prefetch_related('target_products'))

    applied = []
    total_discount = 0.0
    for promo in promos:
        # Find lines that match this promo's target
        target_product_ids = set(promo.target_products.values_list('id', flat=True))
        def matches(line):
            if target_product_ids and line['product_id'] not in target_product_ids:
                return False
            if promo.category_id and line['category_id'] != promo.category_id:
                return False
            return True
        matched = [l for l in cart_lines if matches(l)]
        if not matched:
            continue
        total_matched_qty = sum(l['qty'] for l in matched)
        discount = 0.0
        detail = ''
        if promo.promo_type == Promotion.Type.PERCENT_OFF:
            if total_matched_qty < promo.qty_required:
                continue
            matched_subtotal = sum(l['qty'] * l['price'] for l in matched)
            discount = matched_subtotal * float(promo.percent) / 100
            detail = f"{promo.percent}% × {total_matched_qty} dona"
        elif promo.promo_type == Promotion.Type.BUY_X_GET_Y:
            x = promo.qty_required
            y = promo.qty_free
            if x == 0 or total_matched_qty < x + y:
                continue
            # Number of full groups
            groups = total_matched_qty // (x + y)
            free_qty = groups * y
            # Free the cheapest items
            unit_prices = []
            for l in matched:
                unit_prices.extend([l['price']] * l['qty'])
            unit_prices.sort()
            discount = sum(unit_prices[:free_qty])
            detail = f"{x}+{y} bepul × {groups} marta = {free_qty} dona bepul"
        elif promo.promo_type == Promotion.Type.NTH_PERCENT:
            n = promo.qty_required
            if n == 0 or total_matched_qty < n:
                continue
            count = total_matched_qty // n
            # Apply to cheapest items
            unit_prices = []
            for l in matched:
                unit_prices.extend([l['price']] * l['qty'])
            unit_prices.sort()
            target_prices = unit_prices[:count]
            discount = sum(p * float(promo.percent) / 100 for p in target_prices)
            detail = f"{count} ta mahsulotga −{promo.percent}%"
        if discount > 0:
            applied.append({
                'id': promo.id,
                'name': promo.name,
                'type': promo.promo_type,
                'discount': round(discount, 2),
                'detail': detail,
            })
            total_discount += discount

    return applied, total_discount


@login_required
def pos_payment_intent(request):
    """POST /pos/payment/intent/ {provider, amount, txn_ref}
    Provider'dan QR/deeplink oladi. Sotuv hali yakunlanmagan — bu faqat
    intent yaratish. Mijoz to'laganidan keyin POS pos_checkout'ni chaqiradi."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)
    from .payments import get_provider
    provider_name = (data.get('provider') or '').strip()
    provider = get_provider(provider_name)
    if not provider:
        return JsonResponse({'ok': False, 'error': "noma'lum provider"}, status=400)
    try:
        amount = float(data.get('amount') or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return JsonResponse({'ok': False, 'error': "summa noto'g'ri"}, status=400)
    txn_ref = (data.get('txn_ref') or f'tmp-{request.user.id}-{int(timezone.now().timestamp())}')[:60]
    try:
        intent = provider.create_intent(amount, txn_ref)
    except Exception as e:
        logger.exception('Payment intent failed: %s', e)
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)
    return JsonResponse({
        'ok': True,
        'provider': provider.name,
        'provider_display': provider.display_name,
        'is_installment': provider.is_installment,
        **intent,
    })


@login_required
def pos_payment_status(request):
    """GET /pos/payment/status/?provider=click&intent_id=..."""
    provider_name = request.GET.get('provider') or ''
    intent_id = request.GET.get('intent_id') or ''
    from .payments import get_provider, available_providers
    # SEC-19: mijoz tanlagan provider'ga ISHONMAYMIZ. Faqat ANIQ yoqilgan
    # provider'lar. Aks holда ?provider=noop&intent_id=har-narsa → 'paid'
    # qaytarib, istalgan kirgan foydalanuvchи to'lovni "tasdiqlab" olardi.
    _enabled = {p.name for p in available_providers()}
    if provider_name not in _enabled:
        return JsonResponse({'ok': False, 'error': 'provider yoqilmagan'}, status=400)
    provider = get_provider(provider_name)
    if not provider or not intent_id:
        return JsonResponse({'ok': False, 'error': 'parametr yetishmaydi'}, status=400)
    try:
        status = provider.check_status(intent_id)
    except Exception as e:
        logger.exception('Payment status check failed: %s', e)
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)
    return JsonResponse({'ok': True, **status})


@login_required
def pos_unlock(request):
    """POST /pos/unlock/ {password} — joriy foydalanuvchining paroli to'g'ri
    bo'lsa, idle lock ochiladi. AJAX'dan chaqiriladi."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)
    password = (data.get('password') or '').strip()
    if not password:
        return JsonResponse({'ok': False, 'error': 'parol kerak'}, status=400)
    if request.user.check_password(password):
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': False, 'error': "noto'g'ri parol"}, status=401)


@login_required
def pos_txn_refundable(request, pk):
    """GET /pos/txn/<pk>/refundable/ — returns lines that can still be refunded."""
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    txn = (SaleTransaction.objects.filter(pk=pk, branch=branch)
           .prefetch_related('lines__variant__product', 'lines__returns')
           .first())
    if not txn:
        return JsonResponse({'ok': False, 'error': 'topilmadi'}, status=404)
    lines = []
    for s in txn.lines.all():
        already = sum(r.quantity for r in s.returns.all())
        remaining = s.quantity - already
        if remaining <= 0:
            continue
        lines.append({
            'sale_id': s.id,
            'product_code': s.variant.product.code,
            'product_name': s.variant.product.name,
            'size': s.variant.size,
            'color': s.variant.color,
            'sold_qty': s.quantity,
            'already_returned': already,
            'remaining': remaining,
            'sale_price': float(s.sale_price),
        })
    return JsonResponse({'ok': True, 'txn_id': txn.pk, 'lines': lines})


@login_required
def pos_refund(request):
    """POST /pos/refund/ JSON body: {lines: [{sale_id, qty, reason}]}.
    Processes refunds atomically; restores stock; returns total refunded.

    Requires an OPEN shift: refund returns cash from the drawer, so it must
    be attributed to the current shift for cash reconciliation."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)

    # C1 fix: require open shift
    open_shift = _open_shift_for(branch)
    if not open_shift:
        return JsonResponse({
            'ok': False,
            'error': "Smen ochilmagan. Qaytarish faqat ochiq smen davomida amalga oshiriladi.",
        }, status=400)

    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    items = data.get('lines') or []
    if not items:
        return JsonResponse({'ok': False, 'error': 'qator yo\'q'}, status=400)

    # S3: takroriy qaytarishни (timeout/qayta bosish) bloklaymiz. Bir martalik
    # kalit butun qaytarish partiyasига tegishli — birinchi Return qatoriga
    # yoziladi. O'sha kalit bilan qaytarish bo'lган bo'lса — takror emas.
    _idem = (data.get('idempotency_key') or '').strip()[:64] or None
    if _idem and Return.objects.filter(idempotency_key=_idem).exists():
        return JsonResponse({'ok': True, 'refunded_qty': 0,
                             'refunded_total': 0, 'duplicate': True})

    # REF-1: HAMMA qatorni atomic blokдан OLDIN tekshiramiz. Ilgari tekshiruv
    # atomic ichida `return` qilardi — bitta qator o'tib, keyingisi yiqilса,
    # o'tgani COMMIT bo'lib, 400 qaytardi. Kassir tuzatib qayta yuborsa —
    # birinchi qator IKKINCHI marta qaytarilardi (ikki barobar naqd/ombor).
    parsed = []
    for it in items:
        try:
            sid = int(it['sale_id'])
            qty = int(it['qty'])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({'ok': False, 'error': 'noto\'g\'ri qator'}, status=400)
        if qty <= 0:
            continue
        reason = (it.get('reason') or '').strip()[:200]
        sale = (Sale.objects.select_related('variant', 'branch')
                .filter(pk=sid, branch=branch).first())
        if not sale:
            return JsonResponse({'ok': False, 'error': f'sale {sid} topilmadi'}, status=400)
        parsed.append({'sid': sale.pk, 'qty': qty, 'reason': reason,
                       'code': sale.variant.product.code})
    if not parsed:
        return JsonResponse({'ok': False, 'error': 'qator yo\'q'}, status=400)

    class _RefundAbort(Exception):
        def __init__(self, payload, status=400):
            self.payload = payload
            self.status = status

    refunded_total = Decimal('0')
    refunded_qty = 0
    _first_row = True
    _txn_refunded = {}   # REF-3: chek pk -> shu chekда jami qaytarilgan naqd
    try:
        with transaction.atomic():
            for p in parsed:
                # REF-1: qatorni QULFLAB qayta tekshiramiz — bir vaqtда ikki
                # refund bir xil qatorga o'tmasin (pos_exchange kabi).
                # DIQQAT: 'transaction' NULLABLE FK — uni select_related qilsak
                # LEFT OUTER JOIN bo'ladi va Postgres select_for_update (FOR UPDATE)
                # ни nullable outer join'ga qo'llay olmaydi (SQLite bunga e'tibor
                # bermaydi — shu bois test'да chiqmagan, prod'да 500 bergan).
                # variant/branch — NOT NULL (inner join), FOR UPDATE ular bilan
                # ishlaydi. transaction'ni keyin kerak bo'lganда lazy o'qiymiz.
                sale = (Sale.objects.select_for_update()
                        .select_related('variant', 'branch')
                        .filter(pk=p['sid'], branch=branch).first())
                if not sale:
                    raise _RefundAbort({'ok': False, 'error': f"sale {p['sid']} topilmadi"})
                already = sale.returns.aggregate(s=Sum('quantity'))['s'] or 0
                remaining = sale.quantity - already
                if p['qty'] > remaining:
                    raise _RefundAbort({'ok': False,
                        'error': f"{p['code']}: faqat {remaining} dona qaytarish mumkin"})
                stock = (BranchStock.objects.select_for_update()
                         .filter(variant=sale.variant, branch=sale.branch).first())
                # R2: ochiq narxli tovar sotuvда zaxira KAMAYTIRILMAGAN — shuning
                # uchun qaytarishда ham tiklamaymiz (aks holda yo'qdan zaxira).
                if stock and not sale.variant.product.is_open_price:
                    stock.stock_count = F('stock_count') + p['qty']
                    stock.save(update_fields=['stock_count'])
                # REF-3: qaytariladigan HAQIQIY naqд. Dona O'Z narxida (order_discount
                # taqsimlanmaydi). AMMO bir chek bo'yicha jami qaytarish chek
                # TO'LOVIdan oshmaydi — shu bois butun chek qaytганда chegirма
                # oxirgi qaytarishга singib, jami = to'langan summа bo'ladi
                # (egasining qoidasi: qisman → dona narxi, to'liq → chek jamisi).
                _per_unit = (sale.net_line_total() / sale.quantity
                             if sale.quantity > 0 else Decimal(sale.sale_price))
                _item_value = Decimal(p['qty']) * _per_unit
                _txn = sale.transaction
                if _txn is not None and Decimal(_txn.order_discount or 0) > 0:
                    _paid = Decimal(_txn.total)
                    if _txn.pk not in _txn_refunded:
                        _txn_refunded[_txn.pk] = sum(
                            (r.effective_cash_refund for r in
                             Return.objects.filter(sale__transaction_id=_txn.pk)),
                            Decimal('0'))
                    _prior = _txn_refunded[_txn.pk]
                    _cap = _paid - _prior if _paid > _prior else Decimal('0')
                    _cash = min(_item_value, _cap)
                    _txn_refunded[_txn.pk] = _prior + _cash
                else:
                    _cash = _item_value
                _rk = dict(sale=sale, shift=open_shift, quantity=p['qty'],
                           reason=p['reason'], refunded_by=request.user,
                           refund_cash=_cash)
                if _idem and _first_row:
                    _rk['idempotency_key'] = _idem   # S3: partiyaning bir martalik kaliti
                    _first_row = False
                try:
                    ret = Return.objects.create(**_rk)
                except IntegrityError:
                    # Poyga: boshqa so'rov ayni kalitни yozib ulgurdi — takror.
                    raise _RefundAbort({'ok': True, 'refunded_qty': 0,
                        'refunded_total': 0, 'duplicate': True}, status=200)
                refunded_qty += p['qty']
                refunded_total += _cash
    except _RefundAbort as e:
        return JsonResponse(e.payload, status=e.status)
    return JsonResponse({
        'ok': True,
        'refunded_qty': refunded_qty,
        'refunded_total': float(refunded_total),
    })


@login_required
def pos_exchange(request):
    """POST /pos/exchange/ — ALMASHTIRISH: eski tovar(lar) qaytadi, yangi
    tovar(lar) beriladi, faqat NARX FARQI hisoblanadi.

    JSON body:
      { returns:  [{sale_id, qty}],                 # qaytadigan eski tovar(lar)
        new_lines:[{stock_id, qty, sale_price}],    # beriladigan yangi tovar(lar)
        payment_method: 'cash'|'card'|'transfer',   # yangi qimmat bo'lsa — farq uchun
        reason: '...' }

    Pul modeli (kassa ANIQ to'g'ri qolishi uchun):
      old_total = qaytgan tovar qiymati,  new_total = yangi tovar qiymati
      credit = min(old_total, new_total)  -> yangi chekka order_discount (trade-in krediti)
      extra  = new_total - old_total
        extra > 0  -> mijoz farqni to'laydi (tanlangan usul), yangi chek net = extra
        extra < 0  -> do'kon farqni NAQD qaytaradi (Return.cash_refunded = |extra|)
        extra == 0 -> pul harakati yo'q
    Yangi chek net'i (= max(0,extra)) tanlangan usul bo'yicha kassaga tushadi;
    almashtirish qaytarishlari kassaga faqat cash_refunded miqdorida ta'sir qiladi.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)

    open_shift = _open_shift_for(branch)
    if not open_shift:
        return JsonResponse({'ok': False,
            'error': "Smen ochilmagan. Almashtirish faqat ochiq smen davomida amalga oshiriladi."},
            status=400)

    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    from decimal import Decimal, InvalidOperation

    def _m(v):
        try:
            d = Decimal(str(v if v not in (None, '') else 0))
        except (InvalidOperation, ValueError, TypeError):
            d = Decimal('0')
        return d if d >= 0 else Decimal('0')

    ret_items = data.get('returns') or []
    new_items = data.get('new_lines') or []
    if not ret_items:
        return JsonResponse({'ok': False, 'error': 'qaytariladigan tovar tanlanmagan'}, status=400)
    if not new_items:
        return JsonResponse({'ok': False, 'error': 'yangi tovar tanlanmagan'}, status=400)

    method = (data.get('payment_method') or 'cash').strip()
    if method not in ('cash', 'card', 'transfer'):
        method = 'cash'
    reason = (data.get('reason') or '').strip()[:200]

    parsed_new = []
    for ln in new_items:
        try:
            sid = int(ln['stock_id']); qty = int(ln['qty']); price = _m(ln['sale_price'])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({'ok': False, 'error': "yangi qator noto'g'ri"}, status=400)
        if qty <= 0:
            return JsonResponse({'ok': False, 'error': "yangi qator: soni noto'g'ri"}, status=400)
        if price > Decimal('9999999999.99'):
            return JsonResponse({'ok': False,
                'error': "Narx juda katta. Iltimos, to'g'ri narx kiriting."}, status=400)
        parsed_new.append({'sid': sid, 'qty': qty, 'price': price})

    parsed_ret = []
    for it in ret_items:
        try:
            sid = int(it['sale_id']); qty = int(it['qty'])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({'ok': False, 'error': "qaytish qatori noto'g'ri"}, status=400)
        if qty > 0:
            parsed_ret.append({'sale_id': sid, 'qty': qty})
    if not parsed_ret:
        return JsonResponse({'ok': False, 'error': "qaytish soni noto'g'ri"}, status=400)

    class _Abort(Exception):
        def __init__(self, payload, status=400):
            self.payload = payload; self.status = status

    try:
        with transaction.atomic():
            # ---- eski tovar(lar)ni tekshirib qiymatlash ----
            old_total = Decimal('0')
            ret_ctx = []
            for r in parsed_ret:
                sale = (Sale.objects.select_for_update()
                        .select_related('variant__product', 'branch')
                        .filter(pk=r['sale_id'], branch=branch).first())
                if not sale:
                    raise _Abort({'ok': False, 'error': f"sotuv {r['sale_id']} topilmadi"})
                already = sale.returns.aggregate(s=Sum('quantity'))['s'] or 0
                remaining = sale.quantity - already
                if r['qty'] > remaining:
                    raise _Abort({'ok': False, 'error':
                        f"{sale.variant.product.code}: faqat {remaining} dona qaytarish mumkin"})
                # Trade-in qiymati = tovarning O'Z qatori narxi (order_discount
                # taqsimlanmaydi — qaytarish bilan bir xil siyosat, egasi qarori).
                per_unit = (sale.net_line_total() / sale.quantity) if sale.quantity > 0 else sale.sale_price
                amt = Decimal(r['qty']) * Decimal(per_unit)
                old_total += amt
                ret_ctx.append((sale, r['qty']))

            # ---- yangi tovar(lar)ni tekshirib qiymatlash (qoldiqni bloklaymiz) ----
            sids = sorted({l['sid'] for l in parsed_new})
            locked = {s.pk: s for s in BranchStock.objects.select_for_update()
                      .select_related('variant__product', 'branch')
                      .filter(pk__in=sids, branch=branch)}
            new_total = Decimal('0')
            for ln in parsed_new:
                stock = locked.get(ln['sid'])
                if not stock:
                    raise _Abort({'ok': False, 'error': f"yangi tovar {ln['sid']} topilmadi"})
                if (not stock.variant.product.is_open_price
                        and ln['qty'] > stock.stock_count):
                    raise _Abort({'ok': False, 'error':
                        f"{stock.variant.product.code} {stock.variant.size}/{stock.variant.color}: "
                        f"omborda faqat {stock.stock_count} ta bor"})
                new_total += Decimal(ln['qty']) * Decimal(ln['price'])

            credit = min(old_total, new_total)
            extra = new_total - old_total
            cash_back = (old_total - new_total) if old_total > new_total else Decimal('0')
            new_pm = method if extra > 0 else 'cash'

            txn = SaleTransaction.objects.create(
                branch=branch, sold_by=request.user,
                payment_method=new_pm, payment_breakdown=[],
                note=(reason or 'Almashtirish')[:200],
                order_discount=credit,
                # DISC-1: bu CHEGIRMA EMAS — mijoz eski tovar bilan to'lagan.
                # Alohida maydonда saqlanadi, shunda hisobotlar uni kassir
                # bergan chegirma sifatida ko'rsatmaydi.
                exchange_credit=credit,
                discount_reason='Almashtirish: eski tovar hisobiga',
                shift=open_shift,
            )
            for ln in parsed_new:
                stock = locked[ln['sid']]
                if not stock.variant.product.is_open_price:
                    stock.stock_count = F('stock_count') - ln['qty']
                    stock.save()
                Sale.objects.create(
                    transaction=txn, variant=stock.variant, branch=stock.branch,
                    quantity=ln['qty'], sale_price=ln['price'],
                    cost_at_sale=stock.cost_price, line_discount=Decimal('0'),
                    sold_by=request.user,
                )

            # eski tovar(lar): omborga qaytarish + almashtirish Return'i.
            # cash_back butun almashtirishga bitta — birinchi qatorga yozamiz.
            cb_left = cash_back
            for sale, qty in ret_ctx:
                stock = BranchStock.objects.filter(
                    variant=sale.variant, branch=sale.branch).first()
                # R2: ochiq narxli tovar zaxira yuritmaydi — tiklamaymiz.
                if stock and not sale.variant.product.is_open_price:
                    stock.stock_count = F('stock_count') + qty
                    stock.save()
                this_cb = cb_left if cb_left > 0 else Decimal('0')
                cb_left = Decimal('0')
                Return.objects.create(
                    sale=sale, shift=open_shift, quantity=qty,
                    reason=(reason or f'Almashtirish → chek #{txn.pk}')[:200],
                    refunded_by=request.user,
                    is_exchange=True, cash_refunded=this_cb,
                )
    except _Abort as e:
        return JsonResponse(e.payload, status=e.status)

    return JsonResponse({
        'ok': True,
        'txn_id': txn.pk,
        'receipt_url': f'/transaction/{txn.public_id}/?autoprint=1',
        'old_total': float(old_total),
        'new_total': float(new_total),
        'extra': float(extra),
        'cash_back': float(cash_back),
        'method': new_pm,
    })


@login_required
def pos_parked_delete(request, pk):
    """POST /pos/parked/<pk>/delete/ — saqlangan savatni o'chirish."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    branch = _user_branch_or_403(request)
    if branch is None:
        return JsonResponse({'ok': False, 'error': 'no branch'}, status=403)
    deleted, _ = ParkedSale.objects.filter(pk=pk, branch=branch).delete()
    return JsonResponse({'ok': bool(deleted)})


def _cart_lines(cart):
    """Resolve cart {stock_id: qty} → list of {stock, qty, available, ok}."""
    if not cart:
        return []
    stock_ids = [int(sid) for sid in cart.keys() if str(sid).isdigit()]
    stocks = {bs.id: bs for bs in BranchStock.objects.filter(id__in=stock_ids)
              .select_related('variant__product', 'branch')}
    lines = []
    for sid, qty in cart.items():
        stock = stocks.get(int(sid))
        if not stock:
            continue
        lines.append({
            'stock': stock,
            'qty': int(qty),
            'available': stock.stock_count,
            'ok': int(qty) <= stock.stock_count,
            'subtotal': int(qty) * (stock.sale_price or stock.variant.product.default_sale_price),
        })
    return lines


@login_required
def pos_customer_lookup(request):
    """JSON: GET /pos/customer/?phone=998... → returns matches."""
    phone = (request.GET.get('phone') or '').strip()
    if len(phone) < 3:
        return JsonResponse({'matches': []})
    cleaned = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not cleaned:
        return JsonResponse({'matches': []})
    qs = Customer.objects.filter(phone__icontains=cleaned)[:5]
    return JsonResponse({'matches': [
        {'id': c.pk, 'name': c.name, 'phone': c.phone} for c in qs
    ]})
