from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, F, Q, DecimalField, ExpressionWrapper, Count, Max, Min
from django.db.models.functions import (
    Coalesce, TruncDate, TruncWeek, TruncMonth, ExtractWeekDay,
)
from django.db import transaction
from django.http import HttpResponseForbidden, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.core.cache import cache
import json as _json
from django.utils import timezone
from datetime import timedelta, datetime, date
import csv
import io
import re

import logging
logger = logging.getLogger(__name__)

import qrcode
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

from .models import (
    User, Branch, Product, ProductVariant, BranchStock,
    Category, Intake, Sale, AuditLog, SaleTransaction, Return, Customer, Shift,
    Transfer, TransferLine, Stocktake, StocktakeCount, ParkedSale, Promotion,
    PaymentQR, PaymentIntent,
    Supplier, IntakeSession,
)
_SIZE_WORDS = {'xs', 's', 'm', 'l', 'xl', 'xxl', 'xxxl', '2xl', '3xl', '4xl'}


def smart_title(raw):
    """Nomlarni bir xil ko'rinishga keltiradi: "head and shoulders" ->
    "Head And Shoulders". Qoidalar:
    - faqat to'liq kichik harfli so'zlar bosh harfga ko'tariladi
      (EDP, XPro kabi aralash/katta yozuvlar tegilmaydi)
    - raqamli so'zlar (400ml, 2/1) o'zgarmaydi
    - o'lcham so'zlari (s, m, l, xl...) to'liq KATTA bo'ladi
    - ortiqcha bo'shliqlar yig'ishtiriladi
    """
    words = (raw or '').split()
    out = []
    for w in words:
        if w.lower() in _SIZE_WORDS:
            out.append(w.upper())
        elif w.islower() and not any(ch.isdigit() for ch in w):
            out.append(w[0].upper() + w[1:])
        else:
            out.append(w)
    return ' '.join(out)


def ean13_check_digit(body12):
    """12 xonali EAN body uchun nazorat raqami."""
    total = 0
    for i, ch in enumerate(body12):
        d = int(ch)
        total += d if (i % 2 == 0) else d * 3
    return (10 - (total % 10)) % 10


def gen_internal_ean13(seed_int):
    """Ichki (do'kon) EAN-13: '2' prefiks + 11 xonali seed + nazorat raqami.
    GS1 '20-29' oralig'i do'kon ichki tovarlar uchun ajratilgan."""
    body = ('2' + str(int(seed_int)).zfill(11))[:12]
    return body + str(ean13_check_digit(body))


def parse_dec(raw):
    """Foydalanuvchi kiritgan pul/son matnini Decimal'ga aylantiradi.

    "3 000", "3\u00a0000", "30,000", "30000,50", "30'000" — barchasi ishlaydi.
    Bo'sh bo'lsa 0.
    """
    from decimal import Decimal
    raw = (raw or '').strip()
    for ch in (' ', '\u00a0', '\u202f', "'"):
        raw = raw.replace(ch, '')
    if ',' in raw and '.' in raw:
        raw = raw.replace(',', '')          # 1,234.56 -> 1234.56
    elif ',' in raw:
        head, _, tail = raw.rpartition(',')
        if head and len(tail) in (1, 2) and ',' not in head:
            raw = head + '.' + tail          # 30000,5 -> 30000.5
        else:
            raw = raw.replace(',', '')       # 30,000 -> 30000
    return Decimal(raw) if raw else Decimal('0')


from .forms import (
    LoginForm, BranchForm, ProductForm, CategoryForm,
    IntakeForm, SaleForm, UserCreateForm, UserEditForm, ReportForm,
    PIVOT_METRIC_CHOICES, PIVOT_DIM_CHOICES,
)


def admin_required(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_admin():
            return HttpResponseForbidden(
                "<h3>Ruxsat yo'q. Bu sahifa faqat administrator uchun.</h3>"
            )
        return view_func(request, *args, **kwargs)
    return wrapper


def get_user_branch(user):
    """Sotuvchi uchun majburiy filial. Admin uchun None bo'lishi mumkin."""
    return user.branch


def normalize_code(typed):
    """Kiritilgan kodni standart shaklga keltirish: OYO1 → OYO-0001"""
    if not typed:
        return ''
    t = typed.strip().upper().replace(' ', '')
    m = re.match(r'^([A-Z]+)-?(\d+)$', t)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):04d}'
    return t


# ---------- HEALTH ----------

def healthz(request):
    """Liveness probe for Render / monitoring. Cheap, no DB hit."""
    return HttpResponse('ok', content_type='text/plain')


# ---------- AUTH ----------

