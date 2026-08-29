"""ARCH-1 — pul va hisobot mantiqi bitta joyda.

views.py 13 276 qatorga yetdi va shu tufayli bir NAQSH ikki marta ko'zdan
qochdi: aynan bir xil qulflash xatosi ikki xil view'da (pos_refund 27.08,
price_apply 29.08) prodga chiqdi. Naqsh naqsh sifatida ko'rinmasdi.

Bu modulda faqat PUL bilan bog'liq umumiy mantiq turadi: chek chegirmasini
qatorlarga taqsimlash, qaytarish tuzatmasi, yaxlitlash tasnifi, chegirma
sababi va ommaviy amallar uchun xavfsiz qulflash. Hammasi bir joyda
bo'lgani uchun keyingi safar naqshni ko'rish osonroq.

Bu yerda VIEW yo'q va bo'lmaydi — shuning uchun views.py ni import
qilmaydi va aylanma import xavfi yo'q.
"""
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

from .models import BranchStock, Return, Sale, SaleTransaction, _dec


DISCOUNT_REASONS = (
    "Ko'p tovar oldi",
    "Yaxlitlash",
    "Nuqson / kamchilik",
    "Doimiy mijoz",
    "Rahbar ruxsati",
)


def _valid_discount_reason(reason):
    """DISC-7. Ro'yxatdagi sabab yoki hech bo'lmasa mazmunli erkin matn.

    Server ESKI mijozni ham qabul qiladi. Sabab: bu PWA — service worker
    keshida qolgan eski sahifa erkin matn yuboradi, qat'iy ro'yxat esa
    kassani ish o'rtasida to'xtatib qo'yardi. Shu bois faqat ma'nosizini
    rad etamiz: bitta-ikkita belgi ("s", "c") va faqat raqam ("3000" — bu
    sabab emas, kassir summani qayta terган).
    """
    head = (reason or '').split(';')[0].strip()
    if head in DISCOUNT_REASONS:
        return True
    if head.lower().startswith('boshqa'):
        head = head.split(':', 1)[-1].strip()
    if len(head) < 3:
        return False
    if head.replace(' ', '').replace("'", '').isdigit():
        return False
    return True


# DISC-6: "yaxlitlash" — kassir chekni butun songa tushirish uchun olib
# tashlagan mayda qoldiq (2 000 so'mlik chekда 500 so'm). Bu chegirma emas,
# maydalik: hisobotда uni ulgurji chegirma bilan bir songa qo'shish egani
# chalg'itadi. Auditда ko'rilgani: 308 chekdan 264 tasi shu xil, medianasi
# 1 000 so'm, hammasi birga jamining atigi 20%i.
ROUNDING_MAX = Decimal('5000')     # bundan kattasi yaxlitlash emas
ROUNDING_STEP = Decimal('1000')    # mijoz to'lagani shu songa karrali bo'lsa


def _is_rounding(manual, gross, paid, reason=''):
    """DISC-6. Mayda summa VA chegirma jamini BUTUN songa keltirgan bo'lsa.

    Uch shart ham kerak:

      mayda            — 1 705 000 -> 1 535 000 (170 000) yaxlitlash emas,
                         bu ulgurji chegirma;
      jami butun bo'ldi — 19 500 -> 19 000;
      jami butun EMAS EDI — 100 000 lik chekда 3 000 chegirma ham "butun"
                         qoldiradi (97 000), lekin u yaxlitlash emas: summa
                         allaqachon butun edi, kassir ataylab chegirma bergan.
                         Uchinchi shartsiz karta shu xil cheklarni yaxlitlash
                         deb yashirib qo'yardi — chegirmani KAM ko'rsatish esa
                         ko'p ko'rsatishdан battar.

    DISC-7 dan keyin kassir sababni O'ZI "Yaxlitlash" deb belgilaydi va taxmin
    kerak bo'lmaydi. Taxmin faqat eski cheklar uchun qoladi. Belgilangan sabab
    ham mayda summa cheklovidan o'tadi: "Yaxlitlash" deb 50 000 yozilsa, u
    kartada QO'LDA bo'lib ko'rinishi kerak.
    """
    if manual <= 0 or manual > ROUNDING_MAX:
        # DISC-8: chegara ICHIGA oladi — POS'dagi yaxlitlash tugmasi ham
        # aynan 5 000 gacha taklif qiladi (85 000 -> 80 000 = 5 000).
        # '>=' bo'lsa o'sha tugma bosilgan chek "qo'lda chegirma" bo'lib
        # ko'rinardi, holbuki u YAXLITLASH deb belgilangan.
        return False
    if (reason or '').strip().lower().startswith('yaxlitlash'):
        return True
    if paid <= 0 or gross <= 0:
        return False
    return paid % ROUNDING_STEP == 0 and gross % ROUNDING_STEP != 0