def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    # S5: rate limit — max 5 failed attempts per IP in 5 minutes
    from django.core.cache import cache
    ip = (request.META.get('HTTP_X_FORWARDED_FOR') or
          request.META.get('REMOTE_ADDR') or '0.0.0.0').split(',')[0].strip()
    fail_key = f'login_fail:{ip}'
    fail_count = cache.get(fail_key) or 0
    if fail_count >= 5:
        messages.error(request,
            "Juda ko'p urinish. 5 daqiqa kutib qayta urinib ko'ring.")
        return render(request, 'inventory/login.html',
                      {'form': LoginForm(request), 'rate_limited': True})

    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            cache.delete(fail_key)  # reset counter on success
            login(request, form.get_user())
            messages.success(request, f'Xush kelibsiz, {request.user.username}!')
            return redirect('home')
        else:
            # Increment fail counter (5-min TTL)
            cache.set(fail_key, fail_count + 1, timeout=300)
            remaining = 5 - (fail_count + 1)
            if remaining > 0:
                messages.warning(request,
                    f"Login muvaffaqiyatsiz. Yana {remaining} ta urinishingiz qoldi.")
    else:
        form = LoginForm(request)
    return render(request, 'inventory/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def home(request):
    if request.user.is_admin():
        return redirect('dashboard')
    return redirect('lookup')


# ---------- LOOKUP (asosiy qidiruv) ----------

@login_required
def lookup(request):
    raw_query = (request.GET.get('code') or '').strip()
    code = normalize_code(raw_query.upper())
    product = None
    branches_data = []
    suggestions = []
    product_stats = None
    popular_products = []
    categories_quick = []

    if raw_query:
        # Try exact code first — either internal code or manufacturer barcode
        product = Product.objects.filter(
            Q(code=code) | Q(external_barcode=raw_query)
        ).first()
        if not product:
            _vm = (ProductVariant.objects.filter(barcode=raw_query)
                   .select_related('product').first())
            if _vm:
                product = _vm.product

        # If no code match, fall back to name / category / description search
        if not product:
            matches = (Product.objects
                .filter(Q(name__icontains=raw_query) |
                        Q(category__name__icontains=raw_query) |
                        Q(description__icontains=raw_query))
                .select_related('category')[:20])
            if matches.count() == 1:
                product = matches.first()
                code = product.code
            elif matches.exists():
                suggestions = list(matches)
            else:
                messages.warning(request,
                    f"'{raw_query}' bo'yicha mahsulot topilmadi.")

        if product:
            variants = list(product.variants.all())
            sizes = sorted({v.size for v in variants}, key=lambda s: (len(s), s))
            colors = sorted({v.color for v in variants})

            if request.user.is_admin():
                branches = Branch.objects.filter(is_active=True)
            else:
                branches = Branch.objects.filter(pk=request.user.branch_id) \
                    if request.user.branch_id else Branch.objects.none()

            # Aggregate stats across all branches (admin) or single branch
            total_stock = 0
            total_value = 0
            zero_count = 0
            low_count = 0
            for br in branches:
                stocks = BranchStock.objects.filter(
                    variant__product=product, branch=br
                ).select_related('variant')
                matrix = {(s.variant.size, s.variant.color): s for s in stocks}
                br_total = sum(s.stock_count for s in stocks)
                br_value = sum(s.stock_count * float(s.cost_price) for s in stocks)
                total_stock += br_total
                total_value += br_value
                for s in stocks:
                    if s.stock_count == 0:
                        zero_count += 1
                    elif s.stock_count <= 3:
                        low_count += 1
                # Sotuvchi uchun yassi variant ro'yxati: bor bo'lganlar
                # oldinda, ko'p qoldiq oldinda, keyin rang/o'lcham bo'yicha
                var_list = [{
                    'color': st.variant.color,
                    'size': st.variant.size,
                    'barcode': st.variant.barcode or '',
                    'count': st.stock_count,
                    'sale': float(st.sale_price or product.default_sale_price or 0),
                    'wholesale': float(st.wholesale_price or 0),
                    'stock_id': st.id,
                } for st in stocks]
                var_list.sort(key=lambda r: (r['count'] == 0, -r['count'],
                                             r['color'], r['size']))
                branches_data.append({
                    'branch': br, 'matrix': matrix,
                    'sizes': sizes, 'colors': colors,
                    'total': br_total, 'value': br_value,
                    'variants': var_list,
                    'in_stock_n': sum(1 for r in var_list if r['count'] > 0),
                })

            # 30-day sales velocity
            since_30d = timezone.now() - timedelta(days=30)
            velocity_agg = Sale.objects.filter(
                variant__product=product,
                sold_at__gte=since_30d,
            ).aggregate(
                qty=Sum('quantity'),
                txns=Count('transaction', distinct=True),
            )
            sold_30d = velocity_agg['qty'] or 0
            txns_30d = velocity_agg['txns'] or 0
            daily_avg = sold_30d / 30 if sold_30d else 0
            days_left = (total_stock / daily_avg) if daily_avg else None

            product_stats = {
                'total_stock': total_stock,
                'total_value': total_value,
                'variants_count': len(variants),
                'branches_count': len(branches_data),
                'zero_count': zero_count,
                'low_count': low_count,
                'sold_30d': sold_30d,
                'txns_30d': txns_30d,
                'days_left': days_left,
            }
    else:
        # No search — show popular products and category quick-jumps
        since_30d = timezone.now() - timedelta(days=30)
        top_rows = (Sale.objects
                    .filter(sold_at__gte=since_30d)
                    .values('variant__product')
                    .annotate(qty=Sum('quantity'))
                    .order_by('-qty')[:8])
        top_ids = [r['variant__product'] for r in top_rows]
        prods_map = {p.id: p for p in Product.objects
                     .filter(id__in=top_ids).select_related('category')}
        for r in top_rows:
            p = prods_map.get(r['variant__product'])
            if p:
                popular_products.append({'p': p, 'qty': r['qty']})
        categories_quick = list(Category.objects.annotate(
            n=Count('products')
        ).filter(n__gt=0).order_by('-n')[:8])

    return render(request, 'inventory/lookup.html', {
        'code': raw_query, 'product': product,
        'branches_data': branches_data, 'suggestions': suggestions,
        'product_stats': product_stats,
        'popular_products': popular_products,
        'categories_quick': categories_quick,
        'query_looks_like_barcode': bool(raw_query) and raw_query.isdigit() and len(raw_query) >= 6,
    })


# ---------- DASHBOARD ----------

DASHBOARD_CACHE_KEY = 'dashboard:hq:v1'
DASHBOARD_CACHE_TTL = 60  # seconds — heavy aggregates only; recent_sales stays live


def _dashboard_aggregates():
    """Compute the cacheable, expensive part of the dashboard.

    Returned dict is JSON-serializable (Branch model is keyed by id, looked up
    on render). TTL is short (60s) so per-shift drift is invisible to users.
    Invalidated explicitly when a SaleTransaction is saved (see signals.py).
    """
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)

    def _day_range(d):
        tz = timezone.get_current_timezone()
        start = datetime.combine(d, datetime.min.time()).replace(tzinfo=tz)
        return start, start + timedelta(days=1)

    today_start, today_end = _day_range(today)
    yesterday_start, yesterday_end = _day_range(yesterday)

    revenue_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    def _agg(qs):
        a = qs.aggregate(
            revenue=Sum(revenue_expr),
            cost=Sum(cost_expr),
            qty=Sum('quantity'),
            txns=Count('transaction', distinct=True),
        )
        rev = float(a['revenue'] or 0)
        cost = float(a['cost'] or 0)
        return {
            'revenue': rev,
            'cost': cost,
            'profit': rev - cost,
            'margin': (rev - cost) / rev * 100 if rev else 0,
            'qty': a['qty'] or 0,
            'txns': a['txns'] or 0,
        }

    today_stats = _agg(Sale.objects.filter(sold_at__gte=today_start, sold_at__lt=today_end))
    yesterday_stats = _agg(Sale.objects.filter(sold_at__gte=yesterday_start, sold_at__lt=yesterday_end))

    # 7-day trend — single annotated query
    week_start = today_start - timedelta(days=6)
    trend_qs = (
        Sale.objects.filter(sold_at__gte=week_start, sold_at__lt=today_end)
        .annotate(day=TruncDate('sold_at'))
        .values('day')
        .annotate(rev=Sum(revenue_expr), q=Sum('quantity'))
    )
    day_map = {r['day']: r for r in trend_qs}
    trend_labels, trend_revenue, trend_qty = [], [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        r = day_map.get(d) or {}
        trend_labels.append(d.strftime('%a %d'))
        trend_revenue.append(float(r.get('rev') or 0))
        trend_qty.append(r.get('q') or 0)

    # Inventory snapshot — one aggregate covers stock count + value
    inv_totals = BranchStock.objects.aggregate(
        s=Sum('stock_count'),
        v=Sum(ExpressionWrapper(F('stock_count') * F('cost_price'),
                                output_field=DecimalField(max_digits=14, decimal_places=2))),
    )
    total_stock = inv_totals['s'] or 0
    stock_value = float(inv_totals['v'] or 0)

    # Per-branch today — 2 aggregated queries, independent of branch count
    rev_by_branch = {
        r['branch_id']: r for r in
        Sale.objects.filter(sold_at__gte=today_start, sold_at__lt=today_end)
        .values('branch_id')
        .annotate(
            revenue=Sum(revenue_expr),
            qty=Sum('quantity'),
            txns=Count('transaction', distinct=True),
        )
    }
    stock_by_branch = {
        r['branch_id']: r['stock_count__sum']
        for r in BranchStock.objects.values('branch_id').annotate(stock_count__sum=Sum('stock_count'))
    }

    branch_today_raw = [
        {
            'branch_id': bid,
            'revenue': float((rev_by_branch.get(bid) or {}).get('revenue') or 0),
            'qty': (rev_by_branch.get(bid) or {}).get('qty') or 0,
            'txns': (rev_by_branch.get(bid) or {}).get('txns') or 0,
            'stock': stock_by_branch.get(bid, 0),
        }
        for bid in Branch.objects.filter(is_active=True).values_list('id', flat=True)
    ]

    top_today = list(
        Sale.objects.filter(sold_at__gte=today_start, sold_at__lt=today_end)
        .values('variant__product__code', 'variant__product__name')
        .annotate(qty=Sum('quantity'), revenue=Sum(revenue_expr))
        .order_by('-qty')[:5]
    )
    for r in top_today:
        r['revenue'] = float(r['revenue'] or 0)

    return {
        'today_iso': today.isoformat(),
        'yesterday_iso': yesterday.isoformat(),
        'today_stats': today_stats,
        'yesterday_stats': yesterday_stats,
        'trend_labels': trend_labels,
        'trend_revenue': trend_revenue,
        'trend_qty': trend_qty,
        'total_stock': total_stock,
        'stock_value': stock_value,
        'branch_today_raw': branch_today_raw,
        'top_today': top_today,
    }


@admin_required
def dashboard(request):
    agg = cache.get_or_set(DASHBOARD_CACHE_KEY, _dashboard_aggregates, DASHBOARD_CACHE_TTL)

    today = date.fromisoformat(agg['today_iso'])
    yesterday = date.fromisoformat(agg['yesterday_iso'])
    today_stats = agg['today_stats']
    yesterday_stats = agg['yesterday_stats']

    def _delta(now, prev):
        # Kunning boshida (bugun hali 0) -100% ko'rsatmaymiz — bu
        # signal emas, shunchaki kun boshlangani. Delta faqat bugun
        # ham savdo bo'lganda ma'noli.
        if not prev or not now:
            return None
        return (now - prev) / prev * 100

    deltas = {
        'revenue': _delta(today_stats['revenue'], yesterday_stats['revenue']),
        'qty': _delta(today_stats['qty'], yesterday_stats['qty']),
        'txns': _delta(today_stats['txns'], yesterday_stats['txns']),
        'profit': _delta(today_stats['profit'], yesterday_stats['profit']),
    }

    # Hydrate branch_today_raw with live Branch objects (Branch model not cached
    # in case is_active toggles during the 60s window)
    active_branches = {b.id: b for b in Branch.objects.filter(is_active=True)}
    branch_today = []
    for row in agg['branch_today_raw']:
        br = active_branches.get(row['branch_id'])
        if not br:
            continue
        branch_today.append({
            'branch': br,
            'revenue': row['revenue'],
            'qty': row['qty'],
            'txns': row['txns'],
            'stock': row['stock'],
        })

    total_products = Product.objects.count()
    total_branches = len(active_branches)

    # ---- Live (uncached) data: attention widgets + recent activity ----
    low_stock_count = BranchStock.objects.filter(stock_count__lte=3).count()
    low_stock_preview = list(BranchStock.objects.filter(stock_count__lte=3)
                             .select_related('variant__product', 'branch')
                             .order_by('stock_count')[:5])
    out_of_stock_count = BranchStock.objects.filter(stock_count=0).count()
    open_shifts_count = Shift.objects.filter(status=Shift.Status.OPEN).count()
    pending_intents_count = PaymentIntent.objects.filter(
        status=PaymentIntent.Status.PENDING,
        created_at__gte=timezone.now() - timedelta(hours=24),
    ).count()
    in_transit_count = Transfer.objects.filter(status=Transfer.Status.IN_TRANSIT).count()
    open_parked_count = ParkedSale.objects.count()

    recent_sales = (SaleTransaction.objects
                    .select_related('branch', 'sold_by')
                    .prefetch_related('lines')
                    .order_by('-sold_at')[:8])

    return render(request, 'inventory/dashboard.html', {
        'today': today, 'yesterday': yesterday,
        'today_stats': today_stats,
        'yesterday_stats': yesterday_stats,
        'deltas': deltas,
        'trend_labels': agg['trend_labels'],
        'trend_revenue': agg['trend_revenue'],
        'trend_qty': agg['trend_qty'],
        'total_products': total_products,
        'total_branches': total_branches,
        'total_stock': agg['total_stock'],
        'stock_value': agg['stock_value'],
        'branch_today': branch_today,
        'top_today': agg['top_today'],
        'low_stock_count': low_stock_count,
        'low_stock_preview': low_stock_preview,
        'out_of_stock_count': out_of_stock_count,
        'open_shifts_count': open_shifts_count,
        'pending_intents_count': pending_intents_count,
        'in_transit_count': in_transit_count,
        'open_parked_count': open_parked_count,
        'recent_sales': recent_sales,
    })


# ---------- PRODUCTS ----------

@admin_required
@admin_required
def product_bulk_update(request):
    """POST: tanlangan mahsulotlarga bulk amal qo'llash."""
    if request.method != 'POST':
        return redirect('product_list')
    ids = request.POST.getlist('id')
    if not ids:
        messages.warning(request, "Bironta ham mahsulot tanlanmagan.")
        return redirect('product_list')
    op = request.POST.get('op') or ''
    if op == 'merge':
        clean_ids = ','.join(str(int(i)) for i in ids if str(i).isdigit())
        return redirect(f"{reverse('product_merge')}?ids={clean_ids}")
    from decimal import Decimal
    try:
        value = Decimal(request.POST.get('value') or '0')
    except Exception:
        messages.error(request, "Noto'g'ri qiymat.")
        return redirect('product_list')

    products = Product.objects.filter(id__in=[int(i) for i in ids if str(i).isdigit()])
    n = 0
    affected_codes = []
    for p in products:
        affected_codes.append(p.code)
        if op == 'set_price':
            p.default_sale_price = value
            p.save(update_fields=['default_sale_price'])
            n += 1
        elif op == 'set_markup':
            p.markup_percent = value
            p.save(update_fields=['markup_percent'])
            n += 1
        elif op == 'multiply_price':
            new_price = (p.default_sale_price * (value / 100)).quantize(Decimal('1'))
            p.default_sale_price = new_price
            p.save(update_fields=['default_sale_price'])
            n += 1
        elif op == 'change_category':
            try:
                cat_id = int(value)
                cat = Category.objects.filter(pk=cat_id).first()
                if cat:
                    p.category = cat
                    p.save(update_fields=['category'])
                    n += 1
            except (ValueError, TypeError):
                cat = None
        elif op == 'delete':
            from django.db.models import ProtectedError
            try:
                p.delete()
                n += 1
            except ProtectedError:
                messages.warning(
                    request,
                    f"'{p.name}' ({p.code}) o'chirilmadi — sotuv tarixi bor.")

    # M9: audit the bulk operation as a single summary row
    if n > 0:
        OP_LABELS = {
            'set_price': f"narx={value}",
            'set_markup': f"markup={value}%",
            'multiply_price': f"narx×{value}%",
            'change_category': f"category={value}",
            'delete': "DELETE",
        }
        op_label = OP_LABELS.get(op, op)
        codes_preview = ', '.join(affected_codes[:10])
        if len(affected_codes) > 10:
            codes_preview += f", ... (+{len(affected_codes)-10})"
        AuditLog.objects.create(
            user=request.user,
            username_snapshot=request.user.username,
            action=(AuditLog.Action.DELETE if op == 'delete'
                    else AuditLog.Action.UPDATE),
            model_name='ProductBulk',
            object_repr=f"{n}×{op_label}: {codes_preview}"[:200],
        )

    messages.success(request, f"{n} ta mahsulot yangilandi/o'chirildi.")
    if op == 'change_category' and n > 0:
        try:
            _cid = int(value)
            _cat = Category.objects.filter(pk=_cid).first()
            if _cat:
                messages.success(
                    request,
                    f"{n} ta mahsulot \"{_cat.name}\" kategoriyasiga ko'chirildi.")
                return redirect(f"{reverse('product_list')}?category={_cid}")
        except (ValueError, TypeError):
            pass
    return redirect('product_list')


def product_list(request):
    """Mahsulotlar ro'yxati: qidiruv + filtrlar + sortable + 30 kunlik sotilganlik."""
    q = (request.GET.get('q') or '').strip()
    category_id = request.GET.get('category') or ''
    stock_filter = request.GET.get('stock') or ''  # zero|low|in_stock|''
    sort = request.GET.get('sort') or '-created_at'

    products = Product.objects.select_related('category')

    if q:
        # Subquery orqali — variant JOIN'lari annotatsiya Sum'larini
        # buzmasligi uchun avval mos mahsulot ID'larini topamiz
        _match_ids = Product.objects.filter(
            Q(code__icontains=q) | Q(name__icontains=q) |
            Q(external_barcode__icontains=q) |
            Q(variants__color__icontains=q) |
            Q(variants__size__icontains=q) |
            Q(variants__barcode__icontains=q)
        ).values('id')
        products = products.filter(id__in=_match_ids)
    if category_id:
        try:
            products = products.filter(category_id=int(category_id))
        except ValueError:
            category_id = ''

    # Annotate aggregate stock per product (+ narx diapazoni va real marja
    # uchun qiymatlar — bitta JOIN, qo'shimcha so'rovsiz)
    _val = ExpressionWrapper(
        F('variants__branch_stocks__stock_count') *
        F('variants__branch_stocks__sale_price'),
        output_field=DecimalField(max_digits=16, decimal_places=2))
    _cost = ExpressionWrapper(
        F('variants__branch_stocks__stock_count') *
        F('variants__branch_stocks__cost_price'),
        output_field=DecimalField(max_digits=16, decimal_places=2))
    products = products.annotate(
        total_stock=Coalesce(Sum('variants__branch_stocks__stock_count'), 0),
        variants_count=Count('variants', distinct=True),
        price_min=Min('variants__branch_stocks__sale_price'),
        price_max=Max('variants__branch_stocks__sale_price'),
        sale_val=Sum(_val),
        cost_val=Sum(_cost),
    )

    if stock_filter == 'zero':
        products = products.filter(total_stock=0)
    elif stock_filter == 'low':
        products = products.filter(total_stock__gt=0, total_stock__lte=3)
    elif stock_filter == 'in_stock':
        products = products.filter(total_stock__gt=0)

    # Sort
    allowed_sorts = {
        'name': 'name', '-name': '-name',
        'code': 'code', '-code': '-code',
        'stock': 'total_stock', '-stock': '-total_stock',
        'price': 'default_sale_price', '-price': '-default_sale_price',
        'created': 'created_at', '-created': '-created_at',
        '-created_at': '-created_at',
    }
    products = products.order_by(allowed_sorts.get(sort, '-created_at'))

    products = list(products[:200])
    _pids = [p.id for p in products]
    list_totals = {
        'variants': sum(p.variants_count for p in products),
        'units': sum(p.total_stock for p in products),
    }

    # 30/365 kunlik sotuvlar — faqat ko'rsatiladigan mahsulotlar bo'yicha
    since_30d = timezone.now() - timedelta(days=30)
    sold_map = {r['variant__product_id']: r['qty'] for r in (
        Sale.objects.filter(sold_at__gte=since_30d,
                            variant__product_id__in=_pids)
        .values('variant__product_id').annotate(qty=Sum('quantity')))}
    since_365d = timezone.now() - timedelta(days=365)
    sold_365_map = {r['variant__product_id']: r['qty'] for r in (
        Sale.objects.filter(sold_at__gte=since_365d,
                            variant__product_id__in=_pids)
        .values('variant__product_id').annotate(qty=Sum('quantity')))}

    # Har mahsulotning tur ranglari (nomi yonida ko'rsatish uchun)
    color_rows = (ProductVariant.objects
                  .filter(product__in=products)
                  .exclude(color='')
                  .order_by('color')
                  .values_list('product_id', 'color')
                  .distinct())
    colors_map = {}
    for pid, color in color_rows:
        colors_map.setdefault(pid, []).append(color)

    # Attach velocity + days_left + turnover
    for p in products:
        p.variant_colors = colors_map.get(p.id, [])
        sold = sold_map.get(p.id, 0)
        p.sold_30d = sold
        daily_avg = sold / 30 if sold else 0
        p.days_left = (p.total_stock / daily_avg) if daily_avg else None
        # Annual turnover ≈ annual_sold / avg_stock. Use current stock as proxy.
        annual_sold = sold_365_map.get(p.id, 0)
        if p.total_stock and p.total_stock > 0:
            p.turnover = annual_sold / p.total_stock
        else:
            p.turnover = None
        # Real marja — ombordagi haqiqiy tannarx/narxdan (og'irlikli):
        # marja% = (sotuv qiymati − tannarx qiymati) / tannarx qiymati
        try:
            cost_val = float(p.cost_val or 0)
            sale_val = float(p.sale_val or 0)
            if cost_val > 0:
                p.unit_profit = sale_val - cost_val
                p.margin_percent = (sale_val - cost_val) / cost_val * 100
            else:
                # fallback: eski markup asosidagi taxmin
                m = float(p.markup_percent or 0)
                if m > 0 and p.default_sale_price:
                    cost = float(p.default_sale_price) / (1 + m / 100)
                    p.unit_profit = float(p.default_sale_price) - cost
                    p.margin_percent = (p.unit_profit /
                                        float(p.default_sale_price) * 100)
                else:
                    p.unit_profit = 0
                    p.margin_percent = 0
        except Exception:
            p.unit_profit = 0
            p.margin_percent = 0

    categories = Category.objects.order_by('name')

    # Dublikat nomli mahsulotlar (birlashtirish tavsiyasi uchun)
    dup_groups = []
    _name_map = {}
    for pid, pname in Product.objects.values_list('id', 'name'):
        _name_map.setdefault(pname.strip().lower(), []).append(pid)
    for _n, _ids in _name_map.items():
        if len(_ids) > 1:
            dup_groups.append({'name': _n.title(), 'count': len(_ids),
                               'ids': ','.join(map(str, _ids))})
    dup_groups = dup_groups[:5]

    return render(request, 'inventory/product_list.html', {
        'products': products,
        'dup_groups': dup_groups,
        'list_totals': list_totals,
        'q': q,
        'category_id': category_id,
        'stock_filter': stock_filter,
        'sort': sort,
        'categories': categories,
    })


@admin_required
def product_create(request):
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if form.is_valid():
            _name = smart_title(form.cleaned_data.get('name'))
            product = form.save(commit=False)
            product.name = _name
            product.save()
            # Bir xil nomli boshqa mahsulot bo'lsa — jim birlashtirmaymiz,
            # faqat eslatib qo'yamiz (xohlasa qo'lda birlashtiradi)
            dup = (Product.objects.filter(name__iexact=_name)
                   .exclude(pk=product.pk).first())
            if dup:
                messages.info(
                    request,
                    f"Eslatma: '{_name}' nomli boshqa mahsulot ham bor "
                    f"({dup.code}). Agar bir xil bo'lsa, Mahsulotlar "
                    f"ro'yxatidan birlashtirishingiz mumkin.")
            messages.success(request, f"Mahsulot yaratildi. Kod: {product.code}")
            return redirect('product_detail', code=product.code)
    else:
        # URL prefill: /products/new/?barcode=4607034000234&name=Nike
        initial = {}
        if request.GET.get('barcode'):
            initial['external_barcode'] = request.GET.get('barcode').strip()
        if request.GET.get('name'):
            initial['name'] = request.GET.get('name').strip()
        form = ProductForm(initial=initial)
    return render(request, 'inventory/product_form.html', {
        'form': form, 'title': "Yangi mahsulot qo'shish",
    })


@admin_required
def product_delete(request, code):
    """Mahsulotni o'chirish (POST). Sotuv tarixi bo'lsa DB himoya qiladi
    (PROTECT) — aniq xabar bilan rad etiladi."""
    from django.db.models import ProtectedError
    if request.method != 'POST':
        return redirect('product_list')
    product = get_object_or_404(Product, code=normalize_code(code))
    name, pk = product.name, product.pk
    try:
        product.delete()
    except ProtectedError:
        messages.error(
            request,
            f"'{name}' o'chirilmadi: sotuv/transfer tarixi bor. Tarix "
            f"saqlanishi shart — o'rniga zaxirani 0 ga tushiring yoki "
            f"boshqa mahsulotga birlashtiring.")
        return redirect('product_list')
    AuditLog.objects.create(
        user=request.user,
        username_snapshot=request.user.username,
        action=AuditLog.Action.DELETE,
        model_name='Product',
        object_id=str(pk),
        object_repr=f'{code} — {name}',
    )
    messages.success(request, f"O'chirildi: {name} ({code}).")
    return redirect('product_list')


@admin_required
def variant_delete(request, pk):
    """Turni (rang/o'lcham) o'chirish. Sotuv/transfer/inventarizatsiya
    tarixi bo'lsa DB himoyasi (PROTECT) — aniq xabar bilan rad etiladi."""
    from django.db.models import ProtectedError
    v = get_object_or_404(
        ProductVariant.objects.select_related('product'), pk=pk)
    code = v.product.code
    branch_id = request.POST.get('branch') or ''
    label = f"{v.size or '—'} / {v.color or '—'}"
    back = f"{reverse('product_variants_edit', args=[code])}"
    if branch_id:
        back += f"?branch={branch_id}"
    if request.method != 'POST':
        return redirect(back)
    try:
        v.delete()
    except ProtectedError:
        messages.error(
            request,
            f"{label} o'chirilmadi: sotuv/transfer/inventarizatsiya tarixi "
            f"bor. Tarix saqlanishi shart — o'rniga omborni 0 qiling.")
        return redirect(back)
    AuditLog.objects.create(
        user=request.user,
        username_snapshot=request.user.username,
        action=AuditLog.Action.DELETE,
        model_name='ProductVariant', object_id=str(pk),
        object_repr=f'{code}: {label}')
    messages.success(request, f"O'chirildi: {label}")
    return redirect(back)


@admin_required
def product_variants_move(request, code):
    """Tanlangan turlarni boshqa mahsulotga ko'chirish."""
    product = get_object_or_404(Product, code=normalize_code(code))
    back = reverse('product_variants_edit', args=[product.code])
    if request.method != 'POST':
        return redirect(back)
    target_code = normalize_code(
        (request.POST.get('target_code') or '').strip().upper())
    target = Product.objects.filter(code=target_code).first()
    ids = [int(i) for i in request.POST.getlist('mv') if str(i).isdigit()]
    variants = list(product.variants.filter(pk__in=ids))
    if not variants:
        messages.warning(request, "Ko'chirish uchun tur tanlanmagan.")
        return redirect(back)
    if not target:
        messages.error(request, "Maqsad mahsulot topilmadi — ro'yxatdan tanlang.")
        return redirect(back)
    if target.pk == product.pk:
        messages.warning(request, "Bir xil mahsulot tanlangan.")
        return redirect(back)
    moved, skipped = 0, []
    with transaction.atomic():
        for v in variants:
            if target.variants.filter(size=v.size, color=v.color).exists():
                skipped.append(f"{v.size or '—'}/{v.color or '—'}")
                continue
            v.product = target
            v.save(update_fields=['product'])
            moved += 1
        if moved:
            AuditLog.objects.create(
                user=request.user,
                username_snapshot=request.user.username,
                action=AuditLog.Action.UPDATE,
                model_name='ProductVariant', object_id=str(product.pk),
                object_repr=(f"{moved} ta tur {product.code} -> "
                             f"{target.code}")[:300])
    if moved:
        messages.success(
            request,
            f"{moved} ta tur '{target.name}' ({target.code}) ga ko'chirildi.")
    if skipped:
        messages.warning(
            request,
            f"O'tkazilmadi (maqsadda shu o'lcham/rang bor): {', '.join(skipped)}")
    return redirect(back)


@admin_required
def product_merge(request):
    """Bir nechta mahsulotni bittasiga birlashtirish.

    Tanlanganlarning variantlari, ombor qoldiqlari, sotuv/qabul/transfer
    tarixi asosiy (target) mahsulotga ko'chadi. Manba mahsulotning
    external_barcode'i (agar bitta varianti bo'lsa) o'sha variantning
    shtrix-kodiga aylanadi — skanerlash ishlashda davom etadi.
    """
    ids_raw = (request.GET.get('ids') or request.POST.get('ids') or '')
    ids = [int(i) for i in ids_raw.split(',') if i.strip().isdigit()]
    products = list(Product.objects.filter(id__in=ids)
                    .prefetch_related('variants'))
    if len(products) < 2:
        messages.warning(request, "Birlashtirish uchun kamida 2 ta mahsulot tanlang.")
        return redirect('product_list')

    # Variantlari eng ko'p mahsulot — default target
    products.sort(key=lambda p: (-p.variants.count(), p.created_at))
    default_target = products[0]

    if request.method == 'POST' and request.POST.get('confirm') == '1':
        try:
            target = next(p for p in products
                          if str(p.pk) == request.POST.get('target'))
        except StopIteration:
            messages.error(request, "Asosiy mahsulot noto'g'ri tanlangan.")
            return redirect('product_list')
        sources = [p for p in products if p.pk != target.pk]

        moved_variants = 0
        with transaction.atomic():
            for src in sources:
                src_variants = list(src.variants.all())
                single = len(src_variants) == 1
                for v in src_variants:
                    tv = target.variants.filter(
                        size=v.size, color=v.color).first()
                    if tv:
                        # Bir xil tur — variant darajasida birlashtiramiz
                        for bs in list(v.branch_stocks.all()):
                            tbs = BranchStock.objects.filter(
                                variant=tv, branch=bs.branch).first()
                            if tbs:
                                tbs.stock_count += bs.stock_count
                                if not tbs.sale_price:
                                    tbs.sale_price = bs.sale_price
                                if not tbs.cost_price:
                                    tbs.cost_price = bs.cost_price
                                if not tbs.wholesale_price:
                                    tbs.wholesale_price = bs.wholesale_price
                                tbs.save()
                                bs.delete()
                            else:
                                bs.variant = tv
                                bs.save(update_fields=['variant'])
                        Sale.objects.filter(variant=v).update(variant=tv)
                        Intake.objects.filter(variant=v).update(variant=tv)
                        TransferLine.objects.filter(variant=v).update(variant=tv)
                        StocktakeCount.objects.filter(variant=v).update(variant=tv)
                        if v.barcode and not tv.barcode:
                            bc = v.barcode
                            v.barcode = None
                            v.save(update_fields=['barcode'])
                            tv.barcode = bc
                            tv.save(update_fields=['barcode'])
                        v.delete()
                    else:
                        v.product = target
                        if (single and src.external_barcode and not v.barcode
                                and not ProductVariant.objects.filter(
                                    barcode=src.external_barcode).exists()):
                            v.barcode = src.external_barcode
                        v.save()
                    moved_variants += 1
                if target.external_barcode is None and src.external_barcode \
                        and not single:
                    # ko'p variantli manba barcode'i variantga bog'lanmadi —
                    # yo'qolmasligi uchun targetga o'tkazamiz
                    bc = src.external_barcode
                    src.external_barcode = None
                    src.save(update_fields=['external_barcode'])
                    target.external_barcode = bc
                    target.save(update_fields=['external_barcode'])
                src.delete()
            AuditLog.objects.create(
                user=request.user,
                username_snapshot=request.user.username,
                action=AuditLog.Action.UPDATE,
                model_name='Product',
                object_id=str(target.pk),
                object_repr=(f"Birlashtirildi -> {target.code}: "
                             + ', '.join(x.code for x in sources))[:300],
            )
        messages.success(
            request,
            f"{len(sources)} ta mahsulot '{target.name}' ({target.code}) ga "
            f"birlashtirildi — {moved_variants} ta tur ko'chdi.")
        return redirect('product_detail', code=target.code)

    # Confirm sahifasi
    rows = []
    for p in products:
        rows.append({
            'product': p,
            'variants_count': p.variants.count(),
            'stock': p.total_stock(),
        })
    return render(request, 'inventory/product_merge.html', {
        'rows': rows,
        'ids': ','.join(str(p.pk) for p in products),
        'default_target': default_target,
    })


@admin_required
def product_search_suggest(request):
    """Mahsulotlar sahifasi qidiruvi uchun jonli takliflar:
    mahsulot nomi/kodi, rang, o'lcham, shtrix-kod."""
    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    res = []
    for pr in (Product.objects
               .filter(Q(name__icontains=q) | Q(code__icontains=q.upper()))
               .order_by('name')[:6]):
        res.append({'t': 'mahsulot', 'label': pr.name, 'sub': pr.code,
                    'q': pr.name})
    for cl in (ProductVariant.objects.filter(color__icontains=q)
               .values_list('color', flat=True).distinct()
               .order_by('color')[:4]):
        res.append({'t': 'rang', 'label': cl, 'sub': '', 'q': cl})
    for sz in (ProductVariant.objects.filter(size__icontains=q)
               .values_list('size', flat=True).distinct()
               .order_by('size')[:3]):
        res.append({'t': "o'lcham", 'label': sz, 'sub': '', 'q': sz})
    for bc in (ProductVariant.objects.filter(barcode__istartswith=q)
               .values_list('barcode', flat=True)[:2]):
        res.append({'t': 'shtrix', 'label': bc, 'sub': '', 'q': bc})
    return JsonResponse({'results': res[:12]})


@admin_required
def product_search_for_attach(request):
    """GET /products/search-for-attach/?q=&exclude_with_barcode=1
    Lightweight JSON product search for the "attach EAN to existing
    product" picker. Returns up to 12 matches by name or code."""
    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({'results': []})
    # Avval nomi shu harf(lar) bilan BOSHLANADIGANLAR, keyin ichida
    # uchraydiganlar — "F" yozilganda Fructis birinchi chiqadi
    base = Product.objects.all()
    if request.GET.get('exclude_with_barcode') == '1':
        base = base.filter(external_barcode__isnull=True)
    starts = list(base.filter(name__istartswith=q).order_by('name')[:12])
    qs = starts
    if len(qs) < 12 and len(q) >= 2:
        extra = (base.filter(Q(name__icontains=q) | Q(code__icontains=q.upper()))
                 .exclude(pk__in=[x.pk for x in starts])
                 .order_by('name')[:12 - len(qs)])
        qs = qs + list(extra)
    return JsonResponse({'results': [
        {
            'code': p.code,
            'name': p.name,
            'external_barcode': p.external_barcode or '',
            'category': p.category.name if p.category else '',
        } for p in qs
    ]})


@admin_required
def product_attach_barcode(request):
    """POST /products/attach-barcode/  body: code, barcode
    Attach an external_barcode to an existing product. Refuses if the
    barcode is already attached elsewhere (the unique constraint would
    raise IntegrityError; we want a clean JSON error)."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
    code = normalize_code((request.POST.get('code') or '').upper())
    barcode = (request.POST.get('barcode') or '').strip()
    if not code or not barcode:
        return JsonResponse({'ok': False, 'error': 'code va barcode kerak'},
                            status=400)
    try:
        product = Product.objects.get(code=code)
    except Product.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Mahsulot topilmadi'},
                            status=404)
    clash = (Product.objects
             .filter(external_barcode=barcode)
             .exclude(pk=product.pk)
             .first())
    if clash:
        return JsonResponse({
            'ok': False,
            'error': f"Bu barcode '{clash.name}' ({clash.code}) ga "
                     f"allaqachon biriktirilgan.",
        }, status=409)
    product.external_barcode = barcode
    product.save(update_fields=['external_barcode'])
    return JsonResponse({
        'ok': True,
        'product': {'code': product.code, 'name': product.name,
                    'external_barcode': product.external_barcode},
    })


@admin_required
def product_detail(request, code):
    product = get_object_or_404(Product, code=normalize_code(code))
    variants = list(product.variants.all())
    sizes = sorted({v.size for v in variants}, key=lambda s: (len(s), s))
    colors = sorted({v.color for v in variants})

    # Bitta so'rov — barcha filial zaxiralari (filial boshiga alohida
    # so'rov o'rniga; 20 filialda 20 -> 1 query)
    all_stocks = list(BranchStock.objects.filter(variant__product=product)
                      .select_related('variant', 'branch'))
    stocks_by_branch = {}
    for st in all_stocks:
        stocks_by_branch.setdefault(st.branch_id, []).append(st)

    branches_data = []
    for br in Branch.objects.filter(is_active=True):
        stocks = stocks_by_branch.get(br.id, [])
        matrix = {(s.variant.size, s.variant.color): s for s in stocks}
        total = sum(s.stock_count for s in stocks)
        branches_data.append({
            'branch': br, 'matrix': matrix,
            'sizes': sizes, 'colors': colors, 'total': total,
        })

    # Yassi turlar ro'yxati (shtrix-kod + narx + jami ombor)
    stocks_by_variant = {}
    for st in all_stocks:
        stocks_by_variant.setdefault(st.variant_id, []).append(st)
    variant_rows = []
    for v in variants:
        sts = stocks_by_variant.get(v.pk, [])
        prices = [st.sale_price for st in sts if st.sale_price]
        variant_rows.append({
            'variant': v,
            'stock_total': sum(st.stock_count for st in sts),
            'price_min': min(prices) if prices else None,
            'price_max': max(prices) if prices else None,
        })
    _all_prices = [st.sale_price for st in all_stocks if st.sale_price]
    price_min = min(_all_prices) if _all_prices else None
    price_max = max(_all_prices) if _all_prices else None

    recent_intakes = Intake.objects.filter(variant__product=product) \
        .select_related('variant', 'branch', 'received_by').order_by('-received_at')[:20]

    # 30-day sales chart + KPIs
    since_30d = timezone.now() - timedelta(days=30)
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    sales_30d = Sale.objects.filter(variant__product=product, sold_at__gte=since_30d)
    agg = sales_30d.aggregate(
        qty=Sum('quantity'),
        rev=Sum(rev_expr),
        txns=Count('transaction', distinct=True),
    )
    sold_30d = agg['qty'] or 0
    rev_30d = float(agg['rev'] or 0)
    txns_30d = agg['txns'] or 0
    daily_avg = sold_30d / 30 if sold_30d else 0
    # Single aggregate covers both total stock + total inventory value (was 2 separate queries)
    inv_agg = BranchStock.objects.filter(variant__product=product).aggregate(
        s=Sum('stock_count'),
        v=Sum(ExpressionWrapper(F('stock_count') * F('cost_price'),
                                output_field=DecimalField(max_digits=14, decimal_places=2))),
    )
    total_stock = inv_agg['s'] or 0
    total_value = float(inv_agg['v'] or 0)
    days_left = (total_stock / daily_avg) if daily_avg else None

    # Daily chart data
    from collections import OrderedDict
    chart = OrderedDict()
    today = timezone.localdate()
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        chart[d.isoformat()] = 0
    daily_rows = (sales_30d.values_list('sold_at', 'quantity'))
    for sold_at, qty in daily_rows:
        d = sold_at.astimezone(timezone.get_current_timezone()).date()
        key = d.isoformat()
        if key in chart:
            chart[key] += qty
    chart_labels = [d[5:] for d in chart.keys()]  # MM-DD
    chart_qty = list(chart.values())

    # Top branch for this product (by 30-day revenue)
    top_branch_row = (sales_30d.values('branch__name')
                      .annotate(r=Sum(rev_expr))
                      .order_by('-r').first())
    top_branch_name = top_branch_row['branch__name'] if top_branch_row else None
    top_branch_rev = float(top_branch_row['r'] or 0) if top_branch_row else 0

    # Variants summary
    variants_qs = product.variants.all()
    total_variants = variants_qs.count()
    variants_in_stock = (BranchStock.objects
                         .filter(variant__product=product, stock_count__gt=0)
                         .values('variant').distinct().count())

    product_kpis = {
        'total_stock': total_stock,
        'total_value': total_value,
        'sold_30d': sold_30d,
        'rev_30d': rev_30d,
        'txns_30d': txns_30d,
        'days_left': days_left,
        'top_branch_name': top_branch_name,
        'top_branch_rev': top_branch_rev,
        'total_variants': total_variants,
        'variants_in_stock': variants_in_stock,
    }

    # D5: Cross-sell with confidence + lift (association rules)
    # Confidence P(B|A) = co_count / |A|   — how often B appears when A buys
    # Lift = P(B|A) / P(B)                  — strength of association (1.0 = independent)
    # Sort by lift to surface unusually-strong pairings, not just popular pairs.
    total_txns = SaleTransaction.objects.count() or 1
    txn_ids = list(Sale.objects.filter(variant__product=product)
                   .values_list('transaction_id', flat=True).distinct())
    a_count = len(txn_ids)
    cross_sell = []
    if a_count > 0:
        co_rows = (Sale.objects
                   .filter(transaction_id__in=txn_ids)
                   .exclude(variant__product=product)
                   .values('variant__product_id',
                           'variant__product__code',
                           'variant__product__name')
                   .annotate(co_count=Count('transaction', distinct=True))
                   .order_by('-co_count')[:30])  # candidate pool
        # B-only base counts (how often each candidate B appears overall)
        b_ids = [r['variant__product_id'] for r in co_rows]
        b_counts = {r['variant__product_id']: r['c'] for r in (
            Sale.objects.filter(variant__product_id__in=b_ids)
            .values('variant__product_id')
            .annotate(c=Count('transaction', distinct=True))
        )}
        for r in co_rows:
            pid = r['variant__product_id']
            co = r['co_count']
            b = b_counts.get(pid, 0) or 1
            confidence = co / a_count
            lift = confidence / (b / total_txns) if (b / total_txns) > 0 else 0
            cross_sell.append({
                'code': r['variant__product__code'],
                'name': r['variant__product__name'],
                'co_count': co,
                'confidence': round(confidence * 100, 1),
                'lift': round(lift, 2),
            })
        # Sort by lift (descending), keep min 3 co-occurrences to avoid noise
        cross_sell = [c for c in cross_sell if c['co_count'] >= 2]
        cross_sell.sort(key=lambda c: -c['lift'])
        cross_sell = cross_sell[:5]

    return render(request, 'inventory/product_detail.html', {
        'product': product, 'branches_data': branches_data,
        'recent_intakes': recent_intakes,
        'product_kpis': product_kpis,
        'variant_rows': variant_rows,
        'price_min': price_min,
        'price_max': price_max,
        'chart_labels': chart_labels,
        'chart_qty': chart_qty,
        'cross_sell': cross_sell,
    })


@admin_required
def product_edit(request, code):
    product = get_object_or_404(Product, code=normalize_code(code))
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, 'Mahsulot yangilandi.')
            return redirect('product_detail', code=product.code)
    else:
        form = ProductForm(instance=product)
    return render(request, 'inventory/product_form.html', {
        'form': form, 'title': f'Tahrirlash — {product.name}', 'product': product,
    })


# ---------- INTAKE ----------

def _split_csv(text):
    """Split a comma- or newline-separated string into trimmed tokens."""
    if not text:
        return []
    parts = []
    for chunk in text.replace('\n', ',').split(','):
        c = chunk.strip()
        if c and c not in parts:
            parts.append(c)
    return parts


@admin_required
def intake_for_product(request, code):
    """Bulk intake: matrix of (size × color) → counts in one form."""
    from decimal import Decimal
    product = get_object_or_404(Product, code=normalize_code(code))

    if request.method == 'POST':
        try:
            branch_id = int(request.POST.get('branch') or 0)
            branch = Branch.objects.filter(pk=branch_id, is_active=True).first()
            if not branch:
                messages.error(request, "Filial tanlang.")
                return redirect('intake_for_product', code=product.code)

            cost = Decimal(request.POST.get('cost_per_unit') or '0')
            markup = Decimal(request.POST.get('markup_percent') or '0')
            sale_price_raw = request.POST.get('sale_price') or ''
            if sale_price_raw.strip():
                sale_price = Decimal(sale_price_raw)
            else:
                sale_price = (cost * (1 + markup / 100)).quantize(Decimal('1'))
            wholesale_price = Decimal(request.POST.get('wholesale_price') or '0')

            supplier = (request.POST.get('supplier') or '').strip()
            note = (request.POST.get('note') or '').strip()
            update_price = bool(request.POST.get('update_product_price'))

            # Iterate matrix cells. Inputs are named qty[<size>|<color>].
            total_qty = 0
            variants_touched = 0
            with transaction.atomic():
                for key, value in request.POST.items():
                    if not key.startswith('qty[') or not key.endswith(']'):
                        continue
                    payload = key[4:-1]
                    if '|' not in payload:
                        continue
                    size, color = payload.split('|', 1)
                    size = size.strip()
                    color = color.strip()
                    if not size or not color:
                        continue
                    try:
                        qty = int(value)
                    except (ValueError, TypeError):
                        continue
                    if qty <= 0:
                        continue
                    variant, _ = ProductVariant.objects.get_or_create(
                        product=product, size=size, color=color,
                    )
                    stock, _ = BranchStock.objects.get_or_create(
                        variant=variant, branch=branch,
                        defaults={'cost_price': cost, 'sale_price': sale_price,
                                  'wholesale_price': wholesale_price},
                    )
                    stock.stock_count = F('stock_count') + qty
                    stock.cost_price = cost
                    stock.sale_price = sale_price
                    if wholesale_price > 0:
                        stock.wholesale_price = wholesale_price
                    stock.save()
                    Intake.objects.create(
                        variant=variant, branch=branch,
                        quantity=qty, cost_per_unit=cost,
                        supplier=supplier, note=note,
                        received_by=request.user,
                    )
                    total_qty += qty
                    variants_touched += 1

                if update_price and sale_price > 0:
                    product.default_sale_price = sale_price
                    product.markup_percent = markup
                    product.save()
                    BranchStock.objects.filter(
                        variant__product=product, sale_price=0
                    ).update(sale_price=sale_price)

            if variants_touched == 0:
                messages.warning(request,
                    "Hech qaysi katakka son kiritilmadi — saqlanmadi.")
            else:
                messages.success(request,
                    f"Qabul saqlandi: {branch.name}ga {variants_touched} ta "
                    f"variant uchun jami {total_qty} dona.")
                return redirect('product_detail', code=product.code)
        except (ValueError, TypeError) as e:
            messages.error(request, f"Maydonlarni tekshiring: {e}")

    # Cost history: last 5 intakes for this product
    last_intakes = list(Intake.objects.filter(variant__product=product)
                        .select_related('variant', 'branch', 'supplier_ref')
                        .order_by('-received_at')[:5])

    # Pre-fill sizes/colors from existing variants if present
    variants = list(product.variants.all())
    existing_sizes = sorted({v.size for v in variants}, key=lambda s: (len(s), s))
    existing_colors = sorted({v.color for v in variants})

    return render(request, 'inventory/intake_form.html', {
        'product': product,
        'branches': Branch.objects.filter(is_active=True),
        'existing_sizes': existing_sizes,
        'existing_colors': existing_colors,
        'last_intakes': last_intakes,
        'suppliers': Supplier.objects.filter(is_active=True).order_by('name'),
    })


@admin_required
def product_variants_edit(request, code):
    """Mahsulot turlarini jadvalda tahrirlash — hamma maydon bir joyda.

    Variant maydonlari (rang, o'lcham, shtrix-kod) filialdan mustaqil;
    narxlar va ombor soni tanlangan filial bo'yicha (BranchStock).
    Ombor soni o'zgartirilsa — farq Intake yozuvi sifatida saqlanadi
    (qo'lda tuzatish) va AuditLog'ga tushadi.
    """
    from decimal import Decimal, InvalidOperation

    product = get_object_or_404(Product, code=normalize_code(code))
    branches = Branch.objects.filter(is_active=True)
    try:
        branch = branches.get(pk=int(request.GET.get('branch')
                                     or request.POST.get('branch') or 0))
    except (Branch.DoesNotExist, ValueError, TypeError):
        branch = branches.first()
    if branch is None:
        messages.error(request, "Faol filial yo'q.")
        return redirect('product_detail', code=product.code)

    _dec = parse_dec

    if request.method == 'POST':
        errors = []
        ids = request.POST.getlist('v_id')
        colors = request.POST.getlist('v_color')
        sizes = request.POST.getlist('v_size')
        barcodes = request.POST.getlist('v_barcode')
        costs = request.POST.getlist('v_cost')
        sales = request.POST.getlist('v_sale')
        wholesales = request.POST.getlist('v_wholesale')
        stocks = request.POST.getlist('v_stock')

        variants = {v.pk: v for v in product.variants.all()}
        rows = []
        seen_pairs, seen_barcodes = set(), set()
        for i in range(len(ids)):
            get = lambda lst: (lst[i] if i < len(lst) else '') or ''
            raw_id = (ids[i] or '').strip()
            color = smart_title(get(colors))
            size = smart_title(get(sizes))
            barcode = get(barcodes).strip() or None
            if raw_id:
                try:
                    variant = variants[int(raw_id)]
                except (ValueError, KeyError):
                    continue
            else:
                # Yangi tur qatori — bo'sh bo'lsa jim o'tkazib yuboramiz
                if not (color or size or barcode):
                    continue
                variant = None
            try:
                cost = _dec(get(costs))
                sale = _dec(get(sales))
                wholesale = _dec(get(wholesales))
                stock_new = int(_dec(get(stocks)))
            except (InvalidOperation, ValueError, TypeError):
                errors.append(f"{i + 1}-qator: raqam maydonlari noto'g'ri.")
                continue
            if min(cost, sale, wholesale) < 0 or stock_new < 0:
                errors.append(f"{i + 1}-qator: manfiy qiymat kiritilmaydi.")
                continue
            pair = (size, color)
            if pair in seen_pairs:
                errors.append(
                    f"{i + 1}-qator: {size or '—'} / {color or '—'} takrorlangan.")
                continue
            seen_pairs.add(pair)
            if barcode:
                if barcode in seen_barcodes:
                    errors.append(f"{i + 1}-qator: shtrix-kod jadvalda takror.")
                    continue
                seen_barcodes.add(barcode)
                v_clash = ProductVariant.objects.filter(barcode=barcode)
                if variant is not None:
                    v_clash = v_clash.exclude(pk=variant.pk)
                v_clash = v_clash.select_related('product').first()
                if v_clash:
                    errors.append(
                        f"Shtrix-kod {barcode} band: {v_clash.product.name} "
                        f"({v_clash.size or '—'}/{v_clash.color or '—'}).")
                p_clash = Product.objects.filter(
                    external_barcode=barcode).first()
                if p_clash:
                    errors.append(
                        f"Shtrix-kod {barcode} '{p_clash.name}' "
                        f"mahsulotiga biriktirilgan.")
            rows.append({'variant': variant, 'color': color, 'size': size,
                         'barcode': barcode, 'cost': cost, 'sale': sale,
                         'wholesale': wholesale, 'stock': stock_new})

        # DB darajasida ham juftlik boshqa variant bilan to'qnashmasin
        _form_pks = {x['variant'].pk for x in rows if x['variant'] is not None}
        for r in rows:
            clash_qs = product.variants.filter(size=r['size'], color=r['color'])
            if r['variant'] is not None:
                clash_qs = clash_qs.exclude(pk=r['variant'].pk)
            clash = clash_qs.first()
            if clash and clash.pk not in _form_pks:
                errors.append(
                    f"{r['size'] or '—'} / {r['color'] or '—'} allaqachon mavjud.")

        if errors:
            for e in errors[:8]:
                messages.error(request, e)
        else:
            changed = []
            with transaction.atomic():
                for r in rows:
                    v = r['variant']
                    if v is None:
                        v = ProductVariant.objects.create(
                            product=product, size=r['size'],
                            color=r['color'], barcode=r['barcode'])
                        stock = BranchStock.objects.create(
                            variant=v, branch=branch,
                            cost_price=r['cost'], sale_price=r['sale'],
                            wholesale_price=r['wholesale'],
                            stock_count=r['stock'])
                        if r['stock'] > 0:
                            Intake.objects.create(
                                variant=v, branch=branch,
                                quantity=r['stock'],
                                cost_per_unit=r['cost'],
                                note="Yangi tur (tahrirlash sahifasidan)",
                                received_by=request.user)
                        changed.append(
                            f"+ yangi: {v.size or '—'}/{v.color or '—'}")
                        continue
                    v_fields = []
                    if v.color != r['color']:
                        v_fields.append(f"rang {v.color}→{r['color']}")
                        v.color = r['color']
                    if v.size != r['size']:
                        v_fields.append(f"o'lcham {v.size}→{r['size']}")
                        v.size = r['size']
                    if (v.barcode or None) != r['barcode']:
                        v_fields.append(f"shtrix {v.barcode or '—'}→{r['barcode'] or '—'}")
                        v.barcode = r['barcode']
                    if v_fields:
                        v.save()
                    stock, _ = BranchStock.objects.select_for_update().get_or_create(
                        variant=v, branch=branch)
                    s_fields = []
                    if stock.cost_price != r['cost']:
                        s_fields.append(f"tannarx {stock.cost_price}→{r['cost']}")
                        stock.cost_price = r['cost']
                    if stock.sale_price != r['sale']:
                        s_fields.append(f"narx {stock.sale_price}→{r['sale']}")
                        stock.sale_price = r['sale']
                    if stock.wholesale_price != r['wholesale']:
                        s_fields.append(
                            f"ulgurji {stock.wholesale_price}→{r['wholesale']}")
                        stock.wholesale_price = r['wholesale']
                    delta = r['stock'] - stock.stock_count
                    if delta:
                        s_fields.append(
                            f"ombor {stock.stock_count}→{r['stock']}")
                        stock.stock_count = r['stock']
                        Intake.objects.create(
                            variant=v, branch=branch, quantity=delta,
                            cost_per_unit=stock.cost_price,
                            note="Qo'lda tuzatish (turlarni tahrirlash)",
                            received_by=request.user)
                    if s_fields:
                        stock.save()
                    if v_fields or s_fields:
                        changed.append(
                            f"{v.size or '—'}/{v.color or '—'}: "
                            + ', '.join(v_fields + s_fields))
                if changed:
                    AuditLog.objects.create(
                        user=request.user,
                        username_snapshot=request.user.username,
                        action=AuditLog.Action.UPDATE,
                        model_name='ProductVariant',
                        object_id=str(product.pk),
                        object_repr=(f"{product.code} ({branch.name}): "
                                     + ' | '.join(changed))[:300],
                    )
            if changed:
                messages.success(
                    request, f"Saqlandi: {len(changed)} ta tur yangilandi "
                             f"({branch.name}).")
            else:
                messages.info(request, "O'zgarish yo'q.")
            return redirect('product_detail', code=product.code)

    # GET (yoki xatodan keyin) — joriy holatni chizamiz
    stocks = {st.variant_id: st for st in BranchStock.objects.filter(
        variant__product=product, branch=branch)}
    rows = []
    for v in product.variants.all():
        st = stocks.get(v.pk)
        rows.append({
            'variant': v,
            'cost': st.cost_price if st else '',
            'sale': st.sale_price if st else '',
            'wholesale': st.wholesale_price if st else '',
            'stock': st.stock_count if st else 0,
        })
    return render(request, 'inventory/product_variants_edit.html', {
        'product': product, 'branch': branch, 'branches': branches,
        'rows': rows,
    })


@admin_required
def clothes_intake(request):
    """Kiyim/poyabzal qabul — zavod kodisiz tovarlar. O'lcham x rang
    jadvali, har kombinatsiya uchun avtomatik EAN-13 kod yaratiladi,
    keyin termal etiketka chop etiladi."""
    from decimal import Decimal, InvalidOperation
    categories = Category.objects.order_by('name')
    branches = Branch.objects.filter(is_active=True)

    if request.method == 'POST':
        errors = []
        product = None
        product_code = (request.POST.get('product_code') or '').strip()
        new_name = smart_title(request.POST.get('new_name'))
        if product_code:
            product = Product.objects.filter(
                code=normalize_code(product_code.upper())).first()
            if not product:
                errors.append(f"Mahsulot topilmadi: {product_code}")
        elif not new_name:
            errors.append("Mahsulot nomini kiriting yoki tanlang.")

        branch = Branch.objects.filter(
            pk=request.POST.get('branch') or 0, is_active=True).first()
        if not branch:
            errors.append("Filial tanlang.")

        try:
            price = parse_dec(request.POST.get('price'))
            marja = parse_dec(request.POST.get('marja'))
        except (InvalidOperation, ValueError):
            price = Decimal('0'); marja = Decimal('0')
        if price <= 0:
            errors.append("Sotuv narxini kiriting.")
        if marja < 0:
            marja = Decimal('0')
        cost = (price / (Decimal('1') + marja / Decimal('100'))
                ).quantize(Decimal('0.01')) if price > 0 else Decimal('0')

        # Kataklar: qty[<size>|<color>]
        cells = []
        for key, val in request.POST.items():
            if not (key.startswith('qty[') and key.endswith(']')):
                continue
            payload = key[4:-1]
            if '|' not in payload:
                continue
            size, color = payload.split('|', 1)
            size = smart_title(size); color = smart_title(color)
            try:
                qty = int(parse_dec(val))
            except (InvalidOperation, ValueError, TypeError):
                qty = 0
            if qty > 0 and (size or color):
                cells.append((size, color, qty))
        if not cells and not errors:
            errors.append("Kamida bitta o'lcham/rang katagiga son kiriting.")

        if errors:
            for e in errors[:8]:
                messages.error(request, e)
        else:
            created_ids = []
            total_qty = 0
            with transaction.atomic():
                if product is None:
                    category = Category.objects.filter(
                        pk=request.POST.get('category') or 0).first()
                    product = Product.objects.create(
                        name=new_name, category=category,
                        default_sale_price=price)
                session = IntakeSession.objects.create(
                    branch=branch, received_by=request.user,
                    note="Kiyim/poyabzal qabul (avto-kod)")
                for size, color, qty in cells:
                    variant, _ = ProductVariant.objects.get_or_create(
                        product=product, size=size, color=color)
                    if not variant.barcode:
                        code = gen_internal_ean13(variant.pk)
                        # kolliziya bo'lsa (kamdan-kam) — pk+offset
                        n = 0
                        while ProductVariant.objects.filter(
                                barcode=code).exclude(pk=variant.pk).exists():
                            n += 1
                            code = gen_internal_ean13(variant.pk + n * 100000)
                        variant.barcode = code
                        variant.save(update_fields=['barcode'])
                    stock, _ = BranchStock.objects.get_or_create(
                        variant=variant, branch=branch,
                        defaults={'cost_price': cost, 'sale_price': price})
                    stock.cost_price = cost
                    stock.sale_price = price
                    stock.stock_count = F('stock_count') + qty
                    stock.save()
                    Intake.objects.create(
                        session=session, variant=variant, branch=branch,
                        quantity=qty, cost_per_unit=cost,
                        note="Kiyim qabul", received_by=request.user)
                    created_ids.append(variant.pk)
                    total_qty += qty
            messages.success(
                request,
                f"{product.name}: {len(cells)} tur, {total_qty} dona qabul "
                f"qilindi. Endi etiketkalarni chop eting.")
            ids = ','.join(str(i) for i in created_ids)
            return redirect(f"{reverse('variant_labels')}?ids={ids}&price={price}")

    colors_all = (ProductVariant.objects.exclude(color='')
                  .values_list('color', flat=True).distinct().order_by('color')[:500])
    return render(request, 'inventory/clothes_intake.html', {
        'categories': categories, 'branches': branches,
        'colors_all': colors_all,
    })


@admin_required
def variant_labels(request):
    """Termal etiketka: EAN-13 barcode + QR + kod + narx (variantlar)."""
    import base64
    from barcode import EAN13
    from barcode.writer import SVGWriter

    ids = [int(i) for i in (request.GET.get('ids') or '').split(',')
           if i.strip().isdigit()]
    variants = list(ProductVariant.objects.filter(pk__in=ids)
                    .select_related('product'))
    # Etiketka nusxalari — har variant uchun nechta (default 1)
    try:
        copies = max(1, min(200, int(request.GET.get('copies') or 1)))
    except ValueError:
        copies = 1

    # Narx: variantning filial narxi yoki so'rov paramidan yoki mahsulot default
    price_param = request.GET.get('price')
    labels = []
    for v in variants:
        bc = v.barcode or gen_internal_ean13(v.pk)
        # EAN-13 barcode SVG (12 body -> lib check qo'shadi)
        try:
            svg_io = io.BytesIO()
            EAN13(bc[:12], writer=SVGWriter()).write(
                svg_io, options={'module_height': 9.0, 'module_width': 0.28,
                                 'font_size': 8, 'text_distance': 3.5,
                                 'quiet_zone': 2.0})
            barcode_svg = svg_io.getvalue().decode('utf-8')
            # <?xml ...?> va DOCTYPE'ni olib tashlaymiz (inline uchun)
            i = barcode_svg.find('<svg')
            barcode_svg = barcode_svg[i:] if i >= 0 else barcode_svg
        except Exception:
            barcode_svg = ''
        # QR (kod) -> PNG data URI
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=3, border=1)
        qr.add_data(bc)
        qr.make(fit=True)
        qbuf = io.BytesIO()
        qr.make_image(fill_color='black', back_color='white').save(qbuf, 'PNG')
        qr_uri = 'data:image/png;base64,' + base64.b64encode(qbuf.getvalue()).decode()
        # narx
        st = (BranchStock.objects.filter(variant=v, sale_price__gt=0)
              .order_by('-sale_price').first())
        price = (price_param or (st.sale_price if st else v.product.default_sale_price))
        labels.append({
            'variant': v, 'code': bc, 'barcode_svg': barcode_svg,
            'qr_uri': qr_uri, 'price': price,
        })
    # copies bo'yicha ko'paytiramiz
    render_labels = []
    for lb in labels:
        for _ in range(copies):
            render_labels.append(lb)
    return render(request, 'inventory/variant_labels.html', {
        'labels': render_labels, 'copies': copies,
        'ids': request.GET.get('ids', ''),
    })


@admin_required
def intake_variants(request):
    """Jadval usulida qabul — bitta sahifada mahsulot + bir nechta tur.

    Har qator = tur: rang, o'lcham, shtrix-kod (har turga alohida),
    tannarx, sotuv narx, miqdor. Mahsulot yangi yaratiladi yoki
    mavjudlardan tanlanadi (?code= bilan prefill ham mumkin).
    """
    from decimal import Decimal, InvalidOperation

    categories = Category.objects.order_by('name')
    branches = Branch.objects.filter(is_active=True)
    suppliers = Supplier.objects.filter(is_active=True).order_by('name')

    prefill_product = None
    if request.method == 'GET' and request.GET.get('code'):
        prefill_product = Product.objects.filter(
            code=normalize_code(request.GET['code'].upper())).first()

    post_back = None
    if request.method == 'POST':
        errors = []

        # ---------- mahsulot: mavjud yoki yangi ----------
        product = None
        product_code = (request.POST.get('product_code') or '').strip()
        new_name = smart_title(request.POST.get('new_name'))
        if product_code:
            product = Product.objects.filter(
                code=normalize_code(product_code.upper())).first()
            if not product:
                errors.append(f"Mahsulot topilmadi: {product_code}")
        elif new_name:
            # Yangi nom yozilgan -> YANGI mahsulot (jim birlashtirmaymiz).
            # Mavjud mahsulotga qo'shmoqchi bo'lsa, "Mavjud mahsulot" rejimida
            # ro'yxatdan tanlaydi (product_code). Bir brend ostida (masalan
            # Ezo) turli mahsulotlar bo'lishi mumkin.
            product = None  # keyin yangi yaratiladi
        else:
            errors.append("Mahsulot tanlang yoki yangi mahsulot nomini kiriting.")

        branch = Branch.objects.filter(
            pk=request.POST.get('branch') or 0, is_active=True).first()
        if not branch:
            errors.append("Filial tanlang.")

        supplier_text = (request.POST.get('supplier') or '').strip()
        note = (request.POST.get('note') or '').strip()

        # ---------- qatorlar ----------
        colors = request.POST.getlist('row_color')
        sizes = request.POST.getlist('row_size')
        barcodes = request.POST.getlist('row_barcode')
        prices = request.POST.getlist('row_price')
        qtys = request.POST.getlist('row_qty')

        _dec = parse_dec

        # Marja % (default 0): tannarx = sotuv narx / (1 + marja/100)
        try:
            marja = _dec(request.POST.get('marja'))
        except (InvalidOperation, ValueError):
            marja = Decimal('0')
        if marja < 0:
            marja = Decimal('0')

        rows, raw_rows = [], []
        seen_pairs, seen_barcodes = set(), set()
        for i in range(len(colors)):
            get = lambda lst: (lst[i] if i < len(lst) else '') or ''
            color = smart_title(get(colors))
            size = smart_title(get(sizes))
            barcode = get(barcodes).strip() or None
            price_raw, qty_raw = get(prices), get(qtys)
            if not (color or size or barcode or qty_raw.strip()):
                continue  # butunlay bo'sh qator
            raw_rows.append({'color': color, 'size': size,
                             'barcode': barcode or '',
                             'price': price_raw, 'qty': qty_raw})
            try:
                price = _dec(price_raw)
                qty = int(_dec(qty_raw))
            except (InvalidOperation, ValueError, TypeError):
                errors.append(f"{i + 1}-qator: narx yoki miqdor noto'g'ri.")
                continue
            if qty < 0 or price < 0:
                errors.append(f"{i + 1}-qator: manfiy qiymat kiritilmaydi.")
                continue
            # Tannarx marja orqali hisoblanadi (marja=0 -> tannarx = narx)
            if price > 0:
                cost = (price / (Decimal('1') + marja / Decimal('100'))
                        ).quantize(Decimal('0.01'))
            else:
                cost = Decimal('0')
            pair = (size, color)
            if pair in seen_pairs:
                errors.append(
                    f"{i + 1}-qator: {size or '—'} / {color or '—'} takrorlangan.")
                continue
            seen_pairs.add(pair)
            if barcode:
                if barcode in seen_barcodes:
                    errors.append(f"{i + 1}-qator: shtrix-kod jadvalda takror.")
                    continue
                seen_barcodes.add(barcode)
            rows.append({'color': color, 'size': size, 'barcode': barcode,
                         'cost': cost, 'price': price, 'qty': qty})

        if not rows and not errors:
            errors.append("Kamida bitta tur qatorini kiriting.")

        # Shtrix-kod bazadagi boshqa yozuvlar bilan to'qnashmasin
        for r in rows:
            if not r['barcode']:
                continue
            v_clash = ProductVariant.objects.filter(barcode=r['barcode'])
            if product:
                v_clash = v_clash.exclude(
                    product=product, size=r['size'], color=r['color'])
            v_clash = v_clash.select_related('product').first()
            if v_clash:
                errors.append(
                    f"Shtrix-kod {r['barcode']} band: {v_clash.product.name} "
                    f"({v_clash.size or '—'}/{v_clash.color or '—'}).")
            p_clash = Product.objects.filter(
                external_barcode=r['barcode']).first()
            if p_clash:
                errors.append(
                    f"Shtrix-kod {r['barcode']} '{p_clash.name}' "
                    f"mahsulotiga biriktirilgan.")

        if errors:
            for e in errors[:8]:
                messages.error(request, e)
            post_back = {
                'marja': request.POST.get('marja') or '0',
                'product_code': product_code,
                'product_name': product.name if product else '',
                'new_name': new_name,
                'category': request.POST.get('category') or '',
                'branch': request.POST.get('branch') or '',
                'supplier': supplier_text,
                'note': note,
                'rows': raw_rows,
            }
        else:
            total_qty = 0
            with transaction.atomic():
                if product is None:
                    category = Category.objects.filter(
                        pk=request.POST.get('category') or 0).first()
                    product = Product.objects.create(
                        name=new_name, category=category)
                supplier_obj = None
                if supplier_text:
                    supplier_obj = Supplier.objects.filter(
                        name__iexact=supplier_text).first()
                session = None
                if any(r['qty'] > 0 for r in rows):
                    session = IntakeSession.objects.create(
                        branch=branch, supplier=supplier_obj,
                        supplier_text='' if supplier_obj else supplier_text,
                        received_by=request.user, note=note)
                for r in rows:
                    variant, _ = ProductVariant.objects.get_or_create(
                        product=product, size=r['size'], color=r['color'])
                    if r['barcode'] and variant.barcode != r['barcode']:
                        variant.barcode = r['barcode']
                        variant.save(update_fields=['barcode'])
                    stock, _ = BranchStock.objects.get_or_create(
                        variant=variant, branch=branch,
                        defaults={'cost_price': r['cost'],
                                  'sale_price': r['price']})
                    stock.cost_price = r['cost']
                    if r['price'] > 0:
                        stock.sale_price = r['price']
                    if r['qty'] > 0:
                        stock.stock_count = F('stock_count') + r['qty']
                    stock.save()
                    if r['qty'] > 0:
                        Intake.objects.create(
                            session=session, supplier_ref=supplier_obj,
                            variant=variant, branch=branch,
                            quantity=r['qty'], cost_per_unit=r['cost'],
                            supplier=supplier_text, note=note,
                            received_by=request.user)
                        total_qty += r['qty']
                if product.default_sale_price == 0:
                    first_price = next(
                        (r['price'] for r in rows if r['price'] > 0), None)
                    if first_price:
                        product.default_sale_price = first_price
                        product.save(update_fields=['default_sale_price'])
            messages.success(
                request,
                f"Saqlandi: {product.name} — {len(rows)} ta tur, "
                f"jami {total_qty} dona ({branch.name}).")
            return redirect('product_detail', code=product.code)

    # Rang nomlaridagi YAKKA so'zlar — so'zma-so'z taklif uchun
    # ("Cleaning Anti Micro" -> Cleaning, Anti, Micro)
    _color_words = set()
    for _c in (ProductVariant.objects.exclude(color='')
               .values_list('color', flat=True).distinct()):
        for _w in _c.split():
            if len(_w) > 1:
                _color_words.add(_w)
    color_options = sorted(_color_words)[:800]
    return render(request, 'inventory/intake_variants.html', {
        'categories': categories,
        'branches': branches,
        'suppliers': suppliers,
        'prefill_product': prefill_product,
        'post_back': post_back,
        'color_options': color_options,
    })


PARFUM_BRANDS = [
    # Parfyumeriya
    'Chanel', 'Dior', 'Lancome', 'Guerlain', 'Yves Saint Laurent',
    'Giorgio Armani', 'Versace', 'Dolce & Gabbana', 'Gucci', 'Burberry',
    'Hugo Boss', 'Calvin Klein', 'Paco Rabanne', 'Carolina Herrera',
    'Givenchy', 'Hermes', 'Tom Ford', 'Montale', 'Mancera', 'Ajmal',
    'Lattafa', 'Armaf', 'Rasasi', 'Swiss Arabian', 'Al Haramain',
    # Soch/tana parvarishi
    'Head and Shoulders', 'Pantene', 'Gliss Kur', 'Schwarzkopf', 'Syoss',
    "L'Oreal", 'Garnier', 'Nivea', 'Dove', 'Rexona', 'Old Spice', 'AXE',
    'Palmolive', 'Le Petit Marseillais', 'Fa', 'Camay', 'Safeguard',
    # Koreys kosmetikasi
    'The Face Shop', 'Missha', 'Etude House', 'Innisfree', 'Laneige',
    'COSRX', 'Some By Mi', 'Holika Holika', 'Tony Moly', "It's Skin",
    '3W Clinic', 'FarmStay', 'Ekel', 'Jigott', 'Deoproce', 'Lebelage',
    'Enough', 'Esfolio', 'Eunyul', 'Mizon', 'SNP', 'Dr.Jart+',
    'Beauty of Joseon', 'Round Lab', 'Anua', 'Medicube', 'Skin1004',
    # Maishiy
    'Colgate', 'Oral-B', 'Sensodyne', 'Fairy', 'Ariel', 'Tide', 'Persil',
]

IMPORT_HEADERS = ['Mahsulot', 'Rang/Tur', "O'lcham", 'Shtrix-kod',
                  'Sotuv narx', 'Miqdor']


@admin_required
def intake_import_template(request):
    """yurit-import.xlsx shablonini yaratib beradi.

    1-varaq: import shabloni (+2 namuna qator)
    2-varaq: joriy katalog (faqat ma'lumot uchun — import o'qimaydi)
    3-varaq: mashhur brendlar ro'yxati (ma'lumot uchun)
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font = Font(bold=True, color='FFFFFF')
    head_fill = PatternFill('solid', fgColor='4472C4')

    ws = wb.active
    ws.title = 'Import'
    ws.append(IMPORT_HEADERS)
    for c in ws[1]:
        c.font = head_font
        c.fill = head_fill
    ws.append(['NAMUNA: Chanel Bleu de Chanel', 'EDP', '100ml',
               '3145891073607', '1500000', '2'])
    ws.append(['NAMUNA: Head and Shoulders', 'Mentol', '400',
               '5000174896190', '45000', '10'])
    note = ("Eslatma: NAMUNA qatorlarini o'chirib, o'z tovarlaringizni "
            "yozing. Bir mahsulotning har bir turi alohida qator. "
            "Import faqat shu varaqni o'qiydi.")
    ws.append([])
    ws.append([note])
    widths = [34, 22, 12, 18, 14, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws2 = wb.create_sheet('Katalogim')
    ws2.append(['Mahsulot', 'Rang/Tur', "O'lcham", 'Shtrix-kod'])
    for c in ws2[1]:
        c.font = head_font
        c.fill = head_fill
    for v in (ProductVariant.objects.select_related('product')
              .order_by('product__name', 'size', 'color')[:2000]):
        ws2.append([v.product.name, v.color, v.size, v.barcode or
                    v.product.external_barcode or ''])
    for i, w in enumerate([34, 26, 12, 18], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    ws3 = wb.create_sheet('Brendlar')
    ws3.append(['Mashhur brendlar (ma\'lumot uchun)'])
    ws3[1][0].font = head_font
    ws3[1][0].fill = head_fill
    for b in PARFUM_BRANDS:
        ws3.append([b])
    ws3.column_dimensions['A'].width = 30

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="yurit-import.xlsx"'
    return resp


@admin_required
def intake_import(request):
    """Excel (xlsx) orqali ommaviy qabul: mahsulot + turlar + shtrix-kodlar.

    Hammasi-yoki-hech-narsa: bironta qatorda xato bo'lsa, hech nima
    saqlanmaydi (qayta yuklashda ikki marta qo'shilib ketmasligi uchun).
    """
    from decimal import Decimal

    branches = Branch.objects.filter(is_active=True)

    if request.method == 'POST' and request.FILES.get('file'):
        from openpyxl import load_workbook

        branch = Branch.objects.filter(
            pk=request.POST.get('branch') or 0, is_active=True).first()
        if not branch:
            messages.error(request, "Filial tanlang.")
            return redirect('intake_import')
        try:
            marja = parse_dec(request.POST.get('marja'))
        except Exception:
            marja = Decimal('0')
        if marja < 0:
            marja = Decimal('0')

        try:
            wb = load_workbook(request.FILES['file'], read_only=True,
                               data_only=True)
        except Exception:
            messages.error(request, "Fayl o'qilmadi — .xlsx formatda yuklang.")
            return redirect('intake_import')
        ws = wb['Import'] if 'Import' in wb.sheetnames else wb.worksheets[0]

        errors, rows = [], []
        seen_barcodes = {}
        for idx, raw in enumerate(ws.iter_rows(min_row=2, values_only=True),
                                  start=2):
            vals = [(str(x).strip() if x is not None else '')
                    for x in (list(raw) + [''] * 6)[:6]]
            name, color, size, barcode, price_raw, qty_raw = vals
            if not any(vals):
                continue
            if name.upper().startswith('NAMUNA'):
                continue
            name = smart_title(name)
            color = smart_title(color)
            size = smart_title(size)
            if not name:
                errors.append(f"{idx}-qator: mahsulot nomi bo'sh.")
                continue
            if not (color or size or barcode):
                errors.append(f"{idx}-qator: tur ma'lumoti yo'q.")
                continue
            try:
                price = parse_dec(price_raw)
                qty = int(parse_dec(qty_raw))
            except Exception:
                errors.append(f"{idx}-qator: narx/miqdor noto'g'ri "
                              f"({price_raw!r}, {qty_raw!r}).")
                continue
            if price < 0 or qty < 0:
                errors.append(f"{idx}-qator: manfiy qiymat.")
                continue
            barcode = barcode or None
            if barcode:
                if barcode in seen_barcodes:
                    errors.append(
                        f"{idx}-qator: shtrix-kod {barcode} faylda takror "
                        f"({seen_barcodes[barcode]}-qatorda ham).")
                    continue
                seen_barcodes[barcode] = idx
            rows.append({'line': idx, 'name': name, 'color': color,
                         'size': size, 'barcode': barcode,
                         'price': price, 'qty': qty})

        if not rows and not errors:
            errors.append("Faylda import qilinadigan qator topilmadi.")

        # nom -> mavjud mahsulot (bir nechta bo'lsa xato)
        products_cache = {}
        for r in rows:
            key = r['name'].lower()
            if key in products_cache:
                continue
            qs = list(Product.objects.filter(name__iexact=r['name'])[:2])
            if len(qs) > 1:
                errors.append(
                    f"'{r['name']}' nomida bir nechta mahsulot bor — avval "
                    f"birlashtiring (Mahsulotlar → Birlashtirish).")
                products_cache[key] = None
            else:
                products_cache[key] = qs[0] if qs else None

        # shtrix-kod bazada bandmi?
        for r in rows:
            if not r['barcode']:
                continue
            existing_product = products_cache.get(r['name'].lower())
            v_clash = ProductVariant.objects.filter(barcode=r['barcode'])
            if existing_product:
                v_clash = v_clash.exclude(product=existing_product,
                                          size=r['size'], color=r['color'])
            v_clash = v_clash.select_related('product').first()
            if v_clash:
                errors.append(
                    f"{r['line']}-qator: shtrix-kod {r['barcode']} band — "
                    f"{v_clash.product.name} "
                    f"({v_clash.size or '—'}/{v_clash.color or '—'}).")
            p_clash = Product.objects.filter(
                external_barcode=r['barcode']).first()
            if p_clash and (not existing_product
                            or p_clash.pk != existing_product.pk):
                errors.append(
                    f"{r['line']}-qator: shtrix-kod {r['barcode']} "
                    f"'{p_clash.name}' mahsulotida.")

        if errors:
            for e in errors[:12]:
                messages.error(request, e)
            if len(errors) > 12:
                messages.warning(request,
                                 f"... yana {len(errors) - 12} ta xato.")
            messages.warning(request,
                             "Hech narsa saqlanmadi — faylni tuzatib "
                             "qayta yuklang.")
        else:
            created_products = 0
            total_qty = 0
            with transaction.atomic():
                session = None
                if any(r['qty'] > 0 for r in rows):
                    session = IntakeSession.objects.create(
                        branch=branch, received_by=request.user,
                        note="Excel import")
                for r in rows:
                    key = r['name'].lower()
                    product = products_cache.get(key)
                    if product is None:
                        product = Product.objects.create(name=r['name'])
                        products_cache[key] = product
                        created_products += 1
                    variant, _ = ProductVariant.objects.get_or_create(
                        product=product, size=r['size'], color=r['color'])
                    if r['barcode'] and variant.barcode != r['barcode']:
                        variant.barcode = r['barcode']
                        variant.save(update_fields=['barcode'])
                    cost = (r['price'] / (Decimal('1') + marja /
                            Decimal('100'))).quantize(Decimal('0.01')) \
                        if r['price'] > 0 else Decimal('0')
                    stock, _ = BranchStock.objects.get_or_create(
                        variant=variant, branch=branch,
                        defaults={'cost_price': cost,
                                  'sale_price': r['price']})
                    stock.cost_price = cost
                    if r['price'] > 0:
                        stock.sale_price = r['price']
                    if r['qty'] > 0:
                        stock.stock_count = F('stock_count') + r['qty']
                    stock.save()
                    if r['qty'] > 0:
                        Intake.objects.create(
                            session=session, variant=variant, branch=branch,
                            quantity=r['qty'], cost_per_unit=cost,
                            note="Excel import", received_by=request.user)
                        total_qty += r['qty']
                    if product.default_sale_price == 0 and r['price'] > 0:
                        product.default_sale_price = r['price']
                        product.save(update_fields=['default_sale_price'])
                AuditLog.objects.create(
                    user=request.user,
                    username_snapshot=request.user.username,
                    action=AuditLog.Action.CREATE,
                    model_name='Product',
                    object_id='',
                    object_repr=(f"Excel import: {len(rows)} qator, "
                                 f"{created_products} yangi mahsulot, "
                                 f"{total_qty} dona ({branch.name})")[:300],
                )
            messages.success(
                request,
                f"Import tayyor: {len(rows)} qator — {created_products} ta "
                f"yangi mahsulot, jami {total_qty} dona ({branch.name}).")
            return redirect('product_list')

    return render(request, 'inventory/intake_import.html', {
        'branches': branches,
    })


@admin_required
def intake_new(request):
    """Qabul dashboard'i: 3 ta kirish nuqtasi + so'nggi sessiyalar + low-stock."""
    products = Product.objects.order_by('-created_at')[:50]
    recent_sessions = (IntakeSession.objects
                       .select_related('branch', 'supplier', 'received_by')
                       .prefetch_related('intakes')
                       .order_by('-received_at')[:10])
    # Low-stock: o'rtacha kunlik sotuvga nisbatan kam qolgan mahsulotlar
    low_stock = (BranchStock.objects
                 .filter(stock_count__lte=3)
                 .select_related('variant__product', 'branch')
                 .order_by('stock_count')[:10])
    return render(request, 'inventory/intake_choose.html', {
        'products': products,
        'recent_sessions': recent_sessions,
        'low_stock': low_stock,
    })


# ---------- QUICK INTAKE (scanner-driven, multi-product session) ----------

@admin_required
def intake_quick(request):
    """Tezkor qabul sahifasi — scanner orqali ko'p mahsulot bir sessiyada."""
    branches = Branch.objects.filter(is_active=True).order_by('name')
    suppliers = Supplier.objects.filter(is_active=True).order_by('name')
    return render(request, 'inventory/intake_quick.html', {
        'branches': branches, 'suppliers': suppliers,
    })


@admin_required
def intake_lookup(request):
    """GET /intake/lookup/?q=... — qabul uchun mahsulot qidirish.
    POS'dagidan farqi: stock cheklov yo'q (yangi mahsulot ham qabul qilinadi)."""
    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({'found': False})
    code = normalize_code(q.upper())
    product = Product.objects.filter(
        Q(code=code) | Q(external_barcode=q)
    ).first()
    if not product:
        _vm = ProductVariant.objects.filter(barcode=q).select_related('product').first()
        if _vm:
            product = _vm.product
    if not product:
        matches = list(Product.objects.filter(
            Q(name__icontains=q) | Q(category__name__icontains=q)
        )[:8])
        if len(matches) == 1:
            product = matches[0]
        elif matches:
            return JsonResponse({
                'found': False,
                'suggestions': [{'code': p.code, 'name': p.name} for p in matches],
            })
        else:
            return JsonResponse({'found': False, 'suggestions': []})

    # Existing variants — for matrix products show them, allow new ones too
    variants = list(product.variants.values_list('size', 'color').distinct())
    # Last 5 intakes for cost-history hint
    last_intakes = (Intake.objects.filter(variant__product=product)
                    .select_related('variant', 'branch')
                    .order_by('-received_at')[:5])
    last_intakes_data = [{
        'date': i.received_at.strftime('%d.%m.%Y'),
        'branch': i.branch.name,
        'qty': i.quantity,
        'cost': float(i.cost_per_unit),
        'supplier': i.supplier_ref.name if i.supplier_ref else (i.supplier or ''),
    } for i in last_intakes]

    return JsonResponse({
        'found': True,
        'product': {
            'id': product.id,
            'code': product.code,
            'external_barcode': product.external_barcode or '',
            'name': product.name,
            'default_sale_price': float(product.default_sale_price),
            'markup_percent': float(product.markup_percent),
            'has_variants': bool(variants),
        },
        'variants': [{'size': s, 'color': c} for (s, c) in variants],
        'last_intakes': last_intakes_data,
    })


@admin_required
def intake_supplier_search(request):
    """GET /intake/supplier-search/?q=... — autocomplete."""
    q = (request.GET.get('q') or '').strip()
    qs = Supplier.objects.filter(is_active=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    return JsonResponse({
        'results': [{
            'id': s.id, 'name': s.name, 'phone': s.phone,
            'contact': s.contact_person,
        } for s in qs.order_by('name')[:10]],
    })


@admin_required
def intake_quick_save(request):
    """POST /intake/quick/save/ JSON:
    {branch_id, supplier_id|supplier_text, invoice_number, note,
     lines: [{product_id, size, color, qty, cost, sale_price, wholesale_price,
              markup, update_product_price, is_return, return_reason}]}
    Atomic: sessiya + barcha intakelar."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    try:
        data = _json.loads(request.body.decode('utf-8'))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad JSON'}, status=400)

    try:
        branch_id = int(data.get('branch_id') or 0)
    except (TypeError, ValueError):
        branch_id = 0
    branch = Branch.objects.filter(pk=branch_id, is_active=True).first()
    if not branch:
        return JsonResponse({'ok': False, 'error': "Filial tanlang"}, status=400)

    supplier = None
    try:
        sid = int(data.get('supplier_id') or 0)
        if sid:
            supplier = Supplier.objects.filter(pk=sid, is_active=True).first()
    except (TypeError, ValueError):
        pass
    supplier_text = (data.get('supplier_text') or '').strip()[:200]

    lines = data.get('lines') or []
    if not lines:
        return JsonResponse({'ok': False, 'error': "Qator yo'q"}, status=400)

    from decimal import Decimal
    affected_product_codes = set()
    with transaction.atomic():
        session = IntakeSession.objects.create(
            branch=branch,
            supplier=supplier,
            supplier_text=supplier_text,
            received_by=request.user,
            invoice_number=(data.get('invoice_number') or '').strip()[:80],
            note=(data.get('note') or '').strip(),
        )
        for ln in lines:
            try:
                pid = int(ln['product_id'])
                qty = int(ln['qty'])
                cost = Decimal(str(ln.get('cost') or '0'))
            except (KeyError, ValueError, TypeError):
                return JsonResponse({'ok': False, 'error': "qator noto'g'ri"}, status=400)
            if qty == 0:
                continue
            is_return = bool(ln.get('is_return'))
            if is_return and qty > 0:
                qty = -abs(qty)
            elif not is_return and qty < 0:
                qty = abs(qty)

            size = (ln.get('size') or '—').strip() or '—'
            color = (ln.get('color') or '—').strip() or '—'
            product = Product.objects.filter(pk=pid).first()
            if not product:
                return JsonResponse({'ok': False,
                                     'error': f'Mahsulot #{pid} topilmadi'}, status=400)
            variant, _ = ProductVariant.objects.get_or_create(
                product=product, size=size, color=color,
            )
            sale_price = Decimal(str(ln.get('sale_price') or '0'))
            wholesale_price = Decimal(str(ln.get('wholesale_price') or '0'))
            update_price = bool(ln.get('update_product_price'))

            stock, _ = BranchStock.objects.get_or_create(
                variant=variant, branch=branch,
                defaults={'cost_price': cost, 'sale_price': sale_price,
                          'wholesale_price': wholesale_price},
            )
            # For returns we decrement; never go below 0
            new_count = stock.stock_count + qty
            if new_count < 0:
                return JsonResponse({
                    'ok': False,
                    'error': f"{product.code} {size}/{color}: zaxira yetarli emas "
                             f"(joriy {stock.stock_count}, qaytarish {abs(qty)})",
                }, status=400)
            stock.stock_count = new_count
            if not is_return:
                stock.cost_price = cost
                if sale_price > 0:
                    stock.sale_price = sale_price
                if wholesale_price > 0:
                    stock.wholesale_price = wholesale_price
            stock.save()

            Intake.objects.create(
                session=session,
                supplier_ref=supplier,
                variant=variant, branch=branch,
                quantity=qty, cost_per_unit=cost,
                supplier=supplier.name if supplier else supplier_text,
                is_return=is_return,
                return_reason=(ln.get('return_reason') or '').strip()[:200],
                received_by=request.user,
                note=(ln.get('note') or '').strip(),
            )

            if update_price and sale_price > 0 and not is_return:
                product.default_sale_price = sale_price
                if ln.get('markup'):
                    try:
                        product.markup_percent = Decimal(str(ln['markup']))
                    except (ValueError, TypeError):
                        pass
                product.save()

            affected_product_codes.add(product.code)

    return JsonResponse({
        'ok': True,
        'session_id': session.pk,
        'lines_count': session.intakes.count(),
        'total_qty': session.total_qty,
        'product_codes': list(affected_product_codes),
        'label_url': f"/labels/?codes={','.join(affected_product_codes)}",
    })


@admin_required
def intake_quick_save_upload(request):
    """POST /intake/quick/save-invoice/ — invoice image upload to existing session."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    try:
        session_id = int(request.POST.get('session_id') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'bad session'}, status=400)
    session = IntakeSession.objects.filter(pk=session_id).first()
    if not session:
        return JsonResponse({'ok': False, 'error': 'sessiya topilmadi'}, status=404)
    if 'invoice_image' in request.FILES:
        session.invoice_image = request.FILES['invoice_image']
        session.save(update_fields=['invoice_image'])
    return JsonResponse({'ok': True})


# ---------- SUPPLIER MANAGEMENT ----------

@admin_required
def send_daily_summary_now(request):
    """POST /dashboard/send-daily/ — Telegram'ga kunlik xulosa yuborish."""
    if request.method != 'POST':
        return redirect('dashboard')
    from .notifications import daily_summary_text, send_telegram, _enabled
    if not _enabled():
        messages.error(request,
            "Telegram sozlanmagan: TELEGRAM_BOT_TOKEN va TELEGRAM_CHAT_IDS env'ni belgilang.")
        return redirect('dashboard')
    try:
        text = daily_summary_text()
        ok = send_telegram(text)
        if ok:
            messages.success(request, "✓ Kunlik xulosa Telegram'ga yuborildi.")
        else:
            messages.error(request, "Yuborilmadi — Telegram API javob bermadi.")
    except Exception as e:
        messages.error(request, f"Xatolik: {e}")
    return redirect('dashboard')


def _clean_text(s, max_len=None):
    """Strip HTML tags and normalize whitespace. Prevents stored XSS from CSV."""
    if not s:
        return ''
    import re
    # Remove anything that looks like an HTML tag
    s = re.sub(r'<[^>]*>', '', str(s))
    # Decode HTML entities
    import html
    s = html.unescape(s)
    s = s.strip()
    if max_len:
        s = s[:max_len]
    return s


@admin_required
def csv_import(request):
    """CSV import: mahsulot, mijoz yoki zaxira.

    Format va template'lar pastda. Birinchi qatordagi headerlar bizning
    columns'larga mos kelishi kerak.
    """
    entity = request.GET.get('entity') or request.POST.get('entity') or 'products'
    if entity not in ('products', 'customers', 'stock'):
        entity = 'products'

    if request.method == 'POST' and 'csv_file' in request.FILES:
        f = request.FILES['csv_file']
        # S6: 5MB hard limit on CSV uploads (DATA_UPLOAD_MAX_MEMORY_SIZE
        # is 10MB but a CSV that big = ~50K rows which Django can't process
        # in one request anyway — block early with a useful message)
        if f.size > 5 * 1024 * 1024:
            messages.error(request,
                f"Fayl juda katta ({f.size//1024} KB). "
                "Maksimum 5 MB. Faylni qismlarga bo'ling.")
            return redirect(f'/import/?entity={entity}')
        try:
            data = f.read().decode('utf-8-sig')  # handle BOM
        except UnicodeDecodeError:
            data = f.read().decode('cp1251', errors='ignore')
        reader = csv.DictReader(io.StringIO(data))
        rows = list(reader)
        if not rows:
            messages.error(request, "Bo'sh fayl yoki noto'g'ri format.")
            return redirect(f'/import/?entity={entity}')

        created = 0
        updated = 0
        failed = []

        try:
            with transaction.atomic():
                if entity == 'products':
                    for i, row in enumerate(rows, 2):  # row 2 = first data row
                        try:
                            name = _clean_text(row.get('name'), 200)
                            if not name:
                                raise ValueError("nom (name) bo'sh")
                            code = _clean_text(row.get('code'), 20).upper()
                            cat_name = _clean_text(row.get('category'), 120)
                            category = None
                            if cat_name:
                                category, _ = Category.objects.get_or_create(name=cat_name)
                            from decimal import Decimal
                            price = Decimal(row.get('default_sale_price') or '0')
                            markup = Decimal(row.get('markup_percent') or '40')
                            desc = _clean_text(row.get('description'), 1000)
                            ext_barcode = (row.get('external_barcode') or '').strip() or None
                            if ext_barcode:
                                clash = Product.objects.filter(
                                    external_barcode=ext_barcode
                                )
                                if code:
                                    clash = clash.exclude(code=code)
                                if clash.exists():
                                    raise ValueError(
                                        f"barcode {ext_barcode} boshqa mahsulotda mavjud"
                                    )
                            if code:
                                p, was_created = Product.objects.update_or_create(
                                    code=code,
                                    defaults={
                                        'name': name, 'category': category,
                                        'external_barcode': ext_barcode,
                                        'default_sale_price': price,
                                        'markup_percent': markup,
                                        'description': desc,
                                    }
                                )
                            else:
                                p = Product.objects.create(
                                    name=name, category=category,
                                    external_barcode=ext_barcode,
                                    default_sale_price=price,
                                    markup_percent=markup,
                                    description=desc,
                                )
                                was_created = True
                            if was_created:
                                created += 1
                            else:
                                updated += 1
                        except Exception as e:
                            failed.append({'row': i, 'reason': str(e)})

                elif entity == 'customers':
                    for i, row in enumerate(rows, 2):
                        try:
                            phone = _clean_text(row.get('phone'), 40)
                            name = _clean_text(row.get('name'), 120)
                            if not phone and not name:
                                raise ValueError("phone va name ikkalasi bo'sh")
                            tags = _clean_text(row.get('tags'), 200)
                            inn = _clean_text(row.get('inn'), 14)
                            note = _clean_text(row.get('note'), 500)
                            if phone:
                                c, was_created = Customer.objects.update_or_create(
                                    phone=phone,
                                    defaults={'name': name, 'tags': tags,
                                              'inn': inn, 'note': note},
                                )
                            else:
                                c = Customer.objects.create(
                                    name=name, tags=tags, inn=inn, note=note,
                                )
                                was_created = True
                            if was_created:
                                created += 1
                            else:
                                updated += 1
                        except Exception as e:
                            failed.append({'row': i, 'reason': str(e)})

                elif entity == 'stock':
                    for i, row in enumerate(rows, 2):
                        try:
                            code = (row.get('product_code') or '').strip().upper()
                            branch_name = (row.get('branch') or '').strip()
                            size = (row.get('size') or '—').strip() or '—'
                            color = (row.get('color') or '—').strip() or '—'
                            qty = int(row.get('quantity') or 0)
                            from decimal import Decimal
                            cost = Decimal(row.get('cost_price') or '0')
                            sale = Decimal(row.get('sale_price') or '0')

                            if not code or not branch_name or qty <= 0:
                                raise ValueError("code, branch, va quantity>0 kerak")
                            product = Product.objects.filter(code=code).first()
                            if not product:
                                raise ValueError(f"mahsulot {code} topilmadi")
                            branch = Branch.objects.filter(name=branch_name).first()
                            if not branch:
                                raise ValueError(f"filial '{branch_name}' topilmadi")

                            variant, _ = ProductVariant.objects.get_or_create(
                                product=product, size=size, color=color,
                            )
                            stock, was_created = BranchStock.objects.get_or_create(
                                variant=variant, branch=branch,
                                defaults={'cost_price': cost, 'sale_price': sale,
                                          'stock_count': qty},
                            )
                            if not was_created:
                                stock.stock_count = F('stock_count') + qty
                                if cost > 0: stock.cost_price = cost
                                if sale > 0: stock.sale_price = sale
                                stock.save()
                            # Create intake record
                            Intake.objects.create(
                                variant=variant, branch=branch,
                                quantity=qty, cost_per_unit=cost,
                                supplier='CSV import',
                                received_by=request.user,
                            )
                            if was_created:
                                created += 1
                            else:
                                updated += 1
                        except Exception as e:
                            failed.append({'row': i, 'reason': str(e)})

                if failed and len(failed) == len(rows):
                    # Hammasi xato — rollback
                    raise ValueError(f"Hammasi xato: birinchi sabab — {failed[0]['reason']}")
        except Exception as e:
            messages.error(request, f"Import to'xtatildi: {e}")
            return redirect(f'/import/?entity={entity}')

        if created or updated:
            messages.success(request,
                f"Qo'shildi: {created}, yangilandi: {updated}, xato: {len(failed)}")
        if failed:
            preview = '; '.join(f"qator {f['row']}: {f['reason']}" for f in failed[:5])
            messages.warning(request, f"Xato qatorlar: {preview}"
                             + (f" (jami {len(failed)} ta)" if len(failed) > 5 else ''))
        return redirect(f'/import/?entity={entity}')

    return render(request, 'inventory/csv_import.html', {
        'entity': entity,
    })


@admin_required
def cashier_stats(request, user_id):
    """Sotuvchi performans dashboard'i: 30-kun kpi, kunlik trend,
    soatlar bo'yicha aktivlik, top mahsulotlar, komissiya."""
    seller = get_object_or_404(User, pk=user_id)

    since_30d = timezone.now() - timedelta(days=30)
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    sales_30d = Sale.objects.filter(sold_by=seller, sold_at__gte=since_30d)
    agg = sales_30d.aggregate(
        revenue=Sum(rev_expr),
        cost=Sum(cost_expr),
        qty=Sum('quantity'),
        txns=Count('transaction', distinct=True),
    )
    revenue = float(agg['revenue'] or 0)
    cost = float(agg['cost'] or 0)
    qty = agg['qty'] or 0
    txns = agg['txns'] or 0
    avg_ticket = revenue / txns if txns else 0
    profit = revenue - cost
    commission_pct = float(seller.commission_percent or 0)
    commission = revenue * commission_pct / 100 if commission_pct else 0

    # Refunds initiated by this user
    refunds_30d = Return.objects.filter(refunded_by=seller, refunded_at__gte=since_30d)
    refund_count = refunds_30d.count()
    refund_qty = refunds_30d.aggregate(s=Sum('quantity'))['s'] or 0
    refund_rate = (refund_qty / qty * 100) if qty else 0

    # Daily trend (last 30 days)
    from collections import OrderedDict
    daily = OrderedDict()
    today = timezone.localdate()
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        daily[d.isoformat()] = 0
    for row in (sales_30d.annotate(d=F('sold_at__date')).values('d')
                .annotate(r=Sum(rev_expr))):
        key = row['d'].isoformat() if row['d'] else None
        if key and key in daily:
            daily[key] = float(row['r'] or 0)
    daily_labels = [k[5:] for k in daily.keys()]
    daily_values = list(daily.values())

    # Hourly heatmap
    hour_qty = [0] * 24
    for sold_at, in sales_30d.values_list('sold_at'):
        h = sold_at.astimezone(timezone.get_current_timezone()).hour
        hour_qty[h] += 1
    hour_labels = [f'{h:02d}' for h in range(24)]

    # Top products
    top_products = list(sales_30d.values('variant__product__code', 'variant__product__name')
                        .annotate(qty=Sum('quantity'), revenue=Sum(rev_expr))
                        .order_by('-revenue')[:5])

    # Sales per active day
    active_days = sales_30d.values('sold_at__date').distinct().count() or 1
    sales_per_day = txns / active_days

    # D3: Anomaly detection — compare this seller to peer average
    peer_stats = (Sale.objects.filter(sold_at__gte=since_30d)
                  .exclude(sold_by=seller)
                  .values('sold_by_id')
                  .annotate(rev=Sum(rev_expr), qty=Sum('quantity'),
                            txns=Count('transaction', distinct=True)))
    peers = list(peer_stats)
    peer_count = len(peers)
    anomalies = []
    if peer_count >= 2:  # need enough peers for comparison
        peer_avg_tickets = [float(p['rev'] or 0) / p['txns'] if p['txns'] else 0
                            for p in peers]
        peer_avg_ticket = sum(peer_avg_tickets) / len(peer_avg_tickets) or 1
        peer_avg_qty_per_txn = sum((p['qty'] or 0) / p['txns'] if p['txns'] else 0
                                   for p in peers) / len(peers) or 1

        my_avg_ticket = float(avg_ticket or 0)
        my_qty_per_txn = (qty / txns) if txns else 0

        # Refund rate (peer avg)
        peer_refund_qty = Return.objects.filter(
            refunded_at__gte=since_30d
        ).exclude(refunded_by=seller).aggregate(s=Sum('quantity'))['s'] or 0
        peer_sold_qty = sum((p['qty'] or 0) for p in peers)
        peer_refund_rate = (peer_refund_qty / peer_sold_qty * 100) if peer_sold_qty else 0

        # 1) Average ticket significantly different
        if my_avg_ticket > peer_avg_ticket * 2:
            anomalies.append({
                'level': 'warning',
                'icon': 'arrow-up-circle',
                'title': "O'rtacha chek juda yuqori",
                'detail': f"Sizning: {my_avg_ticket:,.0f}, hamkasblar o'rtacha: {peer_avg_ticket:,.0f} so'm",
            })
        elif my_avg_ticket > 0 and my_avg_ticket < peer_avg_ticket * 0.4:
            anomalies.append({
                'level': 'warning',
                'icon': 'arrow-down-circle',
                'title': "O'rtacha chek juda past",
                'detail': f"Sizning: {my_avg_ticket:,.0f}, hamkasblar o'rtacha: {peer_avg_ticket:,.0f} so'm",
            })

        # 2) Refund rate much higher than peers
        if refund_rate > peer_refund_rate * 2 and refund_rate > 5:
            anomalies.append({
                'level': 'danger',
                'icon': 'exclamation-triangle',
                'title': "Qaytarish foizi juda yuqori",
                'detail': f"Sizning: {refund_rate:.1f}%, hamkasblar o'rtacha: {peer_refund_rate:.1f}%",
            })

        # 3) Very high txn count (productive) or very low
        if peers:
            peer_txns_avg = sum(p['txns'] for p in peers) / len(peers)
            if txns > peer_txns_avg * 3 and txns > 50:
                anomalies.append({
                    'level': 'info',
                    'icon': 'star-fill',
                    'title': "Yuqori sotuv hajmi",
                    'detail': f"Sizning: {txns} chek, hamkasblar o'rtacha: {peer_txns_avg:.0f} — top performer!",
                })

    return render(request, 'inventory/cashier_stats.html', {
        'seller': seller,
        'revenue': revenue, 'cost': cost, 'profit': profit,
        'qty': qty, 'txns': txns,
        'avg_ticket': avg_ticket,
        'commission_pct': commission_pct,
        'commission': commission,
        'refund_count': refund_count,
        'refund_qty': refund_qty,
        'refund_rate': refund_rate,
        'daily_labels': daily_labels,
        'daily_values': daily_values,
        'hour_labels': hour_labels,
        'hour_qty': hour_qty,
        'top_products': top_products,
        'active_days': active_days,
        'sales_per_day': sales_per_day,
        'anomalies': anomalies,
        'peer_count': peer_count,
    })


@admin_required
def reorder_page(request):
    """Smart reorder: past zaxiradagi mahsulotlar + tavsiya etilgan miqdor.

    Formula: 30-kunlik sotuvni 30 ga bo'lib kunlik o'rtacha velocity, undan
    keyin (BUFFER_DAYS = 14) ga ko'paytirib taklif miqdorini chiqaramiz.
    """
    BUFFER_DAYS = int(request.GET.get('buffer') or 14)
    LOW_THRESHOLD = int(request.GET.get('threshold') or 5)
    branch_id = request.GET.get('branch') or ''

    since_30d = timezone.now() - timedelta(days=30)

    stocks_qs = (BranchStock.objects
                 .filter(stock_count__lte=LOW_THRESHOLD)
                 .select_related('variant__product__category', 'branch'))
    if branch_id:
        try:
            stocks_qs = stocks_qs.filter(branch_id=int(branch_id))
        except ValueError:
            branch_id = ''

    # 30-day sales per (variant, branch) pair
    sales_map = {}
    for row in (Sale.objects.filter(sold_at__gte=since_30d)
                .values('variant_id', 'branch_id')
                .annotate(qty=Sum('quantity'))):
        sales_map[(row['variant_id'], row['branch_id'])] = row['qty'] or 0

    # D4: trend velocity — last 7 days vs previous 7 days. If accelerating,
    # adjust suggested order qty upward.
    last_7 = timezone.now() - timedelta(days=7)
    prev_7_start = timezone.now() - timedelta(days=14)
    last7_map = {}
    for row in (Sale.objects.filter(sold_at__gte=last_7)
                .values('variant_id', 'branch_id')
                .annotate(qty=Sum('quantity'))):
        last7_map[(row['variant_id'], row['branch_id'])] = row['qty'] or 0
    prev7_map = {}
    for row in (Sale.objects.filter(sold_at__gte=prev_7_start, sold_at__lt=last_7)
                .values('variant_id', 'branch_id')
                .annotate(qty=Sum('quantity'))):
        prev7_map[(row['variant_id'], row['branch_id'])] = row['qty'] or 0

    # Most recent supplier per product (from last intake)
    supplier_map = {}
    for row in (Intake.objects.filter(quantity__gt=0)
                .order_by('-received_at')
                .values('variant__product_id', 'supplier_ref__name', 'supplier')[:1000]):
        pid = row['variant__product_id']
        if pid not in supplier_map:
            name = row['supplier_ref__name'] or row['supplier'] or ''
            if name:
                supplier_map[pid] = name

    items = []
    for s in stocks_qs:
        sold30 = sales_map.get((s.variant_id, s.branch_id), 0)
        daily = sold30 / 30 if sold30 else 0
        # D4: trend factor — if last 7 days >> previous 7 days, demand is rising;
        # multiply daily by trend factor (capped at 2.0 to avoid wild over-orders)
        l7 = last7_map.get((s.variant_id, s.branch_id), 0)
        p7 = prev7_map.get((s.variant_id, s.branch_id), 0)
        if p7 > 0 and l7 > 0:
            trend_ratio = l7 / p7
            trend_factor = max(0.5, min(2.0, trend_ratio))
        elif l7 > 0 and p7 == 0:
            trend_factor = 1.5  # new product gaining traction
        else:
            trend_factor = 1.0
        adjusted_daily = daily * trend_factor

        # Target = buffer-days of stock; subtract what's already there
        target = adjusted_daily * BUFFER_DAYS
        suggested = max(0, int(round(target - s.stock_count)))
        if suggested == 0 and sold30 == 0 and s.stock_count > 0:
            # Not moving, has some stock — skip
            continue

        trend_label = ''
        if trend_factor > 1.2:
            trend_label = 'up'
        elif trend_factor < 0.8:
            trend_label = 'down'

        items.append({
            'stock': s,
            'product': s.variant.product,
            'variant': s.variant,
            'branch': s.branch,
            'sold_30d': sold30,
            'daily_avg': daily,
            'adjusted_daily': adjusted_daily,
            'trend_factor': trend_factor,
            'trend_label': trend_label,
            'last_7': l7,
            'prev_7': p7,
            'days_left': (s.stock_count / adjusted_daily) if adjusted_daily else None,
            'suggested': suggested,
            'cost': float(s.cost_price),
            'estimated_cost': suggested * float(s.cost_price),
            'last_supplier': supplier_map.get(s.variant.product_id, ''),
        })

    # Group by supplier
    by_supplier = {}
    for it in items:
        key = it['last_supplier'] or '(noma\'lum)'
        by_supplier.setdefault(key, []).append(it)
    suppliers_grouped = sorted(by_supplier.items(),
                               key=lambda kv: -sum(x['estimated_cost'] for x in kv[1]))

    # M5: aggregate per product (across branches) — single row per product
    # showing total stock across all branches, total suggested qty, etc.
    by_product = {}
    for it in items:
        pid = it['product'].id
        if pid not in by_product:
            by_product[pid] = {
                'product': it['product'],
                'last_supplier': it['last_supplier'],
                'stock_total': 0,
                'sold_30d_total': 0,
                'suggested_total': 0,
                'estimated_cost_total': 0.0,
                'branches': set(),
            }
        agg = by_product[pid]
        agg['stock_total'] += it['stock'].stock_count
        agg['sold_30d_total'] += it['sold_30d']
        agg['suggested_total'] += it['suggested']
        agg['estimated_cost_total'] += it['estimated_cost']
        agg['branches'].add(it['branch'].name)
    products_grouped = sorted(by_product.values(),
                              key=lambda x: -x['estimated_cost_total'])
    # Convert branches set to sorted list for template
    for p in products_grouped:
        p['branches'] = sorted(p['branches'])

    total_suggested_value = sum(it['estimated_cost'] for it in items)
    total_items = len(items)
    total_unique_products = len(by_product)

    view_mode = request.GET.get('view') or 'supplier'  # supplier | product

    return render(request, 'inventory/reorder.html', {
        'items': items,
        'by_supplier': suppliers_grouped,
        'products_grouped': products_grouped,
        'view_mode': view_mode,
        'total_items': total_items,
        'total_unique_products': total_unique_products,
        'total_suggested_value': total_suggested_value,
        'buffer_days': BUFFER_DAYS,
        'low_threshold': LOW_THRESHOLD,
        'branch_id': branch_id,
        'branches': Branch.objects.filter(is_active=True).order_by('name'),
    })


@admin_required
def supplier_list(request):
    if request.method == 'POST':
        action = request.POST.get('action') or 'create'
        if action == 'delete':
            try:
                pk = int(request.POST.get('pk') or 0)
                Supplier.objects.filter(pk=pk).delete()
                messages.success(request, "Yetkazib beruvchi o'chirildi.")
            except (TypeError, ValueError):
                pass
            return redirect('supplier_list')
        # create or edit
        try:
            pk = int(request.POST.get('pk') or 0)
        except (TypeError, ValueError):
            pk = 0
        instance = Supplier.objects.filter(pk=pk).first() if pk else None
        s = instance or Supplier()
        s.name = (request.POST.get('name') or '').strip()[:200]
        s.phone = (request.POST.get('phone') or '').strip()[:40]
        s.address = (request.POST.get('address') or '').strip()[:255]
        s.inn = (request.POST.get('inn') or '').strip()[:14]
        s.contact_person = (request.POST.get('contact_person') or '').strip()[:120]
        s.notes = (request.POST.get('notes') or '').strip()
        s.is_active = bool(request.POST.get('is_active'))
        if not s.name:
            messages.error(request, "Nom kerak.")
            return redirect('supplier_list')
        try:
            s.save()
            verb = 'Yangilandi' if instance else "Qo'shildi"
            messages.success(request, f"{verb}: {s.name}")
        except Exception as e:
            messages.error(request, f"Xato: {e}")
        return redirect('supplier_list')

    suppliers = Supplier.objects.order_by('-is_active', 'name')
    # Per-supplier aggregate: total intakes, last delivery date
    for s in suppliers:
        s.total_qty_received = (Intake.objects.filter(supplier_ref=s)
                                .aggregate(s=Sum('quantity'))['s'] or 0)
        s.last_intake = (Intake.objects.filter(supplier_ref=s)
                         .order_by('-received_at').first())
    return render(request, 'inventory/supplier_list.html',
                  {'suppliers': suppliers})


# ---------- INTAKE SESSION DETAIL ----------

@admin_required
def intake_session_detail(request, pk):
    session = get_object_or_404(
        IntakeSession.objects.select_related('branch', 'supplier', 'received_by')
        .prefetch_related('intakes__variant__product'), pk=pk
    )
    if request.method == 'POST' and 'invoice_image' in request.FILES:
        session.invoice_image = request.FILES['invoice_image']
        session.save(update_fields=['invoice_image'])
        messages.success(request, "Faktura rasmi yuklandi.")
        return redirect('intake_session_detail', pk=session.pk)
    return render(request, 'inventory/intake_session_detail.html', {'session': session})


# ---------- STOCKTAKE (physical count vs system) ----------

@admin_required
def stocktake_list(request):
    """Inventarizatsiyalar + filtrlar + ochiq sessiyalar banner."""
    status = request.GET.get('status') or ''
    branch_id = request.GET.get('branch') or ''

    qs = (Stocktake.objects.select_related('branch', 'started_by', 'applied_by'))
    if status:
        qs = qs.filter(status=status)
    if branch_id:
        try:
            qs = qs.filter(branch_id=int(branch_id))
        except ValueError:
            branch_id = ''
    sessions = list(qs.order_by('-started_at')[:50])

    # Variance summary per session
    for s in sessions:
        agg = s.counts.aggregate(
            variance=Sum(F('counted_qty') - F('system_qty')),
            cnt=Count('id'),
        )
        s.variance_total = agg['variance'] or 0
        s.count_lines = agg['cnt'] or 0

    open_sessions = Stocktake.objects.filter(status=Stocktake.Status.OPEN) \
        .select_related('branch', 'started_by').order_by('-started_at')

    return render(request, 'inventory/stocktake_list.html', {
        'sessions': sessions,
        'open_sessions': open_sessions,
        'status': status,
        'branch_id': branch_id,
        'branches': Branch.objects.filter(is_active=True).order_by('name'),
        'status_choices': Stocktake.Status.choices,
    })


@admin_required
def stocktake_create(request):
    branches = Branch.objects.filter(is_active=True)
    if request.method == 'POST':
        try:
            branch_id = int(request.POST.get('branch') or 0)
        except ValueError:
            branch_id = 0
        branch = Branch.objects.filter(pk=branch_id, is_active=True).first()
        if not branch:
            messages.error(request, "Filial tanlang.")
            return redirect('stocktake_create')
        # Snapshot every variant with stock>0 in this branch
        with transaction.atomic():
            session = Stocktake.objects.create(
                branch=branch, started_by=request.user,
                note=(request.POST.get('note') or '').strip()[:200],
            )
            stocks = BranchStock.objects.filter(branch=branch).select_related('variant')
            counts = [
                StocktakeCount(session=session, variant=s.variant,
                               system_qty=s.stock_count, counted_qty=s.stock_count)
                for s in stocks
            ]
            StocktakeCount.objects.bulk_create(counts)
        messages.success(request, f"Inventarizatsiya boshlandi: {len(counts)} ta variant.")
        return redirect('stocktake_detail', pk=session.pk)
    return render(request, 'inventory/stocktake_create.html', {'branches': branches})


@admin_required
def stocktake_detail(request, pk):
    session = get_object_or_404(
        Stocktake.objects.select_related('branch', 'started_by', 'applied_by'),
        pk=pk,
    )
    counts = (session.counts.select_related('variant__product')
              .order_by('variant__product__code', 'variant__size'))

    if request.method == 'POST' and session.status == Stocktake.Status.OPEN:
        action = request.POST.get('action')
        if action == 'save':
            # Update counted_qty for each row
            with transaction.atomic():
                for c in counts:
                    val = request.POST.get(f'count[{c.pk}]')
                    if val is None or val == '':
                        continue
                    try:
                        c.counted_qty = max(0, int(val))
                        c.save(update_fields=['counted_qty'])
                    except ValueError:
                        pass
            messages.success(request, "Hisoblangan miqdorlar saqlandi.")
            return redirect('stocktake_detail', pk=session.pk)

        if action == 'apply':
            # H6 fix: lock the session row so concurrent apply attempts can't
            # both succeed. select_for_update inside atomic() blocks the second
            # transaction until the first commits.
            with transaction.atomic():
                locked = (Stocktake.objects
                          .select_for_update()
                          .filter(pk=session.pk, status=Stocktake.Status.OPEN)
                          .first())
                if not locked:
                    messages.error(request,
                        "Bu inventarizatsiya allaqachon tasdiqlangan yoki bekor qilingan.")
                    return redirect('stocktake_detail', pk=session.pk)
                # Re-fetch counts under the lock window
                fresh_counts = list(locked.counts.select_related('variant'))
                for c in fresh_counts:
                    bs = BranchStock.objects.filter(
                        variant=c.variant, branch=locked.branch
                    ).select_for_update().first()
                    if bs:
                        bs.stock_count = c.counted_qty
                        bs.save(update_fields=['stock_count'])
                locked.status = Stocktake.Status.APPLIED
                locked.applied_by = request.user
                locked.applied_at = timezone.now()
                locked.save()
            messages.success(request, "Inventarizatsiya tasdiqlandi va ombor yangilandi.")
            return redirect('stocktake_detail', pk=session.pk)

    diffs = sum(1 for c in counts if c.diff != 0)
    return render(request, 'inventory/stocktake_detail.html', {
        'session': session, 'counts': counts, 'diff_count': diffs,
    })


# ---------- TRANSFERS (inter-branch stock moves) ----------

@admin_required
def transfer_list(request):
    """Ko'chirishlar ro'yxati + filtrlar + overdue alert + status counts."""
    status = request.GET.get('status') or ''
    branch_id = request.GET.get('branch') or ''

    qs = (Transfer.objects.select_related('from_branch', 'to_branch',
                                          'created_by', 'received_by')
          .prefetch_related('lines'))

    if status:
        qs = qs.filter(status=status)
    if branch_id:
        try:
            bid = int(branch_id)
            qs = qs.filter(Q(from_branch_id=bid) | Q(to_branch_id=bid))
        except ValueError:
            branch_id = ''

    transfers = list(qs.order_by('-created_at')[:100])

    # Per-status counts (all transfers, not filtered)
    counts = {s: 0 for s, _ in Transfer.Status.choices}
    for s, n in (Transfer.objects.values_list('status')
                 .annotate(n=Count('id')).values_list('status', 'n')):
        counts[s] = n

    # Overdue alert: in_transit older than 3 days
    overdue_cutoff = timezone.now() - timedelta(days=3)
    overdue_transfers = list(Transfer.objects
        .filter(status=Transfer.Status.IN_TRANSIT, dispatched_at__lt=overdue_cutoff)
        .select_related('from_branch', 'to_branch')
        .order_by('dispatched_at')[:5])

    return render(request, 'inventory/transfer_list.html', {
        'transfers': transfers,
        'status': status,
        'branch_id': branch_id,
        'branches': Branch.objects.filter(is_active=True).order_by('name'),
        'status_choices': Transfer.Status.choices,
        'counts': counts,
        'overdue_transfers': overdue_transfers,
    })


@admin_required
def transfer_create(request):
    branches = Branch.objects.filter(is_active=True)
    if request.method == 'POST':
        try:
            from_id = int(request.POST.get('from_branch') or 0)
            to_id = int(request.POST.get('to_branch') or 0)
        except ValueError:
            messages.error(request, "Filial tanlanmagan."); return redirect('transfer_create')
        if from_id == to_id:
            messages.error(request, "Bir xil filialga ko'chirib bo'lmaydi.")
            return redirect('transfer_create')
        from_branch = Branch.objects.filter(pk=from_id).first()
        to_branch = Branch.objects.filter(pk=to_id).first()
        if not from_branch or not to_branch:
            messages.error(request, "Filiallar topilmadi."); return redirect('transfer_create')

        # Parse line items: qty[<variant_id>]
        lines_data = []
        for key, value in request.POST.items():
            if not key.startswith('qty[') or not key.endswith(']'):
                continue
            try:
                variant_id = int(key[4:-1])
                qty = int(value)
            except ValueError:
                continue
            if qty <= 0:
                continue
            lines_data.append((variant_id, qty))

        if not lines_data:
            messages.error(request, "Birorta tovar miqdori kiritilmagan.")
            return redirect('transfer_create')

        # Validate enough stock in source branch
        problems = []
        for variant_id, qty in lines_data:
            stock = BranchStock.objects.filter(variant_id=variant_id, branch=from_branch).first()
            available = stock.stock_count if stock else 0
            if qty > available:
                v = ProductVariant.objects.get(pk=variant_id)
                problems.append(f"{v.product.code} {v.size}/{v.color}: {from_branch.name}da {available} bor, so'rov {qty}")
        if problems:
            for p in problems:
                messages.error(request, p)
            return redirect('transfer_create')

        with transaction.atomic():
            t = Transfer.objects.create(
                from_branch=from_branch, to_branch=to_branch,
                created_by=request.user,
                status=Transfer.Status.IN_TRANSIT,
                dispatched_at=timezone.now(),
                note=(request.POST.get('note') or '').strip()[:200],
            )
            for variant_id, qty in lines_data:
                TransferLine.objects.create(transfer=t, variant_id=variant_id, quantity=qty)
                # Decrement source branch immediately on dispatch
                stock = BranchStock.objects.filter(
                    variant_id=variant_id, branch=from_branch).first()
                stock.stock_count = F('stock_count') - qty
                stock.save()

        messages.success(request, f"Ko'chirish #{t.pk} yo'lga chiqarildi.")
        return redirect('transfer_detail', pk=t.pk)

    return render(request, 'inventory/transfer_create.html', {'branches': branches})


@admin_required
def transfer_detail(request, pk):
    t = get_object_or_404(
        Transfer.objects.select_related('from_branch', 'to_branch',
                                         'created_by', 'received_by')
                         .prefetch_related('lines__variant__product'),
        pk=pk,
    )
    return render(request, 'inventory/transfer_detail.html', {'transfer': t})


@admin_required
def transfer_receive(request, pk):
    t = get_object_or_404(Transfer, pk=pk)
    if t.status != Transfer.Status.IN_TRANSIT:
        messages.warning(request, "Bu ko'chirish allaqachon yopilgan.")
        return redirect('transfer_detail', pk=t.pk)

    if request.method == 'POST':
        with transaction.atomic():
            for line in t.lines.all():
                stock, _ = BranchStock.objects.get_or_create(
                    variant=line.variant, branch=t.to_branch,
                    defaults={'cost_price': 0, 'sale_price': 0},
                )
                stock.stock_count = F('stock_count') + line.quantity
                stock.save()
            t.status = Transfer.Status.RECEIVED
            t.received_by = request.user
            t.received_at = timezone.now()
            t.save()
        messages.success(request, f"Ko'chirish #{t.pk} qabul qilindi.")
        return redirect('transfer_detail', pk=t.pk)

    return render(request, 'inventory/transfer_receive.html', {'transfer': t})


# ---------- SHIFTS ----------

def _open_shift_for(branch):
    """Returns the single open shift for a branch, or None."""
    return Shift.objects.filter(branch=branch, status=Shift.Status.OPEN).first()


@login_required
def shift_open(request):
    branch = _user_branch_or_403(request)
    if branch is None:
        messages.error(request, "Filial biriktirilmagan.")
        return redirect('lookup')

    existing = _open_shift_for(branch)
    if existing:
        messages.info(request, f"Ochiq smen allaqachon bor (#{existing.pk}).")
        return redirect('pos_terminal')

    if request.method == 'POST':
        try:
            opening_cash = max(0, float(request.POST.get('opening_cash') or 0))
        except ValueError:
            opening_cash = 0
        Shift.objects.create(
            branch=branch, opened_by=request.user,
            opening_cash=opening_cash,
            note=(request.POST.get('note') or '').strip()[:200],
        )
        messages.success(request, f"Smen ochildi (boshlang'ich naqd: {opening_cash:,.0f} so'm).")
        return redirect('pos_terminal')

    # Hint: oxirgi yopilgan smen kassasi
    last_closed = (Shift.objects.filter(branch=branch, status=Shift.Status.CLOSED)
                   .order_by('-closed_at').first())
    return render(request, 'inventory/shift_open.html', {
        'branch': branch,
        'last_closed': last_closed,
    })


@login_required
def shift_close(request):
    branch = _user_branch_or_403(request)
    if branch is None:
        return redirect('lookup')

    shift = _open_shift_for(branch)
    if not shift:
        messages.warning(request, "Ochiq smen yo'q.")
        return redirect('pos_terminal')

    if request.method == 'POST':
        try:
            counted = max(0, float(request.POST.get('counted_cash') or 0))
        except ValueError:
            counted = 0
        with transaction.atomic():
            shift.counted_cash = counted
            shift.closed_by = request.user
            shift.closed_at = timezone.now()
            shift.status = Shift.Status.CLOSED
            shift.note = (
                (shift.note + ' | ' if shift.note else '')
                + (request.POST.get('note') or '').strip()
            )[:200]
            shift.save()
        var = shift.variance()
        if var is not None and abs(var) >= 1:
            messages.warning(request,
                f"Smen yopildi. Kassa farqi: {var:+,.0f} so'm "
                f"({'ortiq' if var > 0 else 'kam'}).")
        else:
            messages.success(request, "Smen yopildi. Kassa to'g'ri.")
        return redirect('shift_detail', pk=shift.pk)

    return render(request, 'inventory/shift_close.html', {
        'shift': shift,
        'expected': shift.expected_cash(),
        'cash_sales': shift.cash_sales(),
    })


@login_required
def shift_detail(request, pk):
    shift = get_object_or_404(Shift, pk=pk)
    if not request.user.is_admin() and request.user.branch_id != shift.branch_id:
        return HttpResponseForbidden()
    txns = list(shift.transactions.select_related('sold_by')
                .prefetch_related('lines')
                .order_by('-sold_at')[:200])

    # Payment method breakdown
    pm_breakdown = {}
    for t in txns:
        pm = t.get_payment_method_display()
        if pm not in pm_breakdown:
            pm_breakdown[pm] = {'count': 0, 'total': 0}
        pm_breakdown[pm]['count'] += 1
        pm_breakdown[pm]['total'] += float(t.total)
    pm_list = sorted(pm_breakdown.items(), key=lambda kv: -kv[1]['total'])

    # Hourly activity (24-hour)
    hour_qty = [0] * 24
    hour_revenue = [0] * 24
    for t in txns:
        h = t.sold_at.astimezone(timezone.get_current_timezone()).hour
        hour_qty[h] += 1
        hour_revenue[h] += float(t.total)
    # Trim to active range
    active_hours = [(h, q, r) for h, (q, r) in enumerate(zip(hour_qty, hour_revenue)) if q]

    # Top products in this shift
    top_prod = {}
    for t in txns:
        for ln in t.lines.all():
            key = ln.variant.product.code
            if key not in top_prod:
                top_prod[key] = {
                    'code': key,
                    'name': ln.variant.product.name,
                    'qty': 0,
                    'revenue': 0,
                }
            top_prod[key]['qty'] += ln.quantity
            top_prod[key]['revenue'] += float(ln.total)
    top_products = sorted(top_prod.values(), key=lambda x: -x['qty'])[:5]

    # Variance value
    variance_value = shift.variance() if callable(getattr(shift, 'variance', None)) else None

    return render(request, 'inventory/shift_detail.html', {
        'shift': shift,
        'txns': txns,
        'cash_sales': shift.cash_sales(),
        'expected': shift.expected_cash(),
        'pm_list': pm_list,
        'hour_labels': [f'{h:02d}:00' for h, _, _ in active_hours],
        'hour_qty': [q for _, q, _ in active_hours],
        'hour_revenue': [r for _, _, r in active_hours],
        'top_products': top_products,
        'variance_value': variance_value,
    })


@admin_required
def shift_list(request):
    """Smenlar ro'yxati + filtrlar + ochiq smenlar banner."""
    branch_id = request.GET.get('branch') or ''
    status = request.GET.get('status') or ''
    seller_id = request.GET.get('seller') or ''
    date_from = request.GET.get('date_from') or ''
    date_to = request.GET.get('date_to') or ''

    qs = Shift.objects.select_related('branch', 'opened_by', 'closed_by')

    if branch_id:
        try:
            qs = qs.filter(branch_id=int(branch_id))
        except ValueError:
            branch_id = ''
    if status:
        qs = qs.filter(status=status)
    if seller_id:
        try:
            qs = qs.filter(opened_by_id=int(seller_id))
        except ValueError:
            seller_id = ''
    try:
        if date_from:
            qs = qs.filter(opened_at__date__gte=datetime.strptime(date_from, '%Y-%m-%d').date())
    except ValueError:
        date_from = ''
    try:
        if date_to:
            qs = qs.filter(opened_at__date__lte=datetime.strptime(date_to, '%Y-%m-%d').date())
    except ValueError:
        date_to = ''

    shifts = list(qs.order_by('-opened_at')[:100])

    # Ochiq smenlar — har doim alohida ko'rsatamiz
    open_shifts = (Shift.objects.filter(status=Shift.Status.OPEN)
                   .select_related('branch', 'opened_by')
                   .order_by('-opened_at'))

    # Variance stats — total positive vs negative variance from closed shifts.
    # Shift.variance is a method, so call it (cache per row).
    closed = []
    for s in shifts:
        if s.status == Shift.Status.CLOSED:
            v = s.variance() if callable(s.variance) else s.variance
            s._variance_value = v
            closed.append(s)
    variance_positive = sum(float(s._variance_value) for s in closed
                            if s._variance_value is not None and s._variance_value > 0)
    variance_negative = sum(float(s._variance_value) for s in closed
                            if s._variance_value is not None and s._variance_value < 0)
    avg_variance = (sum(float(s._variance_value) for s in closed
                        if s._variance_value is not None) / len(closed)) if closed else 0

    return render(request, 'inventory/shift_list.html', {
        'shifts': shifts,
        'open_shifts': open_shifts,
        'variance_positive': variance_positive,
        'variance_negative': variance_negative,
        'avg_variance': avg_variance,
        'branch_id': branch_id,
        'status': status,
        'seller_id': seller_id,
        'date_from': date_from,
        'date_to': date_to,
        'branches': Branch.objects.filter(is_active=True).order_by('name'),
        'sellers': User.objects.filter(is_active=True).order_by('username'),
    })


# ---------- POS TERMINAL ----------

POS_BRANCH_SESSION_KEY = 'pos_branch_id'


def _user_branch_or_403(request):
    """Sellers are locked to their assigned branch. Admins can pick
    which branch the POS operates on via a dropdown — the choice is
    stored in the session under POS_BRANCH_SESSION_KEY.

    Fallback order for admins:
      1) session-selected branch
      2) ?branch_id= query (used once by pos_terminal to set the session)
      3) admin's own assigned branch (if set)
      4) branch of admin's own open shift
      5) any open shift's branch
      6) first active branch
    """
    if not request.user.is_admin():
        return request.user.branch

    sb_id = request.session.get(POS_BRANCH_SESSION_KEY)
    if sb_id:
        b = Branch.objects.filter(pk=sb_id, is_active=True).first()
        if b:
            return b

    q_id = request.GET.get('branch_id')
    if q_id:
        try:
            b = Branch.objects.filter(pk=int(q_id), is_active=True).first()
            if b:
                return b
        except ValueError:
            pass

    if request.user.branch_id:
        return request.user.branch

    own_shift = Shift.objects.filter(
        opened_by=request.user, status=Shift.Status.OPEN
    ).select_related('branch').order_by('-opened_at').first()
    if own_shift:
        return own_shift.branch

    any_shift = Shift.objects.filter(
        status=Shift.Status.OPEN
    ).select_related('branch').order_by('-opened_at').first()
    if any_shift:
        return any_shift.branch

    return Branch.objects.filter(is_active=True).first()


@login_required
def pos_customer_display(request):
    """Mijoz tomonidagi ekran — POS bilan BroadcastChannel orqali sinxron.
    Faqat o'qish (interaktiv emas). Iste'mol uchun: 2-monitor yoki ikkinchi tab."""
    branch = _user_branch_or_403(request)
    return render(request, 'inventory/pos_display.html', {
        'branch': branch,
        'shop_name': 'yurit',  # TODO: shop name from settings/branding
    })


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

    recent_txns = (SaleTransaction.objects.filter(branch=branch, shift=open_shift)
                   .select_related('sold_by')
                   .prefetch_related('lines')
                   .order_by('-sold_at')[:5])

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
        'shift': open_shift,
        'recent_txns': recent_txns,
        'favorites': favorites,
        'parked_sales': parked_sales,
        'payment_methods': SaleTransaction.PaymentMethod.choices,
        'payment_providers': payment_providers,
        'branches_list': branches_list,
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
    if not product:
        _vm = (ProductVariant.objects.filter(barcode=q.strip())
               .select_related('product').first())
        if _vm:
            product = _vm.product
            matched_variant_id = _vm.id

    if not product:
        # Name search
        matches = list(Product.objects.filter(
            Q(name__icontains=q) | Q(category__name__icontains=q)
        )[:8])
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


@login_required
def pos_checkout(request):
    """POST /pos/checkout/ with JSON body:
      { lines: [{stock_id, qty, sale_price}],
        payment_method: 'cash'|'card'|'transfer'|'mixed',
        customer_name, customer_phone, note }
    Creates SaleTransaction + Sales atomically. Returns {ok, txn_id, receipt_url}.
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

    lines = data.get('lines') or []
    if not lines:
        return JsonResponse({'ok': False, 'error': 'savat boʻsh'}, status=400)

    payment_method = (data.get('payment_method') or 'cash').strip()
    # Mixed payment: client sends [{method, amount}]. Validate sum after we
    # know the total. Keep as list of dicts for storage.
    payment_breakdown = data.get('payment_breakdown') or []
    if not isinstance(payment_breakdown, list):
        payment_breakdown = []
    customer_name = (data.get('customer_name') or '').strip()[:120]
    customer_phone = (data.get('customer_phone') or '').strip()[:40]
    note = (data.get('note') or '').strip()[:200]
    try:
        order_discount = max(0, float(data.get('order_discount') or 0))
    except (ValueError, TypeError):
        order_discount = 0
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
            line_discount = max(0, float(ln.get('line_discount') or 0))
            price = float(ln['sale_price'])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({'ok': False, 'error': 'noto\'g\'ri qator'}, status=400)
        if qty <= 0 or price < 0:
            return JsonResponse({'ok': False, 'error': 'qty/narx noto\'g\'ri'}, status=400)
        parsed_lines.append({'sid': sid, 'qty': qty, 'price': price, 'ld': line_discount})

    # Resolve / auto-create Customer by phone (most reliable key)
    customer = None
    if customer_phone:
        cleaned_phone = ''.join(c for c in customer_phone if c.isdigit() or c == '+')
        if cleaned_phone:
            customer = Customer.objects.filter(phone=cleaned_phone).first()
            if not customer:
                customer = Customer.objects.create(
                    phone=cleaned_phone, name=customer_name,
                )
            elif customer_name and not customer.name:
                customer.name = customer_name
                customer.save(update_fields=['name'])

    open_shift = _open_shift_for(branch)

    # Sanitize breakdown (drop bad entries, round to nearest som)
    clean_breakdown = []
    for entry in payment_breakdown:
        try:
            m = (entry.get('method') or '').strip()
            a = float(entry.get('amount') or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if not m or a <= 0:
            continue
        clean_breakdown.append({'method': m, 'amount': round(a, 2)})

    # If mixed but breakdown empty, fallback to single-method
    if payment_method == 'mixed' and not clean_breakdown:
        payment_method = 'cash'

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
            for ln in parsed_lines:
                stock = locked[ln['sid']]
                if ln['qty'] > stock.stock_count:
                    err = (f"{stock.variant.product.code} {stock.variant.size}/{stock.variant.color}: "
                           f"omborda faqat {stock.stock_count} ta bor, soʻrov {ln['qty']}")
                    # C2: if this is an offline-queue replay, alert admin via Telegram
                    # and log to AuditLog — kassir's offline sale was rejected at sync time.
                    if data.get('is_offline_replay'):
                        try:
                            from .notifications import send_telegram
                            send_telegram(
                                f"⚠️ <b>Offline sotuv konflikti</b>\n"
                                f"Filial: {branch.name}\n"
                                f"Kassir: {request.user.username}\n"
                                f"Mahsulot: {stock.variant.product.code} "
                                f"{stock.variant.size}/{stock.variant.color}\n"
                                f"Soʻrov: {ln['qty']} dona · Omborda: {stock.stock_count}\n"
                                f"Pul allaqachon kassada bo'lishi mumkin — manual reconcile kerak."
                            )
                            AuditLog.objects.create(
                                user=request.user,
                                username_snapshot=request.user.username,
                                action=AuditLog.Action.UPDATE,
                                model_name='OfflineConflict',
                                object_repr=err[:200],
                            )
                        except Exception:
                            pass
                    raise _CheckoutAbort({'ok': False, 'error': err})

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
                discount_reason=discount_reason,
                shift=open_shift,
            )
            for ln in parsed_lines:
                stock = locked[ln['sid']]
                stock.stock_count = F('stock_count') - ln['qty']
                stock.save()
                Sale.objects.create(
                    transaction=txn,
                    variant=stock.variant, branch=stock.branch,
                    quantity=ln['qty'],
                    sale_price=ln['price'],
                    cost_at_sale=stock.cost_price,
                    line_discount=ln['ld'],
                    sold_by=request.user,
                )
    except _CheckoutAbort as e:
        return JsonResponse(e.payload, status=e.status)

    # Best-effort fiscal (noop unless provider configured)
    from .fiscal import submit_for_transaction
    submit_for_transaction(txn)

    # Best-effort SMS receipt (noop unless provider + opt-in)
    sms_result = None
    if data.get('send_sms') and customer_phone:
        from .sms import send_receipt
        sms_result = send_receipt(txn, customer_phone)

    return JsonResponse({
        'ok': True,
        'txn_id': txn.pk,
        'receipt_url': f'/transaction/{txn.pk}/?autoprint=1',
        'total': float(txn.total),
        'item_count': txn.item_count,
        'sms': sms_result,
    })


# ---------- POS PARK / HOLD ----------

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

    Hozir stub: keladi-keladi log'ga yoziladi va ref_code yoki amount bo'yicha
    mos PaymentIntent topilsa 'paid' deb belgilanadi. Real Payme/Click ulanganda
    bu yerga signature verification qo'shiladi."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    body_raw = request.body
    try:
        payload = _json.loads(body_raw.decode('utf-8')) if body_raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}

    logger.info('[payments webhook %s] %s', provider, payload)

    # Eng oson search: ref_code yoki amount + provider bo'yicha
    ref_code = (payload.get('ref_code') or payload.get('comment')
                or payload.get('memo') or '').strip().upper()
    intent = None
    if ref_code:
        intent = (PaymentIntent.objects
                  .filter(provider=provider, ref_code=ref_code,
                          status=PaymentIntent.Status.PENDING)
                  .order_by('-created_at').first())
    if not intent:
        # Amount + recent window fallback
        try:
            amt = float(payload.get('amount') or 0)
        except (TypeError, ValueError):
            amt = 0
        if amt > 0:
            since = timezone.now() - timedelta(minutes=30)
            intent = (PaymentIntent.objects
                      .filter(provider=provider, amount=amt,
                              status=PaymentIntent.Status.PENDING,
                              created_at__gte=since)
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

    return JsonResponse({
        'ok': True,
        'promotions': applied,
        'total_discount': round(total_discount, 2),
    })


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
    from .payments import get_provider
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

    refunded_total = 0
    refunded_qty = 0
    with transaction.atomic():
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
            already = sale.returns.aggregate(s=Sum('quantity'))['s'] or 0
            remaining = sale.quantity - already
            if qty > remaining:
                return JsonResponse({
                    'ok': False,
                    'error': f"{sale.variant.product.code}: faqat {remaining} dona qaytarish mumkin",
                }, status=400)
            stock = BranchStock.objects.filter(
                variant=sale.variant, branch=sale.branch
            ).first()
            if stock:
                stock.stock_count = F('stock_count') + qty
                stock.save()
            Return.objects.create(
                sale=sale, shift=open_shift,
                quantity=qty, reason=reason,
                refunded_by=request.user,
            )
            refunded_qty += qty
            # M8 fix: use sale.total (after line_discount) for accurate refund amount
            if sale.quantity > 0:
                per_unit = float(sale.total) / sale.quantity
            else:
                per_unit = float(sale.sale_price)
            refunded_total += qty * per_unit
    return JsonResponse({
        'ok': True,
        'refunded_qty': refunded_qty,
        'refunded_total': refunded_total,
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


# ---------- SALE ----------

@login_required
def sale_create(request, stock_id):
    stock = get_object_or_404(BranchStock.objects.select_related('variant__product', 'branch'),
                              pk=stock_id)
    # Sotuvchilar faqat o'z filialida sotishi mumkin
    if not request.user.is_admin():
        if request.user.branch_id != stock.branch_id:
            return HttpResponseForbidden(
                "<h3>Bu filialda sotishga ruxsat yo'q.</h3>"
            )

    if request.method == 'POST':
        form = SaleForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            qty = cd['quantity']
            if qty > stock.stock_count:
                messages.error(request,
                    f"Omborda yetarli emas. Mavjud: {stock.stock_count}, so'rov: {qty}.")
                return redirect('sale_create', stock_id=stock.id)
            with transaction.atomic():
                stock.stock_count = F('stock_count') - qty
                stock.save()
                # Wrap every sale in a SaleTransaction so the OFD/fiscal
                # submission flow is the same for single-item and cart sales.
                txn = SaleTransaction.objects.create(
                    branch=stock.branch,
                    sold_by=request.user,
                    payment_method=SaleTransaction.PaymentMethod.CASH,
                    note=cd.get('note') or '',
                )
                Sale.objects.create(
                    transaction=txn,
                    variant=stock.variant, branch=stock.branch,
                    quantity=qty, sale_price=cd['sale_price'],
                    cost_at_sale=stock.cost_price,
                    note=cd.get('note') or '', sold_by=request.user,
                )

            # Best-effort fiscal submission (no-op when no OFD configured).
            from .fiscal import submit_for_transaction
            submit_for_transaction(txn)

            messages.success(request,
                f"Sotildi: {qty} dona × {cd['sale_price']} so'm ({stock.branch.name}).")
            return redirect('lookup')
    else:
        prefill_price = stock.sale_price if stock.sale_price > 0 else \
            stock.variant.product.default_sale_price
        form = SaleForm(initial={'sale_price': prefill_price})
    return render(request, 'inventory/sale_form.html', {
        'form': form, 'stock': stock,
    })


# ---------- BRANCHES ----------

@admin_required
def branch_list(request):
    """Filiallar ro'yxati: 30 kunlik tushum + P&L + xodimlar."""
    since_30d = timezone.now() - timedelta(days=30)
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    branches = list(Branch.objects.annotate(
        stock_total=Coalesce(Sum('stocks__stock_count'), 0),
        staff_count=Count('staff', distinct=True),
        stock_value=Coalesce(Sum(
            ExpressionWrapper(F('stocks__stock_count') * F('stocks__cost_price'),
                              output_field=DecimalField(max_digits=14, decimal_places=2))
        ), 0, output_field=DecimalField(max_digits=14, decimal_places=2)),
    ).order_by('-is_active', 'name'))

    # 30-day stats per branch
    stats_map = {}
    for row in (Sale.objects.filter(sold_at__gte=since_30d)
                .values('branch_id')
                .annotate(rev=Sum(rev_expr), cost=Sum(cost_expr),
                          txns=Count('transaction', distinct=True),
                          qty=Sum('quantity'))):
        stats_map[row['branch_id']] = row

    for br in branches:
        s = stats_map.get(br.id, {})
        rev = float(s.get('rev') or 0)
        cost = float(s.get('cost') or 0)
        period_fraction = 30 / 30.0  # whole 30-day window
        fixed = float((br.monthly_rent or 0) + (br.monthly_other_costs or 0)) * period_fraction
        gross = rev - cost
        net = gross - fixed
        br.m_revenue = rev
        br.m_cost = cost
        br.m_gross = gross
        br.m_fixed = fixed
        br.m_net = net
        br.m_margin = (net / rev * 100) if rev else 0
        br.m_txns = s.get('txns') or 0
        br.m_qty = s.get('qty') or 0

    total_branches = sum(1 for b in branches if b.is_active)
    total_revenue = sum(float(b.m_revenue) for b in branches)
    total_stock_value = sum(float(b.stock_value or 0) for b in branches)

    return render(request, 'inventory/branch_list.html', {
        'branches': branches,
        'total_branches': total_branches,
        'total_revenue': total_revenue,
        'total_stock_value': total_stock_value,
    })


@admin_required
def branch_create(request):
    if request.method == 'POST':
        form = BranchForm(request.POST)
        if form.is_valid():
            br = form.save()
            messages.success(request, f"Filial yaratildi: {br.name}")
            return redirect('branch_list')
    else:
        form = BranchForm()
    return render(request, 'inventory/branch_form.html', {
        'form': form, 'title': "Yangi filial",
    })


@admin_required
def branch_edit(request, pk):
    branch = get_object_or_404(Branch, pk=pk)
    if request.method == 'POST':
        form = BranchForm(request.POST, instance=branch)
        if form.is_valid():
            form.save()
            messages.success(request, 'Filial yangilandi.')
            return redirect('branch_list')
    else:
        form = BranchForm(instance=branch)
    return render(request, 'inventory/branch_form.html', {
        'form': form, 'title': f'Tahrirlash — {branch.name}', 'branch': branch,
    })


# ---------- USERS ----------

@admin_required
def user_list(request):
    """Foydalanuvchilar: 30 kunlik performans + oxirgi kirish."""
    role_filter = request.GET.get('role') or ''
    branch_id = request.GET.get('branch') or ''
    only_active = request.GET.get('active') == '1'

    users = User.objects.select_related('branch')
    if role_filter:
        users = users.filter(role=role_filter)
    if branch_id:
        try:
            users = users.filter(branch_id=int(branch_id))
        except ValueError:
            branch_id = ''
    if only_active:
        users = users.filter(is_active=True)

    users = list(users.order_by('-is_active', 'username'))

    # 30-kunlik sotuv stat'lari
    since = timezone.now() - timedelta(days=30)
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    stats = (Sale.objects.filter(sold_at__gte=since)
             .values('sold_by_id')
             .annotate(
                 revenue=Sum(rev_expr),
                 qty=Sum('quantity'),
                 n=Count('transaction', distinct=True),
             ))
    stat_map = {s['sold_by_id']: s for s in stats}
    for u in users:
        s = stat_map.get(u.id, {})
        u.s_revenue = float(s.get('revenue') or 0)
        u.s_qty = s.get('qty') or 0
        u.s_txns = s.get('n') or 0
        pct = float(u.commission_percent or 0)
        u.s_commission = u.s_revenue * pct / 100 if pct else 0

    return render(request, 'inventory/user_list.html', {
        'users': users,
        'role_filter': role_filter,
        'branch_id': branch_id,
        'only_active': only_active,
        'branches': Branch.objects.filter(is_active=True).order_by('name'),
        'role_choices': User.Role.choices,
    })


@admin_required
def user_create(request):
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            # H7 fix: audit account creation (password set is implicit)
            AuditLog.objects.create(
                user=request.user,
                username_snapshot=request.user.username,
                action=AuditLog.Action.CREATE,
                model_name='User',
                object_id=str(user.pk),
                object_repr=f'{user.username} ({user.get_role_display()})',
            )
            messages.success(request, f"Foydalanuvchi yaratildi: {user.username}")
            return redirect('user_list')
    else:
        form = UserCreateForm()
    return render(request, 'inventory/user_form.html', {
        'form': form, 'title': "Yangi foydalanuvchi", 'is_create': True,
    })


@admin_required
def user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        form = UserEditForm(request.POST, instance=target)
        if form.is_valid():
            new_password = (form.cleaned_data.get('new_password') or '').strip()
            user = form.save()
            # H7 fix: explicit audit row when password changes — never log the
            # password itself, only the fact that it changed (and by whom)
            if new_password:
                AuditLog.objects.create(
                    user=request.user,
                    username_snapshot=request.user.username,
                    action=AuditLog.Action.UPDATE,
                    model_name='UserPassword',
                    object_id=str(user.pk),
                    object_repr=f'Parol o\'zgartirildi: {user.username}',
                )
            messages.success(request, 'Foydalanuvchi yangilandi.')
            return redirect('user_list')
    else:
        form = UserEditForm(instance=target)
    return render(request, 'inventory/user_form.html', {
        'form': form, 'title': f'Tahrirlash — {target.username}',
        'target_user': target, 'is_create': False,
    })


# ---------- CATEGORIES ----------

@admin_required
def category_list(request):
    if request.method == 'POST':
        action = request.POST.get('action') or 'create'
        if action == 'delete':
            try:
                pk = int(request.POST.get('pk') or 0)
            except (TypeError, ValueError):
                pk = 0
            cat = Category.objects.filter(pk=pk).first()
            if cat:
                n_products = cat.products.count()
                move_to = (request.POST.get('move_to') or '').strip()
                if n_products > 0:
                    # Mahsulotlarni avval tanlangan joyga ko'chiramiz
                    if move_to == 'none':
                        cat.products.update(category=None)
                        messages.info(request,
                            f"{n_products} ta mahsulot kategoriyasiz qoldi.")
                    elif move_to.isdigit() and Category.objects.filter(
                            pk=int(move_to)).exclude(pk=cat.pk).exists():
                        target = Category.objects.get(pk=int(move_to))
                        cat.products.update(category=target)
                        messages.info(request,
                            f"{n_products} ta mahsulot \"{target.name}\" ga ko'chirildi.")
                    else:
                        messages.error(request,
                            f"\"{cat.name}\" o'chirilmadi: unda {n_products} ta "
                            "mahsulot bor — ular ko'chiriladigan joyni tanlang.")
                        return redirect('category_list')
                name = cat.name
                cat.delete()
                AuditLog.objects.create(
                    user=request.user,
                    username_snapshot=request.user.username,
                    action=AuditLog.Action.DELETE,
                    model_name='Category', object_id=str(pk),
                    object_repr=name)
                messages.success(request, f"\"{name}\" o'chirildi.")
            return redirect('category_list')
        if action == 'edit':
            try:
                pk = int(request.POST.get('pk') or 0)
            except (TypeError, ValueError):
                pk = 0
            cat = Category.objects.filter(pk=pk).first()
            if cat:
                new_name = (request.POST.get('name') or '').strip()[:120]
                new_prefix = (request.POST.get('prefix') or '').strip()[:6]
                if not new_name:
                    messages.error(request, "Nom kerak.")
                else:
                    cat.name = new_name
                    if new_prefix:
                        cat.prefix = new_prefix.upper()
                    cat.save()
                    messages.success(request, f"\"{cat.name}\" yangilandi.")
            return redirect('category_list')
        # create
        form = CategoryForm(request.POST)
        if form.is_valid():
            cat = form.save()
            messages.success(request, f"\"{cat.name}\" qo'shildi (prefiks: {cat.prefix}).")
            return redirect('category_list')
    else:
        form = CategoryForm()

    # Filter + search
    q = (request.GET.get('q') or '').strip()
    categories = Category.objects.all()
    if q:
        categories = categories.filter(name__icontains=q)

    # 30-kunlik sotuvlar va revenue agregati
    since_30d = timezone.now() - timedelta(days=30)
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    cat_stats = {}
    for row in (Sale.objects.filter(sold_at__gte=since_30d)
                .values('variant__product__category_id')
                .annotate(rev=Sum(rev_expr), cost=Sum(cost_expr),
                          qty=Sum('quantity'))):
        cat_stats[row['variant__product__category_id']] = {
            'rev': float(row['rev'] or 0),
            'cost': float(row['cost'] or 0),
            'qty': row['qty'] or 0,
        }

    # Per-category stock + value
    stock_stats = {}
    for row in (BranchStock.objects
                .values('variant__product__category_id')
                .annotate(stock=Sum('stock_count'),
                          value=Sum(ExpressionWrapper(
                              F('stock_count') * F('cost_price'),
                              output_field=DecimalField(max_digits=14, decimal_places=2))))):
        stock_stats[row['variant__product__category_id']] = {
            'stock': row['stock'] or 0,
            'value': float(row['value'] or 0),
        }

    categories = list(categories.annotate(product_count=Count('products')).order_by('name'))
    for c in categories:
        s = cat_stats.get(c.id, {})
        st = stock_stats.get(c.id, {})
        c.s_rev = s.get('rev', 0)
        c.s_cost = s.get('cost', 0)
        c.s_qty = s.get('qty', 0)
        c.s_profit = c.s_rev - c.s_cost
        c.s_margin = (c.s_profit / c.s_rev * 100) if c.s_rev else 0
        c.s_stock = st.get('stock', 0)
        c.s_value = st.get('value', 0)

    # Top-level stats
    total_categories = len(categories)
    no_category_count = Product.objects.filter(category__isnull=True).count()
    top_cat = max(categories, key=lambda c: c.s_rev, default=None)

    return render(request, 'inventory/category_list.html', {
        'categories': categories, 'form': form,
        'q': q,
        'total_categories': total_categories,
        'no_category_count': no_category_count,
        'top_cat': top_cat,
    })


# ---------- SALES LIST ----------

@admin_required
def sales_list(request):
    """Sotuvlar ro'yxati: filterlar + kunlik jami + CSV export."""
    # Filterlar
    q = (request.GET.get('q') or '').strip()
    date_from_raw = request.GET.get('date_from') or ''
    date_to_raw = request.GET.get('date_to') or ''
    branch_id = request.GET.get('branch') or ''
    seller_id = request.GET.get('seller') or ''
    payment_method = request.GET.get('payment_method') or ''
    export = request.GET.get('export') == 'csv'

    qs = Sale.objects.select_related(
        'variant__product', 'branch', 'sold_by', 'transaction'
    ).annotate(_returned=Coalesce(Sum('returns__quantity'), 0))

    try:
        if date_from_raw:
            df = datetime.strptime(date_from_raw, '%Y-%m-%d').date()
            qs = qs.filter(sold_at__date__gte=df)
    except ValueError:
        date_from_raw = ''
    try:
        if date_to_raw:
            dt = datetime.strptime(date_to_raw, '%Y-%m-%d').date()
            qs = qs.filter(sold_at__date__lte=dt)
    except ValueError:
        date_to_raw = ''
    if branch_id:
        try:
            qs = qs.filter(branch_id=int(branch_id))
        except ValueError:
            branch_id = ''
    if seller_id:
        try:
            qs = qs.filter(sold_by_id=int(seller_id))
        except ValueError:
            seller_id = ''
    if payment_method:
        qs = qs.filter(transaction__payment_method=payment_method)
    if q:
        qs = qs.filter(
            Q(variant__product__name__icontains=q)
            | Q(variant__product__code__icontains=q)
            | Q(transaction__customer_name__icontains=q)
            | Q(transaction__customer_phone__icontains=q)
        )

    qs = qs.order_by('-sold_at')

    if export:
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename="sales.csv"'
        resp.write('﻿')  # BOM for Excel
        w = csv.writer(resp)
        w.writerow(['Sana', 'Vaqt', 'Kod', 'Mahsulot', 'Variant', 'Filial',
                    'Sotuvchi', 'Soni', 'Narx', 'Chegirma', 'Jami',
                    'To\'lov turi', 'Mijoz'])
        for s in qs[:10000]:
            t = s.transaction
            w.writerow([
                s.sold_at.strftime('%Y-%m-%d'),
                s.sold_at.strftime('%H:%M'),
                s.variant.product.code,
                s.variant.product.name,
                f'{s.variant.size}/{s.variant.color}',
                s.branch.name,
                s.sold_by.username,
                s.quantity,
                float(s.sale_price),
                float(s.line_discount or 0),
                float(s.total),
                t.get_payment_method_display() if t else '',
                t.customer_name if t else '',
            ])
        return resp

    sales = list(qs[:300])
    total = sum(s.total for s in sales)
    qty_total = sum(s.quantity for s in sales)

    # Group by date for daily subtotals (for display only)
    from collections import OrderedDict
    daily = OrderedDict()
    for s in sales:
        d = s.sold_at.date()
        if d not in daily:
            daily[d] = {'date': d, 'total': 0, 'qty': 0, 'count': 0}
        daily[d]['total'] += float(s.total)
        daily[d]['qty'] += s.quantity
        daily[d]['count'] += 1
    daily_list = list(daily.values())

    return render(request, 'inventory/sales_list.html', {
        'sales': sales,
        'total': total,
        'qty_total': qty_total,
        'daily_list': daily_list,
        # Filter state
        'q': q,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
        'branch_id': branch_id,
        'seller_id': seller_id,
        'payment_method': payment_method,
        # Choices
        'branches': Branch.objects.filter(is_active=True).order_by('name'),
        'sellers': User.objects.filter(is_active=True).order_by('username'),
        'payment_methods': SaleTransaction.PaymentMethod.choices,
    })


# ---------- REPORTS ----------

def _resolve_period(period, date_from, date_to):
    """Davr bo'yicha (start_dt, end_dt) qaytaradi. end_dt = davrdan keyingi kun."""
    today = timezone.localdate()
    if period == 'today':
        start = today
        end = today + timedelta(days=1)
    elif period == 'yesterday':
        start = today - timedelta(days=1)
        end = today
    elif period == 'week':
        start = today - timedelta(days=7)
        end = today + timedelta(days=1)
    elif period == 'month':
        start = today - timedelta(days=30)
        end = today + timedelta(days=1)
    elif period == 'this_month':
        start = today.replace(day=1)
        end = today + timedelta(days=1)
    elif period == 'custom':
        start = date_from or today
        end = (date_to or today) + timedelta(days=1)
    else:
        start = today - timedelta(days=7)
        end = today + timedelta(days=1)
    tz = timezone.get_current_timezone()
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=tz)
    end_dt = datetime.combine(end, datetime.min.time()).replace(tzinfo=tz)
    return start, end - timedelta(days=1), start_dt, end_dt


PIVOT_DIM_CONFIG = {
    # Dimensions that need an annotation to project a value out of sold_at
    'date':     {'label': 'Sana',       'expr': TruncDate('sold_at'),     'field': 'pv_date'},
    'week':     {'label': 'Hafta',      'expr': TruncWeek('sold_at'),     'field': 'pv_week'},
    'month':    {'label': 'Oy',         'expr': TruncMonth('sold_at'),    'field': 'pv_month'},
    'weekday':  {'label': 'Hafta kuni', 'expr': ExtractWeekDay('sold_at'),'field': 'pv_wd'},
    # Direct relational fields
    'branch':   {'label': 'Filial',       'field': 'branch__name'},
    'category': {'label': 'Kategoriya',   'field': 'variant__product__category__name'},
    'product':  {'label': 'Mahsulot',     'field': 'variant__product__name'},
    'seller':   {'label': 'Sotuvchi',     'field': 'sold_by__username'},
    'payment':  {'label': "To'lov turi",  'field': 'transaction__payment_method'},
}

# Auto-hierarchy: coarser categories first when user picks multiple row dims.
# Lower number = closer to the root of the visual tree.
PIVOT_DIM_ORDER = {
    'branch':   10,
    'seller':   20,
    'category': 30,
    'product':  40,
    'payment':  50,
    'month':    60,
    'week':     70,
    'date':     80,
    'weekday':  90,
}

# Django ExtractWeekDay: Sunday=1, Monday=2, ..., Saturday=7
PIVOT_WEEKDAY_NAMES = {1: 'Yakshanba', 2: 'Dushanba', 3: 'Seshanba',
                       4: 'Chorshanba', 5: 'Payshanba',
                       6: 'Juma',      7: 'Shanba'}


def _pivot_format_key(dim, val):
    """Render a row/column key for display in the pivot table."""
    if val is None:
        return '—'
    if dim == 'weekday':
        return PIVOT_WEEKDAY_NAMES.get(int(val), str(val))
    if dim in ('date', 'week'):
        return val.strftime('%Y-%m-%d') if hasattr(val, 'strftime') else str(val)
    if dim == 'month':
        return val.strftime('%Y-%m') if hasattr(val, 'strftime') else str(val)
    if dim == 'payment':
        labels = dict(SaleTransaction._meta.get_field('payment_method').choices)
        return labels.get(val, val)
    return str(val)


def _sort_dim_value(dim, v):
    """Sort key for distinct row/col values within a dimension."""
    if v is None:
        return (1, '')
    if dim == 'weekday':
        return (0, int(v))
    if dim in ('date', 'week', 'month') and hasattr(v, 'isoformat'):
        return (0, v.isoformat())
    return (0, str(v))


def _build_pivot(sale_qs, rows_dims, cols_dim, metric):
    """Aggregate sale_qs by N row dimensions and (optional) 1 col dimension.

    rows_dims: list[str] — already sorted by auto-hierarchy (coarsest first).
    Returns:
      mode '1d'  → items: [{keys, value, pct}, ...]  + total + headers
      mode '2d'  → headers (row + col), matrix rows w/ keys+cells+total,
                   col_totals, grand_total
    """
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2))
    profit_expr = ExpressionWrapper(
        (F('sale_price') - F('cost_at_sale')) * F('quantity') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2))
    annot = {
        'revenue': Sum(rev_expr),
        'qty':     Sum('quantity'),
        'count':   Count('transaction_id', distinct=True),
        'profit':  Sum(profit_expr),
    }

    extra = {}
    group_fields = []
    all_dims = list(rows_dims) + ([cols_dim] if cols_dim else [])
    for dim in all_dims:
        if not dim or dim not in PIVOT_DIM_CONFIG:
            continue
        cfg = PIVOT_DIM_CONFIG[dim]
        if 'expr' in cfg:
            extra[cfg['field']] = cfg['expr']
        group_fields.append(cfg['field'])

    qs = sale_qs.annotate(**extra) if extra else sale_qs
    agg = list(qs.values(*group_fields).annotate(**annot).order_by())

    def m(r): return r.get(metric) or 0

    row_fields = [PIVOT_DIM_CONFIG[d]['field'] for d in rows_dims]
    row_labels = [PIVOT_DIM_CONFIG[d]['label'] for d in rows_dims]

    if not cols_dim:
        # 1-D group (possibly N-level nested rows)
        bucket = {}  # tuple-of-keys → metric value
        for r in agg:
            key = tuple(r[f] for f in row_fields)
            bucket[key] = bucket.get(key, 0) + m(r)

        # Sort by row_dims order (parent first), within each level use _sort_dim_value
        items_keys = sorted(bucket.keys(),
                            key=lambda k: tuple(_sort_dim_value(rows_dims[i], k[i])
                                                for i in range(len(k))))
        total = sum(bucket.values())
        items = []
        prev_keys = [None] * len(row_fields)
        for k in items_keys:
            display = []
            for i, raw in enumerate(k):
                fmt = _pivot_format_key(rows_dims[i], raw)
                # Hide repeated parent values from adjacent rows for clarity
                if prev_keys[i] == raw and i < len(k) - 1:
                    display.append('')
                else:
                    display.append(fmt)
            prev_keys = list(k)
            value = bucket[k]
            items.append({'keys': display, 'value': value,
                          'pct': (value / total * 100) if total else 0})
        return {
            'mode': '1d',
            'row_labels': row_labels,
            'items':      items,
            'total':      total,
        }

    # 2-D pivot with N-level rows
    cols_field = PIVOT_DIM_CONFIG[cols_dim]['field']

    # Aggregate: row_key_tuple → col_key → value
    bucket = {}
    col_set = set()
    for r in agg:
        rk = tuple(r[f] for f in row_fields)
        ck = r[cols_field]
        col_set.add(ck)
        bucket.setdefault(rk, {})
        bucket[rk][ck] = bucket[rk].get(ck, 0) + m(r)

    col_keys_raw = sorted(col_set, key=lambda v: _sort_dim_value(cols_dim, v))
    row_keys = sorted(bucket.keys(),
                      key=lambda k: tuple(_sort_dim_value(rows_dims[i], k[i])
                                          for i in range(len(k))))

    matrix = []
    col_totals = [0] * len(col_keys_raw)
    grand_total = 0
    prev_keys = [None] * len(row_fields)
    for rk in row_keys:
        row_cells = []
        row_total = 0
        for ci, ck in enumerate(col_keys_raw):
            v = bucket[rk].get(ck, 0)
            row_cells.append(v)
            row_total += v
            col_totals[ci] += v
        display = []
        for i, raw in enumerate(rk):
            fmt = _pivot_format_key(rows_dims[i], raw)
            if prev_keys[i] == raw and i < len(rk) - 1:
                display.append('')
            else:
                display.append(fmt)
        prev_keys = list(rk)
        matrix.append({
            'keys':  display,
            'cells': row_cells,
            'total': row_total,
        })
        grand_total += row_total

    return {
        'mode': '2d',
        'row_labels': row_labels,
        'cols_label': PIVOT_DIM_CONFIG[cols_dim]['label'],
        'col_keys':   [_pivot_format_key(cols_dim, v) for v in col_keys_raw],
        'matrix':     matrix,
        'col_totals': col_totals,
        'grand_total': grand_total,
    }


@admin_required
def reports(request):
    form = ReportForm(request.GET or None, initial={'period': 'week', 'report_type': 'sales'})
    rows = []
    headers = []
    title = ''
    summary = {}
    pivot = None

    if form.is_valid():
        rtype = form.cleaned_data['report_type']
        period = form.cleaned_data['period']
        date_from = form.cleaned_data.get('date_from')
        date_to = form.cleaned_data.get('date_to')
        branch = form.cleaned_data.get('branch')

        d_start, d_end, dt_start, dt_end = _resolve_period(period, date_from, date_to)

        if rtype == 'sales':
            title = "Sotuvlar hisoboti"
            qs = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end) \
                .select_related('variant__product', 'branch', 'sold_by').order_by('-sold_at')
            if branch:
                qs = qs.filter(branch=branch)
            headers = ['Sana', 'Filial', 'Kod', 'Mahsulot', "O'lcham", 'Rang',
                       'Soni', "Narx (so'm)", "Jami (so'm)", 'Sotuvchi', 'Izoh']
            total_qty = 0
            total_rev = 0
            for s in qs:
                rows.append([
                    timezone.localtime(s.sold_at).strftime('%Y-%m-%d %H:%M'),
                    s.branch.name, s.variant.product.code, s.variant.product.name,
                    s.variant.size, s.variant.color, s.quantity,
                    s.sale_price, s.total, s.sold_by.username, s.note,
                ])
                total_qty += s.quantity
                total_rev += s.total
            summary = {'Jami sotilgan dona': total_qty,
                       "Jami daromad (so'm)": total_rev,
                       'Sotuvlar soni': qs.count()}

        elif rtype == 'intakes':
            title = "Qabullar hisoboti"
            qs = Intake.objects.filter(received_at__gte=dt_start, received_at__lt=dt_end) \
                .select_related('variant__product', 'branch', 'received_by') \
                .order_by('-received_at')
            if branch:
                qs = qs.filter(branch=branch)
            headers = ['Sana', 'Filial', 'Kod', 'Mahsulot', "O'lcham", 'Rang',
                       'Soni', "Tannarx (so'm)", "Jami xarajat (so'm)",
                       'Yetkazib beruvchi', 'Qabul qildi', 'Izoh']
            total_qty = 0
            total_cost = 0
            for i in qs:
                rows.append([
                    timezone.localtime(i.received_at).strftime('%Y-%m-%d %H:%M'),
                    i.branch.name, i.variant.product.code, i.variant.product.name,
                    i.variant.size, i.variant.color, i.quantity,
                    i.cost_per_unit, i.total_cost, i.supplier,
                    i.received_by.username, i.note,
                ])
                total_qty += i.quantity
                total_cost += i.total_cost
            summary = {'Jami qabul qilingan dona': total_qty,
                       "Jami xarajat (so'm)": total_cost,
                       'Qabullar soni': qs.count()}

        elif rtype == 'inventory':
            title = "Joriy ombor holati"
            qs = BranchStock.objects.filter(stock_count__gt=0) \
                .select_related('variant__product', 'branch') \
                .order_by('branch__name', 'variant__product__name')
            if branch:
                qs = qs.filter(branch=branch)
            headers = ['Filial', 'Kod', 'Mahsulot', "O'lcham", 'Rang',
                       'Soni', "Tannarx (so'm)", "Jami qiymat (so'm)"]
            total_qty = 0
            total_val = 0
            for s in qs:
                val = s.stock_count * s.cost_price
                rows.append([
                    s.branch.name, s.variant.product.code, s.variant.product.name,
                    s.variant.size, s.variant.color,
                    s.stock_count, s.cost_price, val,
                ])
                total_qty += s.stock_count
                total_val += val
            summary = {'Jami dona': total_qty, "Jami qiymat (so'm)": total_val,
                       'Variantlar soni': qs.count()}

        elif rtype == 'by_product':
            title = "Mahsulotlar bo'yicha sotuv xulosasi"
            sale_qs = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end)
            if branch:
                sale_qs = sale_qs.filter(branch=branch)
            agg = sale_qs.values(
                'variant__product__code', 'variant__product__name'
            ).annotate(
                qty=Sum('quantity'),
                revenue=Sum(ExpressionWrapper(
                    F('quantity') * F('sale_price'),
                    output_field=DecimalField(max_digits=14, decimal_places=2))),
                count=Count('id'),
            ).order_by('-revenue')
            headers = ['Kod', 'Mahsulot', 'Sotuvlar soni', 'Sotilgan dona', "Daromad (so'm)"]
            total_qty = 0
            total_rev = 0
            for a in agg:
                rows.append([
                    a['variant__product__code'], a['variant__product__name'],
                    a['count'], a['qty'], a['revenue'],
                ])
                total_qty += a['qty'] or 0
                total_rev += a['revenue'] or 0
            summary = {'Jami sotilgan dona': total_qty,
                       "Jami daromad (so'm)": total_rev,
                       'Mahsulotlar soni': len(agg)}

        elif rtype == 'pivot':
            # Multi-select rows: pick whatever user checked, default to branch.
            picked_rows = form.cleaned_data.get('pivot_rows') or []
            if not picked_rows:
                picked_rows = ['branch']
            # Auto-hierarchy: sort by the canonical parent-first order.
            rows_dims = sorted(
                [d for d in picked_rows if d in PIVOT_DIM_ORDER],
                key=lambda d: PIVOT_DIM_ORDER[d],
            )
            cols_dim = form.cleaned_data.get('pivot_cols') or ''
            metric   = form.cleaned_data.get('pivot_metric') or 'revenue'

            metric_label = dict(PIVOT_METRIC_CHOICES).get(metric, metric)
            row_labels   = [PIVOT_DIM_CONFIG[d]['label'] for d in rows_dims]
            cols_label   = PIVOT_DIM_CONFIG[cols_dim]['label'] if cols_dim else ''

            title = (f"Pivot — {metric_label}: "
                     + ' > '.join(row_labels)
                     + (f" × {cols_label}" if cols_label else ''))

            sale_qs = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end)
            if branch:
                sale_qs = sale_qs.filter(branch=branch)

            pivot = _build_pivot(sale_qs, rows_dims, cols_dim, metric)
            pivot['metric'] = metric
            pivot['metric_label'] = metric_label
            pivot['is_currency'] = metric in ('revenue', 'profit')
            pivot['rows_dims']  = rows_dims  # what the picker should re-check

            # CSV / PDF flat representation
            if pivot['mode'] == '1d':
                headers = row_labels + [metric_label, '% Ulush']
                rows = []
                for it in pivot['items']:
                    rows.append(list(it['keys']) + [it['value'], f"{it['pct']:.1f}%"])
                summary = {f"Jami {metric_label.lower()}": pivot['total']}
            else:
                headers = row_labels + list(pivot['col_keys']) + ['Jami']
                rows = []
                for r in pivot['matrix']:
                    rows.append(list(r['keys']) + list(r['cells']) + [r['total']])
                rows.append(['Ustun jami'] + [''] * (len(row_labels) - 1)
                            + list(pivot['col_totals']) + [pivot['grand_total']])
                summary = {f"Jami {metric_label.lower()}": pivot['grand_total']}

        if request.GET.get('export') == 'csv':
            return _csv_response(title, headers, rows, summary,
                                 d_start, d_end, branch)
        if request.GET.get('export') == 'pdf':
            return _pdf_response(title, headers, rows, summary,
                                 d_start, d_end, branch)

        return render(request, 'inventory/reports.html', {
            'form': form, 'rows': rows, 'headers': headers, 'title': title,
            'summary': summary, 'd_start': d_start, 'd_end': d_end,
            'branch': branch, 'pivot': pivot,
        })

    return render(request, 'inventory/reports.html', {
        'form': form, 'rows': None, 'headers': [], 'title': '',
        'pivot': None,
    })