def _order_discount_share(sale_qs, *group_fields, split=False):
    """Filtrlangan sotuv qatorlariga to'g'ri keladigan CHEK chegirmasi ulushi.

    SAL-1. Sale qatorlarida faqat `line_discount` bor. CHEK bo'yicha umumiy
    chegirma (`order_discount`) SaleTransaction'da turadi, shuning uchun
    qatorlar ustidan Sum() olgan HAR BIR sahifa mijoz TO'LAGANIDAN ko'p
    ko'rsatardi:

        3×100 000, chek chegirmasi 60 000 (mijoz 240 000 to'lagan)
          qatorlar Sum'i         300 000
          chek jamisi (t.total)  240 000   ← to'g'risi

    QAYSI o'lchov bo'yicha ayirish mumkin — muhim farq bor:

      CHEK darajasidagi o'lchovlar (filial, sotuvchi, mijoz, kun): bitta chek
      AYNAN bitta filialga/sotuvchiga/kunga tegishli, shuning uchun chegirma
      shu guruhga to'liq va aniq tushadi — to'g'ridan-to'g'ri ayiriladi.

      QATOR darajasidagi o'lchovlar (mahsulot, kategoriya, guruh): bitta chek
      bir nechta mahsulotni qamraydi va egasining qoidasi bo'yicha (REF-3,
      net_line_total'ga qarang) chek chegirmasi ALOHIDA tovarga tegishli
      EMAS. Shuning uchun bunday ro'yxatlarda qatorlar O'Z narxida qoladi va
      chegirma ALOHIDA qator sifatida ko'rsatiladi:
          mahsulotlar 300 000 + "Chek chegirmasi −60 000" = JAMI 240 000

    Filtr chekning bir qismini tanlasa — faqat o'sha qismning ulushi olinadi
    (bitta mahsulot bo'yicha filtr butun chek chegirmasini yeb qo'ymasin).
    Faqat `order_discount > 0` bo'lgan cheklar so'raladi — chegirmasiz
    sahifada qo'shimcha yuk amalda yo'q.

    Qaytaradi: (jami_ulush, {kalit: ulush}). `group_fields` bitta bo'lsa kalit
    — skalyar, bir nechta bo'lsa — tuple, umuman berilmasa — bo'sh dict.

    `split=True` bo'lsa uchinchi qiymat ham qaytadi (DISC-1/DISC-6):
    {'promo', 'rounding', 'manual', 'exchange', 'total'} — chek chegirmasining
    qismlari.
    Ular bitta songa yig'ilgan holда hisobotlar egasi sozlagan AKSIYAni kassir
    o'zi bergan chegirmadan ajrata olmasdi, almashtirish krediti esa umuman
    chegirma bo'lmasa ham "chegirma" bo'lib ko'rinardi.
    """
    rev = F('quantity') * F('sale_price') - F('line_discount')
    # DISC-1: uchala qismni BIR so'rovда olamiz (qo'shimcha so'rov yo'q).
    disc_rows = list(SaleTransaction.objects
                     .filter(id__in=sale_qs.order_by().values('transaction_id'),
                             order_discount__gt=0)
                     .values_list('id', 'order_discount',
                                  'promo_discount', 'exchange_credit',
                                  'discount_reason'))
    if not disc_rows:
        empty = {'promo': Decimal('0'), 'rounding': Decimal('0'),
                 'manual': Decimal('0'), 'exchange': Decimal('0'),
                 'total': Decimal('0')}
        return (Decimal('0'), {}, empty) if split else (Decimal('0'), {})
    disc = {r[0]: _dec(r[1]) for r in disc_rows}
    ids = list(disc)
    # Chekning TO'LIQ summasi (filtrdan qat'i nazar) — maxraj.
    whole = {r['transaction_id']: _dec(r['s'] or 0)
             for r in (Sale.objects.filter(transaction_id__in=ids)
                       .order_by().values('transaction_id').annotate(s=Sum(rev)))}
    parts = {}
    for _id, _tot, _promo, _exch, _reason in disc_rows:
        _tot, _promo, _exch = _dec(_tot), _dec(_promo or 0), _dec(_exch or 0)
        _manual = _tot - _promo - _exch
        if _manual < 0:
            _manual = Decimal('0')
        # DISC-6: mayda qoldiqni AJRATAMIZ. Bitta songa yig'ilganда karta
        # 1 000 so'mlik yaxlitlash bilan 186 500 so'mlik ulgurji chegirmani
        # o'rtachalab, egaga "biz bunchalik chegirma bermaganmiz" degan
        # tuyg'u berardi. Ular boshqa-boshqa hodisa — alohida ko'rsatiladi.
        _round = Decimal('0')
        _gross = whole.get(_id) or Decimal('0')
        if _is_rounding(_manual, _gross, _gross - _tot, _reason):
            _round, _manual = _manual, Decimal('0')
        parts[_id] = (_promo, _round, _manual, _exch)
    fields = ['transaction_id'] + list(group_fields)
    picked = (sale_qs.filter(transaction_id__in=ids)
              .order_by().values(*fields).annotate(s=Sum(rev)))
    total = Decimal('0')
    by_group = {}
    acc = {'promo': Decimal('0'), 'rounding': Decimal('0'),
           'manual': Decimal('0'), 'exchange': Decimal('0'),
           'total': Decimal('0')}
    for r in picked:
        tid = r['transaction_id']
        w = whole.get(tid) or Decimal('0')
        if w <= 0:
            continue
        frac = _dec(r['s'] or 0) / w        # chekning tanlangan ulushi
        share = _dec(disc[tid]) * frac
        total += share
        if split:
            _p, _r, _m, _e = parts[tid]
            acc['promo'] += _p * frac
            acc['rounding'] += _r * frac
            acc['manual'] += _m * frac
            acc['exchange'] += _e * frac
            acc['total'] += share
        if group_fields:
            key = (r[group_fields[0]] if len(group_fields) == 1
                   else tuple(r[g] for g in group_fields))
            by_group[key] = by_group.get(key, Decimal('0')) + share
    return (total, by_group, acc) if split else (total, by_group)