# ---------- CART / MULTI-ITEM SALE ----------

CART_KEY = 'cart'  # session dict: {str(stock_id): qty}


def _get_cart(request):
    return request.session.get(CART_KEY, {}) or {}


def _save_cart(request, cart):
    # drop zero/negative entries
    cart = {k: int(v) for k, v in cart.items() if int(v) > 0}
    request.session[CART_KEY] = cart
    request.session.modified = True
    return cart


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


def cart_count(request):
    """Helper used by base.html navbar to show cart badge."""
    cart = _get_cart(request)
    return sum(int(v) for v in cart.values())


@login_required
def cart_add(request, stock_id):
    """POST /cart/add/<stock_id>/  with qty (default 1)."""
    stock = get_object_or_404(BranchStock.objects.select_related('branch'), pk=stock_id)
    if not request.user.is_admin() and request.user.branch_id != stock.branch_id:
        return HttpResponseForbidden("Bu filialda sotishga ruxsat yo'q.")

    try:
        qty = max(1, int(request.POST.get('qty') or 1))
    except ValueError:
        qty = 1
    cart = _get_cart(request)
    new_qty = int(cart.get(str(stock_id), 0)) + qty
    if new_qty > stock.stock_count:
        new_qty = stock.stock_count
        messages.warning(request,
            f"Omborda faqat {stock.stock_count} dona bor.")
    cart[str(stock_id)] = new_qty
    _save_cart(request, cart)
    messages.success(request,
        f"Savatga qo'shildi: {stock.variant.product.code} × {qty} dona.")
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or 'lookup'
    return redirect(next_url if next_url.startswith('/') else 'lookup')


@login_required
def cart_view(request):
    cart = _get_cart(request)
    lines = _cart_lines(cart)
    # If user is a seller, only their branch's items
    if not request.user.is_admin():
        lines = [l for l in lines if l['stock'].branch_id == request.user.branch_id]
    total = sum(l['subtotal'] for l in lines)
    return render(request, 'inventory/cart.html', {
        'lines': lines, 'total': total,
        'payment_methods': SaleTransaction.PaymentMethod.choices,
    })


@login_required
def cart_update(request):
    """POST: update qty of an item or remove it."""
    cart = _get_cart(request)
    stock_id = request.POST.get('stock_id')
    action = request.POST.get('action')
    if stock_id in cart:
        if action == 'remove':
            del cart[stock_id]
        else:
            try:
                qty = int(request.POST.get('qty') or 0)
            except ValueError:
                qty = 0
            if qty <= 0:
                del cart[stock_id]
            else:
                stock = BranchStock.objects.filter(pk=stock_id).first()
                if stock:
                    cart[stock_id] = min(qty, stock.stock_count)
    _save_cart(request, cart)
    return redirect('cart_view')


@login_required
def cart_clear(request):
    request.session[CART_KEY] = {}
    request.session.modified = True
    messages.info(request, "Savat bo'shatildi.")
    return redirect('lookup')