def _returns_adjustment(sale_qs, *group_fields):
    """Qaytarilgan tovarlar uchun TUSHUM va TANNARX tuzatmasi (RET-1).

    Muammo: `/sales/` dan boshqa HECH BIR sahifa qaytarishni hisobga
    olmasdi — na tushumда, na tannarxда. 3 dona sotilib 1 dona qaytsa,
    hamma joyда 3 donaning tushumi ham, tannarxi ham turaverardi.

    Ikki tuzatma kerak, va ular BOSHQA-BOSHQA:

      TUSHUM  — kassaдан HAQIQATDA chiqqan pul (effective_cash_refund).
        Almashtirishда naqd chiqmaydi (0), demak tushum kamaymaydi —
        to'g'ri, chunki mijoz pulni oldingi chekда to'lagan va u
        do'kondа qoladi.

      TANNARX — tovar OMBORGA QAYTGAN bo'lsagina qaytariladi. Qaytgan
        tovar endi sotilgan emas, u zaxira; tannarxi COGSда qolsa
        ikki marta sanaladi. `pos_refund` ochiq narxli (is_open_price)
        tovarni omborga TIKLAMAYDI — demak ularning tannarxi COGSда
        qolishi KERAK (tovar ham ketdi, pul ham qaytdi: haqiqiy zarar).

    Shu ikki qoida almashtirishni ham, oddiy qaytarishni ham bir xil
    to'g'ri hisoblaydi:
        almashtirish : tushum −0,      tannarx −45 000  -> foyda to'g'ri
        oddiy qaytish: tushum −80 500, tannarx −45 000  -> foyda 0
        ochiq narxli : tushum −80 500, tannarx −0       -> foyda −45 000

    O'lchovlar bo'yicha guruhlash ANIQ: har Return bitta Sale qatoriga
    tegishli, u qatorда esa bitta filial/sotuvchi/mahsulot/kategoriya bor.
    Shu bois chek chegirmasidan farqli o'laroq bu yerда taqsimlash yo'q.

    Qaytaradi: (tushum_krediti, tannarx_krediti, {kalit: (tushum, tannarx)})
    """
    rets = (Return.objects.filter(sale__in=sale_qs.order_by().values('pk'))
            .select_related('sale__variant__product__category__group',
                            'sale__transaction')
            .prefetch_related('sale__transaction__lines'))
    rev_credit = Decimal('0')
    cost_credit = Decimal('0')
    by_group = {}
    for r in rets:
        sale = r.sale
        _rev = _dec(r.effective_cash_refund or 0)
        # Omborga qaytmagan tovarning tannarxi COGSда qoladi.
        _cost = (Decimal('0') if sale.variant.product.is_open_price
                 else _dec(r.quantity) * _dec(sale.cost_at_sale))
        rev_credit += _rev
        cost_credit += _cost
        if group_fields:
            key = (_ret_group_value(sale, group_fields[0]) if len(group_fields) == 1
                   else tuple(_ret_group_value(sale, g) for g in group_fields))
            cur = by_group.get(key) or (Decimal('0'), Decimal('0'))
            by_group[key] = (cur[0] + _rev, cur[1] + _cost)
    return rev_credit, cost_credit, by_group