@login_required
def checkout(request):
    cart = _get_cart(request)
    lines = _cart_lines(cart)
    if not request.user.is_admin():
        lines = [l for l in lines if l['stock'].branch_id == request.user.branch_id]
    if not lines:
        messages.warning(request, "Savat bo'sh.")
        return redirect('lookup')

    # All lines must be from same branch
    branches = {l['stock'].branch_id for l in lines}
    if len(branches) > 1:
        messages.error(request,
            "Bitta chekda turli filiallardan tovar bo'lmaydi. Ortiqcha tovarlarni olib tashlang.")
        return redirect('cart_view')
    if any(not l['ok'] for l in lines):
        messages.error(request, "Ba'zi tovarlar yetarli emas — savatni tekshiring.")
        return redirect('cart_view')

    if request.method == 'POST':
        with transaction.atomic():
            branch = lines[0]['stock'].branch
            txn = SaleTransaction.objects.create(
                branch=branch,
                sold_by=request.user,
                payment_method=request.POST.get('payment_method') or 'cash',
                customer_name=request.POST.get('customer_name') or '',
                customer_phone=request.POST.get('customer_phone') or '',
                note=request.POST.get('note') or '',
            )
            for line in lines:
                stock = line['stock']
                qty = line['qty']
                stock.stock_count = F('stock_count') - qty
                stock.save()
                Sale.objects.create(
                    transaction=txn,
                    variant=stock.variant, branch=stock.branch,
                    quantity=qty,
                    sale_price=stock.sale_price or stock.variant.product.default_sale_price,
                    cost_at_sale=stock.cost_price,
                    sold_by=request.user,
                )
            request.session[CART_KEY] = {}
            request.session.modified = True

        from .fiscal import submit_for_transaction
        submit_for_transaction(txn)

        messages.success(request,
            f"Sotuv yakunlandi: {len(lines)} ta mahsulot.")
        return redirect('transaction_detail', pk=txn.pk)

    return render(request, 'inventory/checkout.html', {
        'lines': lines, 'total': sum(l['subtotal'] for l in lines),
        'payment_methods': SaleTransaction.PaymentMethod.choices,
    })


@login_required
def transaction_detail(request, pk):
    txn = get_object_or_404(
        SaleTransaction.objects.select_related('branch', 'sold_by')
            .prefetch_related('lines__variant__product'),
        pk=pk,
    )
    if not request.user.is_admin() and request.user.branch_id != txn.branch_id:
        return HttpResponseForbidden("Bu chekni ko'rishga ruxsat yo'q.")
    return render(request, 'inventory/transaction_detail.html', {'txn': txn})


# ---------- RETURNS ----------

@login_required
def return_create(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related('variant__product', 'branch'),
                             pk=sale_id)
    if not request.user.is_admin() and request.user.branch_id != sale.branch_id:
        return HttpResponseForbidden()

    max_returnable = sale.quantity - sale.returned_qty
    if max_returnable <= 0:
        messages.warning(request, "Bu sotuv allaqachon to'liq qaytarilgan.")
        return redirect('sales_list')

    # Require open shift for cash reconciliation
    open_shift = _open_shift_for(sale.branch)
    if request.method == 'POST' and not open_shift:
        messages.error(request,
            "Smen ochilmagan. Qaytarish faqat ochiq smen davomida amalga oshiriladi.")
        return redirect('return_create', sale_id=sale.id)

    if request.method == 'POST':
        try:
            qty = int(request.POST.get('quantity') or 0)
        except ValueError:
            qty = 0
        reason = (request.POST.get('reason') or '').strip()
        if qty < 1 or qty > max_returnable:
            messages.error(request,
                f"Qaytarish miqdori 1 dan {max_returnable} gacha bo'lishi kerak.")
            return redirect('return_create', sale_id=sale.id)
        with transaction.atomic():
            stock = BranchStock.objects.filter(
                variant=sale.variant, branch=sale.branch
            ).first()
            if stock:
                stock.stock_count = F('stock_count') + qty
                stock.save()
            Return.objects.create(
                shift=open_shift,
                sale=sale, quantity=qty, reason=reason,
                refunded_by=request.user,
            )
        messages.success(request,
            f"Qaytarildi: {qty} dona × {sale.sale_price} so'm.")
        return redirect('sales_list')

    return render(request, 'inventory/return_form.html', {
        'sale': sale, 'max_returnable': max_returnable,
    })


# ---------- AUDIT LOG ----------

@admin_required
def audit_list(request):
    logs = AuditLog.objects.select_related('user').all()
    # Filters
    action = request.GET.get('action') or ''
    user_filter = request.GET.get('user') or ''
    model = request.GET.get('model') or ''
    q = (request.GET.get('q') or '').strip()
    date_from = request.GET.get('date_from') or ''
    date_to = request.GET.get('date_to') or ''
    export = request.GET.get('export') == 'csv'

    if action:
        logs = logs.filter(action=action)
    if user_filter:
        logs = logs.filter(username_snapshot=user_filter)
    if model:
        logs = logs.filter(model_name=model)
    if q:
        logs = logs.filter(
            Q(username_snapshot__icontains=q) |
            Q(object_repr__icontains=q) |
            Q(model_name__icontains=q) |
            Q(object_id=q)
        )
    try:
        if date_from:
            logs = logs.filter(created_at__date__gte=datetime.strptime(date_from, '%Y-%m-%d').date())
    except ValueError:
        date_from = ''
    try:
        if date_to:
            logs = logs.filter(created_at__date__lte=datetime.strptime(date_to, '%Y-%m-%d').date())
    except ValueError:
        date_to = ''

    logs = logs.order_by('-created_at')

    if export:
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename="audit_log.csv"'
        resp.write('﻿')
        w = csv.writer(resp)
        w.writerow(['Sana', 'Vaqt', 'Foydalanuvchi', 'Amal', 'Model',
                    'Obyekt ID', 'Obyekt', 'IP'])
        for l in logs[:10000]:
            w.writerow([
                l.created_at.strftime('%Y-%m-%d'),
                l.created_at.strftime('%H:%M:%S'),
                l.username_snapshot or '',
                l.get_action_display(),
                l.model_name or '',
                l.object_id or '',
                l.object_repr or '',
                l.ip_address or '',
            ])
        return resp

    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(logs, 50)
    page = paginator.get_page(request.GET.get('page'))

    actions = AuditLog.Action.choices
    users = (AuditLog.objects.values_list('username_snapshot', flat=True)
             .distinct().order_by('username_snapshot'))
    users = [u for u in users if u]
    models_list = (AuditLog.objects.values_list('model_name', flat=True)
                   .distinct().order_by('model_name'))
    models_list = [m for m in models_list if m]

    return render(request, 'inventory/audit_list.html', {
        'page': page, 'actions': actions, 'users': users,
        'models_list': models_list,
        'f_action': action, 'f_user': user_filter, 'f_model': model, 'q': q,
        'date_from': date_from, 'date_to': date_to,
    })


# ---------- CUSTOMERS ----------