def _ret_group_value(sale, field):
    """`_returns_adjustment` uchun guruh kalitini Sale qatoridan oladi."""
    if field == 'branch_id':
        return sale.branch_id
    if field == 'sold_by_id':
        return sale.sold_by_id
    if field == 'sold_at__date':
        return timezone.localtime(sale.sold_at).date()
    if field == 'variant__product_id':
        return sale.variant.product_id
    if field == 'variant__product__category_id':
        return sale.variant.product.category_id
    if field == 'variant__product__category__name':
        cat = sale.variant.product.category
        return cat.name if cat else None
    if field == 'variant__product__category__group__name':
        cat = sale.variant.product.category
        return cat.group.name if (cat and cat.group_id) else None
    raise ValueError(f'_returns_adjustment: qo\'llab-quvvatlanmagan o\'lchov {field}')

def _lock_stocks(qs):
    """STK-14: BranchStock qatorlarini JOIN'SIZ qulflaydi.

    Postgres FOR UPDATE ni outer join'ning NULLABLE tomoniga qo'llay olmaydi:

        psycopg.errors.FeatureNotSupported:
        FOR UPDATE cannot be applied to the nullable side of an outer join

    `_price_qs` 'variant__product__category' ni select_related qiladi,
    `Product.category` esa null=True — demak LEFT OUTER JOIN paydo bo'ladi va
    butun /prices/apply/ 500 beradi. SQLite select_for_update'ni UMUMAN
    e'tiborsiz qoldiradi, shuning uchun test bazasida chiqmaydi va faqat
    prodda ko'rinadi. Aynan shu xato ilgari pos_refund'да ham bo'lgan
    (REF-1 izohiga qarang) — ya'ni bu takrorlanuvchi tuzoq.

    Yechim: qulflanadigan so'rovда JOIN umuman bo'lmasin. Avval PK'larni
    o'qiymiz (bu FOR UPDATE emas, join zarar qilmaydi), keyin faqat PK
    bo'yicha qulflaymiz.
    """
    ids = list(qs.order_by().values_list('pk', flat=True))
    return BranchStock.objects.select_for_update().filter(pk__in=ids)


# STK-15: ommaviy amallar uchun bo'lak o'lchami. Kichikroq = qulf kamroq
# ushlanadi (sotuv kamroq kutadi), kattaroq = kamroq tranzaksiya.
PRICE_CHUNK = 200


def _chunked(seq, n=PRICE_CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