@admin_required
def customer_list(request):
    """Mijozlar ro'yxati: qidiruv + segment filter + CSV export."""
    q = (request.GET.get('q') or '').strip()
    segment = request.GET.get('segment') or ''
    export = request.GET.get('export') == 'csv'

    customers = Customer.objects.all()
    if q:
        customers = customers.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    revenue_expr = ExpressionWrapper(
        F('transactions__lines__quantity') * F('transactions__lines__sale_price'),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    customers = customers.annotate(
        txn_count=Count('transactions', distinct=True),
        total_spent=Coalesce(Sum(revenue_expr), 0,
                             output_field=DecimalField(max_digits=14, decimal_places=2)),
        last_visit=Max('transactions__sold_at'),
    ).order_by('-total_spent', 'name')

    # Segment thresholds (sums in UZS)
    VIP_THRESHOLD = 5_000_000   # 5M+ → VIP
    REGULAR_THRESHOLD = 500_000  # 500k+ → Regular

    if segment == 'vip':
        customers = customers.filter(total_spent__gte=VIP_THRESHOLD)
    elif segment == 'regular':
        customers = customers.filter(total_spent__gte=REGULAR_THRESHOLD,
                                     total_spent__lt=VIP_THRESHOLD)
    elif segment == 'new':
        customers = customers.filter(total_spent__lt=REGULAR_THRESHOLD)

    customers = customers[:300]

    if export:
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename="customers.csv"'
        resp.write('﻿')
        w = csv.writer(resp)
        w.writerow(['Ism', 'Telefon', 'Tag', 'Cheklar', 'Jami sarf',
                    "O'rtacha chek", 'Oxirgi tashrif'])
        for c in customers:
            avg = (c.total_spent / c.txn_count) if c.txn_count else 0
            w.writerow([
                c.name or '', c.phone or '', c.tags or '',
                c.txn_count or 0,
                float(c.total_spent or 0),
                float(avg),
                c.last_visit.strftime('%Y-%m-%d %H:%M') if c.last_visit else '',
            ])
        return resp

    customers = list(customers)
    # Compute average ticket and segment label for display
    for c in customers:
        c.avg_ticket = (float(c.total_spent) / c.txn_count) if c.txn_count else 0
        spent = float(c.total_spent or 0)
        if spent >= VIP_THRESHOLD:
            c.segment_label = 'VIP'
            c.segment_class = 'bg-warning text-dark'
        elif spent >= REGULAR_THRESHOLD:
            c.segment_label = 'Doimiy'
            c.segment_class = 'bg-success'
        else:
            c.segment_label = 'Yangi'
            c.segment_class = 'bg-secondary'

    # Aggregate stats for header
    total_count = Customer.objects.count()
    total_revenue = Customer.objects.annotate(
        s=Coalesce(Sum(revenue_expr), 0,
                   output_field=DecimalField(max_digits=14, decimal_places=2))
    ).aggregate(t=Sum('s'))['t'] or 0

    return render(request, 'inventory/customer_list.html', {
        'customers': customers,
        'q': q, 'segment': segment,
        'total_count': total_count,
        'total_revenue': total_revenue,
    })


@admin_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    txns = (customer.transactions.select_related('branch', 'sold_by')
            .prefetch_related('lines__variant__product')
            .order_by('-sold_at')[:50])
    revenue_expr = ExpressionWrapper(
        F('lines__quantity') * F('lines__sale_price'),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    stats = customer.transactions.aggregate(
        n=Count('id', distinct=True),
        total=Coalesce(Sum(revenue_expr), 0,
                       output_field=DecimalField(max_digits=14, decimal_places=2)),
        first_visit=Min('sold_at'),
        last_visit=Max('sold_at'),
    )
    total = float(stats['total'] or 0)
    n_txns = stats['n'] or 0
    avg_ticket = total / n_txns if n_txns else 0

    # Segment
    if total >= 5_000_000:
        segment = ('VIP', 'bg-warning text-dark')
    elif total >= 500_000:
        segment = ('Doimiy', 'bg-success')
    else:
        segment = ('Yangi', 'bg-secondary')

    # Top 5 favorite products (with image)
    fav_qs = (Sale.objects
              .filter(transaction__customer=customer)
              .values('variant__product_id',
                      'variant__product__code',
                      'variant__product__name')
              .annotate(qty=Sum('quantity'), revenue=Sum(F('quantity') * F('sale_price'),
                                                        output_field=DecimalField(max_digits=14, decimal_places=2)))
              .order_by('-qty')[:5])
    favorites = list(fav_qs)
    # Attach product image URL where available
    fav_ids = [f['variant__product_id'] for f in favorites]
    prod_imgs = {p.id: (p.image.url if p.image else '')
                 for p in Product.objects.filter(id__in=fav_ids)}
    for f in favorites:
        f['image_url'] = prod_imgs.get(f['variant__product_id'], '')

    # 6-month spend chart (by month)
    from collections import OrderedDict
    from datetime import date as _date
    chart = OrderedDict()
    today = timezone.localdate()
    # Last 6 months
    cur = today.replace(day=1)
    months = []
    for _ in range(6):
        months.append(cur)
        # Previous month
        if cur.month == 1:
            cur = cur.replace(year=cur.year - 1, month=12)
        else:
            cur = cur.replace(month=cur.month - 1)
    months.reverse()
    for m in months:
        chart[m.strftime('%Y-%m')] = 0
    # Sum per month
    for t in customer.transactions.all():
        key = t.sold_at.astimezone(timezone.get_current_timezone()).strftime('%Y-%m')
        if key in chart:
            chart[key] += float(t.total)
    chart_labels = [m.strftime('%b %Y') for m in months]
    chart_data = list(chart.values())

    return render(request, 'inventory/customer_detail.html', {
        'customer': customer, 'txns': txns,
        'stats': stats,
        'total_spent': total,
        'avg_ticket': avg_ticket,
        'n_txns': n_txns,
        'first_visit': stats.get('first_visit'),
        'last_visit': stats.get('last_visit'),
        'segment_label': segment[0],
        'segment_class': segment[1],
        'favorites': favorites,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    })


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


# ---------- PWA ----------

def _serve_static_file(rel_path, content_type):
    from django.conf import settings
    import os
    path = os.path.join(settings.BASE_DIR, 'static', rel_path)
    if not os.path.exists(path):
        return HttpResponse(status=404)
    with open(path, 'rb') as f:
        data = f.read()
    response = HttpResponse(data, content_type=content_type)
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def manifest(request):
    """PWA manifest served at /manifest.webmanifest"""
    return _serve_static_file('manifest.webmanifest', 'application/manifest+json')


def service_worker(request):
    """PWA service worker — must be served at root scope."""
    response = _serve_static_file('sw.js', 'application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response


# ---------- QR / ETIKETKA ----------

@login_required
def qr_image(request, code):
    """Mahsulot kodi uchun QR PNG"""
    norm = normalize_code(code)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8, border=2,
    )
    qr.add_data(norm)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    response = HttpResponse(buf.getvalue(), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=3600'
    return response


@admin_required
def product_labels(request, code=None):
    """A4 print etiketka sahifasi"""
    if code:
        products = [get_object_or_404(Product, code=normalize_code(code))]
    else:
        ids = request.GET.getlist('id') or request.GET.get('id', '').split(',')
        ids = [i for i in ids if i.strip().isdigit()]
        if ids:
            products = list(Product.objects.filter(id__in=ids))
        else:
            products = []
    try:
        copies = max(1, min(50, int(request.GET.get('copies', 6))))
    except ValueError:
        copies = 6
    items = []
    for p in products:
        for _ in range(copies):
            items.append(p)
    return render(request, 'inventory/labels.html', {
        'items': items, 'products': products, 'copies': copies,
    })


# ---------- INSIGHTS / ANALYTICS ----------

def _insights_context(request):
    """Insights view va export uchun barcha hisoblar."""
    period = request.GET.get('period', 'month')
    branch_id = request.GET.get('branch') or ''

    today = timezone.localdate()
    if period == 'week':
        d_start = today - timedelta(days=7)
        days = 7
    elif period == 'today':
        d_start = today
        days = 1
    elif period == 'quarter':
        d_start = today - timedelta(days=90)
        days = 90
    else:
        period = 'month'
        d_start = today - timedelta(days=30)
        days = 30

    tz = timezone.get_current_timezone()
    dt_start = datetime.combine(d_start, datetime.min.time()).replace(tzinfo=tz)
    dt_end = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(tzinfo=tz)
    # Oldingi davr (taqqoslash uchun)
    prev_d_start = d_start - timedelta(days=days)
    prev_dt_start = datetime.combine(prev_d_start, datetime.min.time()).replace(tzinfo=tz)
    prev_dt_end = dt_start

    sales = Sale.objects.filter(sold_at__gte=dt_start, sold_at__lt=dt_end)
    branches_all = Branch.objects.filter(is_active=True)
    selected_branch = None
    if branch_id:
        try:
            selected_branch = Branch.objects.get(pk=int(branch_id))
            sales = sales.filter(branch=selected_branch)
        except (Branch.DoesNotExist, ValueError):
            pass

    # Net revenue = qty*price - line_discount. Whole-order discount is
    # attached to SaleTransaction so we subtract it at the txn-id aggregate.
    revenue_expr = ExpressionWrapper(
        F('quantity') * F('sale_price') - F('line_discount'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    totals = sales.aggregate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        sales_count=Count('id'),
    )

    # Subtract whole-order discounts (stored on SaleTransaction)
    txn_qs = SaleTransaction.objects.filter(
        sold_at__gte=dt_start, sold_at__lt=dt_end
    )
    if branch_id and selected_branch:
        txn_qs = txn_qs.filter(branch=selected_branch)
    order_disc = txn_qs.aggregate(s=Sum('order_discount'))['s'] or 0

    revenue = (totals['revenue'] or 0) - order_disc
    qty = totals['qty'] or 0
    sales_count = totals['sales_count'] or 0
    discount_total = order_disc + (sales.aggregate(s=Sum('line_discount'))['s'] or 0)

    # Foyda hisoblash: revenue - cost. cost = sum(quantity * variant.branch_stock.cost_price)
    # Sale modelida cost_price yo'q, lekin oxirgi BranchStock dan oladi (taxmin).
    # Aniqroq bo'lishi uchun har sotuv pay'tida cost_price'ni Sale'da saqlash kerak edi;
    # hozircha BranchStock.cost_price'dan foydalanamiz.
    # Cost is snapshotted on the Sale itself (cost_at_sale), so we can aggregate in SQL.
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    total_cost = sales.aggregate(c=Sum(cost_expr))['c'] or 0

    profit_by_product_qs = sales.values(
        'variant__product_id', 'variant__product__code', 'variant__product__name',
    ).annotate(
        revenue=Sum(revenue_expr),
        cost=Sum(cost_expr),
        qty=Sum('quantity'),
    )
    profit_by_product = {}
    for row in profit_by_product_qs:
        profit_by_product[row['variant__product_id']] = {
            'code': row['variant__product__code'],
            'name': row['variant__product__name'],
            'revenue': row['revenue'] or 0,
            'cost': row['cost'] or 0,
            'profit': (row['revenue'] or 0) - (row['cost'] or 0),
            'qty': row['qty'] or 0,
        }

    profit = revenue - total_cost
    margin = (profit / revenue * 100) if revenue else 0

    # TOP foyda keltirgan mahsulotlar (revenue emas, balki real foyda)
    top_profit = sorted(profit_by_product.values(),
                        key=lambda x: x['profit'], reverse=True)[:10]
    for p in top_profit:
        p['margin'] = (p['profit'] / p['revenue'] * 100) if p['revenue'] else 0

    # O'rtacha sotuv summasi va eng katta sotuv
    avg_sale_value = (revenue / sales_count) if sales_count else 0
    largest_sale = sales.annotate(
        line=ExpressionWrapper(F('quantity') * F('sale_price'),
                               output_field=DecimalField(max_digits=14, decimal_places=2))
    ).order_by('-line').first()

    # O'tgan davr taqqoslash (week-over-week / period-over-period)
    prev_sales = Sale.objects.filter(sold_at__gte=prev_dt_start, sold_at__lt=prev_dt_end)
    if branch_id and selected_branch:
        prev_sales = prev_sales.filter(branch=selected_branch)
    prev_totals = prev_sales.aggregate(
        revenue=Sum(revenue_expr), qty=Sum('quantity'), n=Count('id'),
    )
    prev_revenue = prev_totals['revenue'] or 0
    prev_qty = prev_totals['qty'] or 0
    prev_n = prev_totals['n'] or 0

    def growth(curr, prev):
        if not prev:
            return None
        return float((curr - prev) / prev * 100)

    rev_growth = growth(float(revenue), float(prev_revenue))
    qty_growth = growth(qty, prev_qty)
    sales_growth = growth(sales_count, prev_n)

    # Filiallar taqqoslash (current period) + P&L (qat'iy xarajatlardan keyin)
    period_fraction = days / 30.0
    branch_compare = []
    for br in branches_all:
        b_sales = sales.filter(branch=br)
        b_agg = b_sales.aggregate(
            rev=Sum(revenue_expr), cost=Sum(cost_expr),
            qty=Sum('quantity'), n=Count('id'),
        )
        b_rev = float(b_agg['rev'] or 0)
        b_cost = float(b_agg['cost'] or 0)
        b_profit = b_rev - b_cost
        b_n = b_agg['n'] or 0
        b_qty = b_agg['qty'] or 0
        b_stock = BranchStock.objects.filter(branch=br).aggregate(
            s=Sum('stock_count'))['s'] or 0
        rent_period = float(br.monthly_rent or 0) * period_fraction
        other_period = float(br.monthly_other_costs or 0) * period_fraction
        fixed_period = rent_period + other_period
        net_profit = b_profit - fixed_period
        branch_compare.append({
            'branch': br,
            'revenue': b_rev,
            'cost': b_cost,
            'profit': b_profit,  # gross (foyda tannarxdan keyin)
            'fixed_period': fixed_period,
            'rent_period': rent_period,
            'other_period': other_period,
            'net_profit': net_profit,
            'net_margin': (net_profit / b_rev * 100) if b_rev else 0,
            'margin': (b_profit / b_rev * 100) if b_rev else 0,
            'sales_count': b_n,
            'qty': b_qty,
            'avg_sale': (b_rev / b_n) if b_n else 0,
            'stock': b_stock,
        })

    # Tovar aylanmasi: kunlik o'rtacha sotuv va omborda qancha kun yetadi
    # Top sotilgan mahsulotlar uchun
    turnover = []
    for pid, pdata in sorted(profit_by_product.items(),
                             key=lambda x: x[1]['qty'], reverse=True)[:10]:
        daily_avg = pdata['qty'] / days if days else 0
        current_stock = BranchStock.objects.filter(
            variant__product_id=pid
        ).aggregate(s=Sum('stock_count'))['s'] or 0
        days_left = (current_stock / daily_avg) if daily_avg else None
        turnover.append({
            'code': pdata['code'], 'name': pdata['name'],
            'daily_avg': daily_avg, 'stock': current_stock,
            'days_left': days_left,
        })

    # TOP 10 mahsulot (qty va daromad bo'yicha)
    top_products = list(sales.values(
        'variant__product__id', 'variant__product__code',
        'variant__product__name',
    ).annotate(
        qty=Sum('quantity'),
        revenue=Sum(revenue_expr),
        n_sales=Count('id'),
    ).order_by('-revenue')[:10])

    # SLOW MOVERS — bu davrda umuman sotilmagan mahsulotlar
    sold_product_ids = sales.values_list('variant__product_id', flat=True).distinct()
    slow_products = list(
        Product.objects.exclude(id__in=sold_product_ids)
        .select_related('category')
        .annotate(stock=Sum('variants__branch_stocks__stock_count'))[:15]
    )

    # TOP sotuvchilar + komissiya
    top_sellers = list(sales.values(
        'sold_by__id', 'sold_by__username',
        'sold_by__first_name', 'sold_by__last_name',
        'sold_by__branch__name',
        'sold_by__commission_percent',
    ).annotate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        n_sales=Count('id'),
    ).order_by('-revenue')[:10])
    for s in top_sellers:
        pct = float(s.get('sold_by__commission_percent') or 0)
        rev = float(s.get('revenue') or 0)
        s['commission_percent'] = pct
        s['commission'] = rev * pct / 100 if pct else 0

    # Filiallar bo'yicha (donut/bar uchun)
    by_branch = list(sales.values(
        'branch__id', 'branch__name'
    ).annotate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
        n_sales=Count('id'),
    ).order_by('-revenue'))

    # Kategoriya bo'yicha
    by_category = list(sales.values(
        'variant__product__category__name',
    ).annotate(
        revenue=Sum(revenue_expr),
        qty=Sum('quantity'),
    ).order_by('-revenue'))

    # Sotuv trendi (kunlar bo'yicha)
    daily_map = {}
    for i in range(days):
        d = d_start + timedelta(days=i)
        daily_map[d.isoformat()] = {'qty': 0, 'revenue': 0}
    for s in sales:
        day_key = timezone.localtime(s.sold_at).date().isoformat()
        if day_key in daily_map:
            daily_map[day_key]['qty'] += s.quantity
            daily_map[day_key]['revenue'] += float(s.quantity * s.sale_price)
    daily_labels = list(daily_map.keys())
    daily_revenue = [daily_map[k]['revenue'] for k in daily_labels]
    daily_qty = [daily_map[k]['qty'] for k in daily_labels]

    # Soatlar bo'yicha (0-23)
    hour_buckets = [0] * 24
    for s in sales:
        h = timezone.localtime(s.sold_at).hour
        hour_buckets[h] += s.quantity

    # Hafta kunlari bo'yicha
    weekday_labels = ['Du', 'Se', 'Ch', 'Pa', 'Ju', 'Sh', 'Ya']
    weekday_buckets = [0] * 7
    for s in sales:
        wd = timezone.localtime(s.sold_at).weekday()  # 0=Monday
        weekday_buckets[wd] += s.quantity

    # Ombor risk: sof aksiya, narx
    out_of_stock = list(
        BranchStock.objects.filter(stock_count=0)
        .select_related('variant__product', 'branch')[:20]
    )

    # Qaytarilishlar: davr ichida eng ko'p qaytarilgan mahsulotlar.
    # Sifat muammosi indikatori — agar % returned high bo'lsa, mahsulot
    # tekshirilishi kerak (nuqson, o'lcham xato, va h.k.).
    returns_qs = Return.objects.filter(
        refunded_at__gte=dt_start, refunded_at__lt=dt_end,
    )
    if branch_id and selected_branch:
        returns_qs = returns_qs.filter(sale__branch=selected_branch)
    returns_total_qty = returns_qs.aggregate(s=Sum('quantity'))['s'] or 0
    returns_total_amount = returns_qs.aggregate(
        s=Sum(ExpressionWrapper(
            F('quantity') * F('sale__sale_price'),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ))
    )['s'] or 0
    return_rate = (returns_total_qty / qty * 100) if qty else 0

    top_returns_qs = (returns_qs
        .values('sale__variant__product_id',
                'sale__variant__product__code',
                'sale__variant__product__name')
        .annotate(qty_returned=Sum('quantity'))
        .order_by('-qty_returned')[:10])
    top_returns = []
    for row in top_returns_qs:
        pid = row['sale__variant__product_id']
        sold = profit_by_product.get(pid, {}).get('qty', 0)
        returned = row['qty_returned']
        rate = (returned / sold * 100) if sold else 0
        # Top reasons for this product
        reasons = (returns_qs.filter(sale__variant__product_id=pid)
                   .exclude(reason='')
                   .values('reason')
                   .annotate(n=Count('id'))
                   .order_by('-n')[:3])
        top_returns.append({
            'code': row['sale__variant__product__code'],
            'name': row['sale__variant__product__name'],
            'returned': returned,
            'sold': sold,
            'rate': rate,
            'reasons': [r['reason'] for r in reasons],
        })

    # Foyda yuqori mahsulotlar (markup bo'yicha eng yuqori)
    high_margin = list(
        Product.objects.order_by('-markup_percent')[:8]
    )

    context = {
        'period': period,
        'd_start': d_start,
        'd_end': today,
        'days': days,
        'discount_total': discount_total,
        'selected_branch': selected_branch,
        'branches_all': branches_all,
        'revenue': revenue,
        'qty': qty,
        'sales_count': sales_count,
        'total_cost': total_cost,
        'profit': profit,
        'margin': margin,
        'avg_sale_value': avg_sale_value,
        'largest_sale': largest_sale,
        'prev_revenue': prev_revenue,
        'prev_qty': prev_qty,
        'prev_n': prev_n,
        'rev_growth': rev_growth,
        'qty_growth': qty_growth,
        'sales_growth': sales_growth,
        'top_products': top_products,
        'top_profit': top_profit,
        'slow_products': slow_products,
        'top_sellers': top_sellers,
        'by_branch': by_branch,
        'by_category': by_category,
        'branch_compare': branch_compare,
        'turnover': turnover,
        'daily_labels': daily_labels,
        'daily_revenue': daily_revenue,
        'daily_qty': daily_qty,
        'hour_buckets': hour_buckets,
        'weekday_labels': weekday_labels,
        'weekday_buckets': weekday_buckets,
        'out_of_stock': out_of_stock,
        'high_margin': high_margin,
        'returns_total_qty': returns_total_qty,
        'returns_total_amount': returns_total_amount,
        'return_rate': return_rate,
        'top_returns': top_returns,
    }

    # ===== TIER D — Advanced BI =====

    # D1: Sales forecast — 90-day daily revenue → linear regression → next 30 days
    fc_start = today - timedelta(days=90)
    fc_dt_start = datetime.combine(fc_start, datetime.min.time()).replace(tzinfo=tz)
    fc_sales = Sale.objects.filter(sold_at__gte=fc_dt_start, sold_at__lt=dt_end)
    if branch_id and selected_branch:
        fc_sales = fc_sales.filter(branch=selected_branch)
    daily_rev = {}
    for i in range(91):  # 0..90 days back
        d = today - timedelta(days=90 - i)
        daily_rev[d.isoformat()] = 0.0
    for row in fc_sales.values('sold_at', 'quantity', 'sale_price', 'line_discount'):
        d = row['sold_at'].astimezone(tz).date().isoformat()
        if d in daily_rev:
            daily_rev[d] += (float(row['quantity']) * float(row['sale_price'])
                             - float(row['line_discount'] or 0))
    # Simple linear regression: y = a + b*x where x is day index (0..90)
    xs = list(range(len(daily_rev)))
    ys = list(daily_rev.values())
    n = len(xs)
    if n > 1 and sum(ys) > 0:
        sx = sum(xs); sy = sum(ys)
        sxy = sum(x * y for x, y in zip(xs, ys))
        sxx = sum(x * x for x in xs)
        denom = (n * sxx - sx * sx)
        if denom != 0:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n
        else:
            slope = 0; intercept = sy / n if n else 0
    else:
        slope = 0; intercept = 0
    # Build 90-actual + 30-predicted series
    actual_labels = []
    actual_values = []
    forecast_labels = []
    forecast_values = []
    for i, (date_iso, v) in enumerate(daily_rev.items()):
        actual_labels.append(date_iso[5:])
        actual_values.append(round(v))
    for j in range(1, 31):  # next 30 days
        future_date = today + timedelta(days=j)
        forecast_labels.append(future_date.isoformat()[5:])
        x_future = (n - 1) + j  # extending the index
        predicted = max(0, intercept + slope * x_future)
        forecast_values.append(round(predicted))
    total_forecast_30d = sum(forecast_values)
    context['fc_actual_labels'] = actual_labels
    context['fc_actual_values'] = actual_values
    context['fc_forecast_labels'] = forecast_labels
    context['fc_forecast_values'] = forecast_values
    context['fc_slope'] = slope
    context['fc_total_30d'] = total_forecast_30d
    context['fc_trend_direction'] = 'up' if slope > 0 else ('down' if slope < 0 else 'flat')

    # D2: Customer cohort retention (6 months)
    from collections import defaultdict
    cohort_first = {}  # customer_id -> first purchase month (YYYY-MM)
    cohort_activity = defaultdict(set)  # (cohort_month, activity_month) -> customers
    for row in (SaleTransaction.objects
                .filter(customer__isnull=False,
                        sold_at__gte=today - timedelta(days=180))
                .values('customer_id', 'sold_at')):
        cid = row['customer_id']
        m = row['sold_at'].astimezone(tz).strftime('%Y-%m')
        if cid not in cohort_first or m < cohort_first[cid]:
            cohort_first[cid] = m
        cohort_activity[(cohort_first.get(cid, m), m)].add(cid)
    # We need to re-pass since cohort_first might have updated
    cohort_first = {}
    for row in (SaleTransaction.objects
                .filter(customer__isnull=False,
                        sold_at__gte=today - timedelta(days=180))
                .order_by('sold_at')
                .values('customer_id', 'sold_at')):
        cid = row['customer_id']
        if cid not in cohort_first:
            cohort_first[cid] = row['sold_at'].astimezone(tz).strftime('%Y-%m')
    # Build cohort matrix
    cohort_activity = defaultdict(set)
    for row in (SaleTransaction.objects
                .filter(customer__isnull=False,
                        sold_at__gte=today - timedelta(days=180))
                .values('customer_id', 'sold_at')):
        cid = row['customer_id']
        if cid not in cohort_first:
            continue
        m = row['sold_at'].astimezone(tz).strftime('%Y-%m')
        cohort_activity[(cohort_first[cid], m)].add(cid)
    # List of 6 months back
    cohort_months = []
    cur_month_d = today.replace(day=1)
    for _ in range(6):
        cohort_months.append(cur_month_d.strftime('%Y-%m'))
        if cur_month_d.month == 1:
            cur_month_d = cur_month_d.replace(year=cur_month_d.year - 1, month=12)
        else:
            cur_month_d = cur_month_d.replace(month=cur_month_d.month - 1)
    cohort_months.reverse()  # oldest first
    cohort_rows = []
    for cohort in cohort_months:
        cohort_size = sum(1 for cid, m in cohort_first.items() if m == cohort)
        if cohort_size == 0:
            cohort_rows.append({'cohort': cohort, 'size': 0, 'cells': []})
            continue
        cells = []
        for activity in cohort_months:
            if activity < cohort:
                cells.append(None)
                continue
            active = len(cohort_activity.get((cohort, activity), set()))
            pct = (active / cohort_size * 100) if cohort_size else 0
            cells.append({'count': active, 'pct': round(pct, 1)})
        cohort_rows.append({'cohort': cohort, 'size': cohort_size, 'cells': cells})
    context['cohort_months'] = cohort_months
    context['cohort_rows'] = cohort_rows

    # ===== BI EXTENSIONS =====

    # Hour × Weekday heatmap (revenue grid 7×24)
    heatmap = [[0 for _ in range(24)] for _ in range(7)]  # 0=Mon
    for sold_at, qty, price, disc in sales.values_list('sold_at', 'quantity', 'sale_price', 'line_discount'):
        local_dt = timezone.localtime(sold_at)
        wd = local_dt.weekday()
        h = local_dt.hour
        heatmap[wd][h] += float(qty) * float(price) - float(disc or 0)
    flat_heatmap = []
    for wd in range(7):
        for h in range(24):
            flat_heatmap.append({'x': h, 'y': wd, 'v': heatmap[wd][h]})
    max_heat = max((max(row) for row in heatmap), default=0) or 1
    context['heatmap'] = heatmap
    context['heatmap_max'] = max_heat
    context['heatmap_hour_labels'] = list(range(24))
    context['heatmap_day_labels'] = ['Du', 'Se', 'Ch', 'Pa', 'Ju', 'Sh', 'Ya']

    # Year-over-year: same date range last year
    last_year_start = dt_start.replace(year=dt_start.year - 1)
    last_year_end = dt_end.replace(year=dt_end.year - 1)
    ly_sales = Sale.objects.filter(sold_at__gte=last_year_start, sold_at__lt=last_year_end)
    if branch_id and selected_branch:
        ly_sales = ly_sales.filter(branch=selected_branch)
    ly_agg = ly_sales.aggregate(
        revenue=Sum(revenue_expr), qty=Sum('quantity'),
        txns=Count('transaction', distinct=True),
    )
    ly_revenue = float(ly_agg['revenue'] or 0)
    ly_qty = ly_agg['qty'] or 0
    ly_txns = ly_agg['txns'] or 0
    def _pct(now, prev):
        if not prev: return None
        return (float(now) - float(prev)) / float(prev) * 100
    context['ly_revenue'] = ly_revenue
    context['ly_qty'] = ly_qty
    context['ly_txns'] = ly_txns
    context['yoy_revenue_delta'] = _pct(revenue, ly_revenue)
    context['yoy_qty_delta'] = _pct(qty, ly_qty)
    context['yoy_txns_delta'] = _pct(sales_count, ly_txns)

    # ABC analysis: sort by revenue, classify by cumulative %.
    # L10 fix: thresholds configurable via query string (?abc_a=80&abc_b=95)
    try:
        abc_a_threshold = float(request.GET.get('abc_a') or 80)
        abc_b_threshold = float(request.GET.get('abc_b') or 95)
    except (TypeError, ValueError):
        abc_a_threshold, abc_b_threshold = 80, 95
    abc_a_threshold = max(1, min(99, abc_a_threshold))
    abc_b_threshold = max(abc_a_threshold + 1, min(99.5, abc_b_threshold))

    sorted_products = sorted(profit_by_product.values(),
                             key=lambda x: float(x['revenue'] or 0), reverse=True)
    total_rev = sum(float(p['revenue'] or 0) for p in sorted_products) or 1
    abc_summary = {'A': 0, 'B': 0, 'C': 0}
    abc_count = {'A': 0, 'B': 0, 'C': 0}
    cumulative = 0
    for p in sorted_products:
        pct = float(p['revenue'] or 0) / total_rev * 100
        cumulative += pct
        if cumulative <= abc_a_threshold:
            cls = 'A'
        elif cumulative <= abc_b_threshold:
            cls = 'B'
        else:
            cls = 'C'
        p['abc'] = cls
        abc_summary[cls] += float(p['revenue'] or 0)
        abc_count[cls] += 1
    context['abc_a_threshold'] = abc_a_threshold
    context['abc_b_threshold'] = abc_b_threshold
    context['abc_top_a'] = [p for p in sorted_products if p['abc'] == 'A'][:8]
    context['abc_summary'] = abc_summary
    context['abc_count'] = abc_count

    # Dead stock: products with zero sales in last 90 days but stock > 0
    cutoff_90d = timezone.now() - timedelta(days=90)
    recently_sold_ids = set(Sale.objects.filter(sold_at__gte=cutoff_90d)
                            .values_list('variant__product_id', flat=True).distinct())
    has_stock = (BranchStock.objects.filter(stock_count__gt=0)
                 .values_list('variant__product_id', flat=True).distinct())
    dead_ids = set(has_stock) - recently_sold_ids
    dead_products = list(Product.objects
                         .filter(id__in=list(dead_ids))
                         .annotate(stock=Sum('variants__branch_stocks__stock_count'),
                                   value=Sum(ExpressionWrapper(
                                       F('variants__branch_stocks__stock_count')
                                       * F('variants__branch_stocks__cost_price'),
                                       output_field=DecimalField(max_digits=14, decimal_places=2))))
                         .order_by('-value')[:10])
    context['dead_products'] = dead_products
    context['dead_count'] = len(dead_ids)

    # Margin alerts: products where last sale was at/below cost
    margin_alerts = []
    bad_sales = (sales.filter(sale_price__lte=F('cost_at_sale'))
                 .select_related('variant__product', 'branch')
                 .order_by('-sold_at')[:5])
    for s in bad_sales:
        margin_alerts.append({
            'product': s.variant.product,
            'branch': s.branch.name,
            'date': s.sold_at,
            'sale_price': float(s.sale_price),
            'cost': float(s.cost_at_sale),
            'loss': (float(s.cost_at_sale) - float(s.sale_price)) * s.quantity,
        })
    context['margin_alerts'] = margin_alerts

    return context


@admin_required
def insights(request):
    ctx = _insights_context(request)
    fmt = request.GET.get('export', '').lower()
    if fmt == 'csv':
        return _insights_csv(ctx)
    if fmt == 'pdf':
        return _insights_pdf(ctx)
    return render(request, 'inventory/insights.html', ctx)


def _insights_csv(ctx):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fn = f"Biznes_tahlili_{ctx['d_start']}_{ctx['d_end']}.csv"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'
    response.write('﻿')
    w = csv.writer(response)
    branch_str = ctx['selected_branch'].name if ctx['selected_branch'] else 'Barcha filiallar'
    w.writerow([f"yurit — Biznes tahlili"])
    w.writerow([f"Davr: {ctx['d_start']} — {ctx['d_end']}  ({ctx['days']} kun)"])
    w.writerow([f"Filial: {branch_str}"])
    w.writerow([])

    w.writerow(['ASOSIY KO\'RSATKICHLAR'])
    w.writerow(["Daromad (so'm)", ctx['revenue']])
    w.writerow(["Tannarx (so'm)", ctx['total_cost']])
    w.writerow(["Sof foyda (so'm)", ctx['profit']])
    w.writerow(['Foyda marjasi (%)', f"{ctx['margin']:.2f}"])
    w.writerow(['Sotuvlar soni', ctx['sales_count']])
    w.writerow(['Sotilgan dona', ctx['qty']])
    w.writerow(["O'rtacha sotuv summasi (so'm)", round(ctx['avg_sale_value'])])
    if ctx['largest_sale']:
        w.writerow(["Eng katta bitta sotuv (so'm)", ctx['largest_sale'].line])
    w.writerow([])

    w.writerow(["OLDINGI DAVR BILAN TAQQOSLASH"])
    w.writerow(['Ko\'rsatkich', 'Hozir', 'Oldin', "O'sish %"])
    g = lambda v: f"{v:+.1f}%" if v is not None else '—'
    w.writerow(['Daromad', ctx['revenue'], ctx['prev_revenue'], g(ctx['rev_growth'])])
    w.writerow(['Dona', ctx['qty'], ctx['prev_qty'], g(ctx['qty_growth'])])
    w.writerow(['Sotuvlar', ctx['sales_count'], ctx['prev_n'], g(ctx['sales_growth'])])
    w.writerow([])

    w.writerow([f"TOP-10 MAHSULOT (DAROMAD BO'YICHA)"])
    w.writerow(['Kod', 'Mahsulot', 'Dona', 'Sotuvlar', "Daromad (so'm)"])
    for p in ctx['top_products']:
        w.writerow([p['variant__product__code'], p['variant__product__name'],
                    p['qty'], p['n_sales'], p['revenue']])
    w.writerow([])

    w.writerow([f"TOP-10 MAHSULOT (SOF FOYDA BO'YICHA)"])
    w.writerow(['Kod', 'Mahsulot', 'Dona', "Daromad", "Tannarx", "Foyda", 'Marja %'])
    for p in ctx['top_profit']:
        w.writerow([p['code'], p['name'], p['qty'], p['revenue'],
                    p['cost'], p['profit'], f"{p['margin']:.1f}"])
    w.writerow([])

    w.writerow(["SOTILMAYOTGAN MAHSULOTLAR"])
    w.writerow(['Kod', 'Mahsulot', 'Kategoriya', 'Omborda dona'])
    for p in ctx['slow_products']:
        w.writerow([p.code, p.name,
                    p.category.name if p.category else '—',
                    p.stock or 0])
    w.writerow([])

    w.writerow(["FILIALLAR TAQQOSLASH"])
    w.writerow(['Filial', "Daromad", "Tannarx", "Foyda", 'Marja %',
                'Sotuvlar', 'Dona', "O'rtacha sotuv", 'Omborda dona'])
    for b in ctx['branch_compare']:
        w.writerow([b['branch'].name, b['revenue'], b['cost'], b['profit'],
                    f"{b['margin']:.1f}", b['sales_count'], b['qty'],
                    round(b['avg_sale']), b['stock']])
    w.writerow([])

    w.writerow(["TOP-10 SOTUVCHILAR"])
    w.writerow(['Username', 'Ism', 'Filial', 'Sotuvlar', 'Dona', "Daromad (so'm)"])
    for s in ctx['top_sellers']:
        w.writerow([
            s['sold_by__username'],
            f"{s['sold_by__first_name'] or ''} {s['sold_by__last_name'] or ''}".strip(),
            s['sold_by__branch__name'] or '—',
            s['n_sales'], s['qty'], s['revenue'],
        ])
    w.writerow([])

    w.writerow(["TOVAR AYLANMASI (Top sotilganlar)"])
    w.writerow(['Kod', 'Mahsulot', 'Kunlik o\'rtacha (dona)', 'Hozir omborda', 'Necha kun yetadi'])
    for t in ctx['turnover']:
        dleft = f"{t['days_left']:.0f}" if t['days_left'] is not None else '—'
        w.writerow([t['code'], t['name'], f"{t['daily_avg']:.2f}", t['stock'], dleft])
    w.writerow([])

    w.writerow(["OMBORDA TUGAGAN"])
    w.writerow(['Filial', 'Kod', "O'lcham", 'Rang'])
    for o in ctx['out_of_stock']:
        w.writerow([o.branch.name, o.variant.product.code, o.variant.size, o.variant.color])
    return response


def _insights_pdf(ctx):
    response = HttpResponse(content_type='application/pdf')
    fn = f"Biznes_tahlili_{ctx['d_start']}_{ctx['d_end']}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="Biznes tahlili",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('H1', parent=styles['Title'], fontSize=18, spaceAfter=2, alignment=0)
    h2 = ParagraphStyle('H2', parent=styles['Heading3'], fontSize=12,
                        spaceBefore=8, spaceAfter=4, textColor=colors.HexColor('#0d6efd'))
    meta = ParagraphStyle('M', parent=styles['Normal'], fontSize=9,
                          textColor=colors.HexColor('#666'))

    branch_str = ctx['selected_branch'].name if ctx['selected_branch'] else 'Barcha filiallar'
    elements = [
        Paragraph("yurit — Biznes tahlili", h1),
        Paragraph(f"Davr: <b>{ctx['d_start']} — {ctx['d_end']}</b> ({ctx['days']} kun) &nbsp; | &nbsp; "
                  f"Filial: <b>{branch_str}</b> &nbsp; | &nbsp; "
                  f"Yaratildi: {timezone.localtime().strftime('%Y-%m-%d %H:%M')}", meta),
        Spacer(1, 4*mm),
    ]

    def header_style(t):
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbb')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1),
             [colors.white, colors.HexColor('#f4f6fa')]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        return t

    # KPI table
    elements.append(Paragraph("Asosiy ko'rsatkichlar", h2))
    kpi = [
        ['Ko\'rsatkich', 'Qiymat', 'Oldingi davr', "O'sish %"],
        ["Daromad (so'm)", _fmt(ctx['revenue']), _fmt(ctx['prev_revenue']),
         f"{ctx['rev_growth']:+.1f}%" if ctx['rev_growth'] is not None else '—'],
        ["Tannarx (so'm)", _fmt(ctx['total_cost']), '—', '—'],
        ["Sof foyda (so'm)", _fmt(ctx['profit']), '—', '—'],
        ['Foyda marjasi (%)', f"{ctx['margin']:.2f}%", '—', '—'],
        ['Sotuvlar', _fmt(ctx['sales_count']), _fmt(ctx['prev_n']),
         f"{ctx['sales_growth']:+.1f}%" if ctx['sales_growth'] is not None else '—'],
        ['Dona', _fmt(ctx['qty']), _fmt(ctx['prev_qty']),
         f"{ctx['qty_growth']:+.1f}%" if ctx['qty_growth'] is not None else '—'],
        ["O'rtacha sotuv (so'm)", _fmt(ctx['avg_sale_value']), '—', '—'],
    ]
    elements.append(header_style(Table(kpi, repeatRows=1,
                                       colWidths=[55*mm, 40*mm, 40*mm, 35*mm])))

    # Filial taqqoslash
    if ctx['branch_compare']:
        elements.append(Paragraph("Filiallar taqqoslash", h2))
        data = [['Filial', "Daromad", "Foyda", 'Marja', 'Sotuvlar', 'Dona', 'Omborda']]
        for b in ctx['branch_compare']:
            data.append([
                b['branch'].name, _fmt(b['revenue']), _fmt(b['profit']),
                f"{b['margin']:.1f}%", _fmt(b['sales_count']),
                _fmt(b['qty']), _fmt(b['stock']),
            ])
        elements.append(header_style(Table(data, repeatRows=1)))

    # Top by revenue
    elements.append(Paragraph("TOP-10 mahsulot (daromad bo'yicha)", h2))
    data = [['#', 'Kod', 'Mahsulot', 'Dona', 'Sotuvlar', "Daromad"]]
    for i, p in enumerate(ctx['top_products'], 1):
        data.append([str(i), p['variant__product__code'],
                     p['variant__product__name'][:40],
                     _fmt(p['qty']), _fmt(p['n_sales']), _fmt(p['revenue'])])
    elements.append(header_style(Table(data, repeatRows=1)))

    # Top by profit
    elements.append(Paragraph("TOP-10 mahsulot (sof foyda bo'yicha)", h2))
    data = [['#', 'Kod', 'Mahsulot', "Daromad", "Foyda", 'Marja']]
    for i, p in enumerate(ctx['top_profit'], 1):
        data.append([str(i), p['code'], p['name'][:40],
                     _fmt(p['revenue']), _fmt(p['profit']),
                     f"{p['margin']:.1f}%"])
    elements.append(header_style(Table(data, repeatRows=1)))

    # Slow movers
    if ctx['slow_products']:
        elements.append(Paragraph("Sotilmayotgan mahsulotlar", h2))
        data = [['Kod', 'Mahsulot', 'Kategoriya', 'Omborda']]
        for p in ctx['slow_products']:
            data.append([p.code, p.name[:40],
                         p.category.name if p.category else '—',
                         _fmt(p.stock or 0)])
        elements.append(header_style(Table(data, repeatRows=1)))

    # Top sellers
    elements.append(Paragraph("Top-10 sotuvchilar", h2))
    data = [['#', 'Username', 'Filial', 'Sotuvlar', 'Dona', "Daromad"]]
    for i, s in enumerate(ctx['top_sellers'], 1):
        data.append([str(i), s['sold_by__username'],
                     s['sold_by__branch__name'] or '—',
                     _fmt(s['n_sales']), _fmt(s['qty']), _fmt(s['revenue'])])
    elements.append(header_style(Table(data, repeatRows=1)))

    # Turnover
    if ctx['turnover']:
        elements.append(Paragraph("Tovar aylanmasi", h2))
        data = [['Kod', 'Mahsulot', 'Kunlik o\'rtacha', 'Omborda', 'Necha kun yetadi']]
        for t in ctx['turnover']:
            dleft = f"{t['days_left']:.0f} kun" if t['days_left'] is not None else '—'
            data.append([t['code'], t['name'][:40],
                         f"{t['daily_avg']:.2f}", _fmt(t['stock']), dleft])
        elements.append(header_style(Table(data, repeatRows=1)))

    def _footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#999'))
        canvas.drawRightString(doc_obj.pagesize[0] - 15*mm, 8*mm,
                               f"yurit — bet {doc_obj.page}")
        canvas.restoreState()

    doc.build(elements, onFirstPage=_footer, onLaterPages=_footer)
    response.write(buf.getvalue())
    buf.close()
    return response


def _csv_response(title, headers, rows, summary, d_start, d_end, branch):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    fn = f"{title.replace(' ', '_')}_{d_start}_{d_end}.csv"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'
    response.write('﻿')  # BOM for Excel
    writer = csv.writer(response)
    writer.writerow([title])
    writer.writerow([f"Davr: {d_start} — {d_end}"])
    if branch:
        writer.writerow([f"Filial: {branch.name}"])
    else:
        writer.writerow(["Filial: Barchasi"])
    writer.writerow([])
    writer.writerow(headers)
    for r in rows:
        writer.writerow(r)
    writer.writerow([])
    writer.writerow(['Xulosa:'])
    for k, v in summary.items():
        writer.writerow([k, v])
    return response


def _fmt(v):
    """PDF jadval uchun qiymatni formatlash."""
    if v is None:
        return ''
    if hasattr(v, 'quantize'):  # Decimal
        return f"{v:,.0f}".replace(',', ' ')
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:,}".replace(',', ' ')
    return str(v)


def _pdf_response(title, headers, rows, summary, d_start, d_end, branch):
    response = HttpResponse(content_type='application/pdf')
    fn = f"{title.replace(' ', '_').replace(chr(39), '')}_{d_start}_{d_end}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{fn}"'

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=12*mm, rightMargin=12*mm,
        topMargin=12*mm, bottomMargin=12*mm,
        title=title,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'TitleU', parent=styles['Title'], fontSize=16, spaceAfter=4, alignment=0,
    )
    meta_style = ParagraphStyle(
        'MetaU', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#555'),
    )

    elements = []
    elements.append(Paragraph(title, title_style))
    branch_str = branch.name if branch else "Barcha filiallar"
    elements.append(Paragraph(
        f"Davr: <b>{d_start} — {d_end}</b> &nbsp; | &nbsp; "
        f"Filial: <b>{branch_str}</b> &nbsp; | &nbsp; "
        f"Yaratildi: {timezone.localtime().strftime('%Y-%m-%d %H:%M')}",
        meta_style,
    ))
    elements.append(Spacer(1, 6*mm))

    data = [list(headers)]
    for row in rows:
        data.append([_fmt(c) for c in row])
    if len(data) == 1:
        data.append(['—'] + [''] * (len(headers) - 1))

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.white, colors.HexColor('#f4f6fa')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(table)

    if summary:
        elements.append(Spacer(1, 8*mm))
        elements.append(Paragraph("<b>Xulosa:</b>", styles['Heading4']))
        summary_data = [[k, _fmt(v)] for k, v in summary.items()]
        st = Table(summary_data, colWidths=[100*mm, 60*mm])
        st.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f4f6fa')),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(st)

    def _page_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#999'))
        canvas.drawRightString(
            doc_obj.pagesize[0] - 12*mm, 8*mm,
            f"yurit Ombor Boshqaruv  -  bet {doc_obj.page}"
        )
        canvas.restoreState()

    doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)
    response.write(buf.getvalue())
    buf.close()
    return response
