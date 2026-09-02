# QO'LDA CHEGIRMA audit — DISC-6
from decimal import Decimal
from datetime import timedelta
from collections import Counter, defaultdict
from django.utils import timezone
from inventory.models import SaleTransaction, Promotion

D = Decimal
today = timezone.localdate()
start = today - timedelta(days=30)

qs = (SaleTransaction.objects
      .filter(sold_at__date__gte=start, sold_at__date__lte=today,
              order_discount__gt=0)
      .select_related('sold_by', 'branch')
      .prefetch_related('lines'))

print(f"OYNA: {start} .. {today}   (Promotion jami: {Promotion.objects.count()})")
print(f"order_discount>0 cheklar: {qs.count()}")

manual, promo, exch = [], [], []
for t in qs:
    if D(t.exchange_credit) > 0:
        exch.append(t)
    elif D(t.promo_discount) > 0:
        promo.append(t)
    else:
        manual.append(t)

def tot(rows, attr='order_discount'):
    return sum((D(getattr(r, attr)) for r in rows), D('0'))

print(f"  almashtirish : {tot(exch,'exchange_credit'):>14,.0f}  ({len(exch)} chek)")
print(f"  aksiya       : {tot(promo,'promo_discount'):>14,.0f}  ({len(promo)} chek)")
print(f"  QO'LDA       : {tot(manual):>14,.0f}  ({len(manual)} chek)")
print()

# --- 1. Nechta yirik chek ustunlik qiladi?
manual.sort(key=lambda t: -D(t.order_discount))
mt = tot(manual)
run = D('0')
n80 = 0
for i, t in enumerate(manual, 1):
    run += D(t.order_discount)
    if run >= mt * D('0.8'):
        n80 = i
        break
print(f"--- 1. JAMLANISH: summaning 80%i {n80} ta chekdan keladi "
      f"({len(manual)} tadan)")
amts = [D(t.order_discount) for t in manual]
if amts:
    med = amts[len(amts)//2]
    print(f"    median: {med:,.0f}   o'rtacha: {mt/len(amts):,.0f}   "
          f"eng katta: {amts[0]:,.0f}")
print()

print("--- 2. ENG KATTA 15 TA")
print(f"{'sana':<11}{'kassir':<12}{'yalpi':>12}{'chegirma':>12}{'%':>6}"
      f"{'to`lagan':>12}  sabab")
for t in manual[:15]:
    g = D(t.gross) - D(t.line_discount_total)
    od = D(t.order_discount)
    pct = (od / g * 100) if g else D('0')
    print(f"{timezone.localtime(t.sold_at).strftime('%d.%m %H:%M'):<13}"
          f"{(t.sold_by.username if t.sold_by else '?'):<12}"
          f"{g:>12,.0f}{od:>12,.0f}{pct:>5.0f}%{(g-od):>12,.0f}  "
          f"{(t.discount_reason or '(bo`sh)')[:28]}")
print()

# --- 3. Yaxlitlash izi: to'langan summa dumaloqmi, yalpi dumaloq emasmi?
def rnd(v, m):
    return v % m == 0
sig = Counter()
for t in manual:
    g = D(t.gross) - D(t.line_discount_total)
    paid = g - D(t.order_discount)
    if not rnd(g, 1000) and rnd(paid, 1000):
        sig['yalpi dumaloq EMAS -> to`lagan 1000ga karrali'] += 1
    if rnd(paid, 10000) and not rnd(g, 10000):
        sig['to`lagan 10 000ga karrali'] += 1
print("--- 3. YAXLITLASH IZI")
for k, v in sig.items():
    print(f"    {k}: {v} / {len(manual)}")
print()

# --- 4. Aniq foiz izi (5/10/15/20/25/30/50%)
pcts = Counter()
for t in manual:
    g = D(t.gross) - D(t.line_discount_total)
    if not g:
        continue
    p = D(t.order_discount) / g * 100
    for target in (5, 10, 15, 20, 25, 30, 50):
        if abs(p - target) < D('0.6'):
            pcts[f'~{target}%'] += 1
            break
    else:
        pcts['boshqa'] += 1
print("--- 4. FOIZ IZI")
for k, v in sorted(pcts.items(), key=lambda x: -x[1]):
    print(f"    {k}: {v}")
print()

# --- 5. Kassir va kun bo'yicha
by_user, by_day = defaultdict(lambda: [D('0'), 0]), defaultdict(lambda: [D('0'), 0])
for t in manual:
    u = t.sold_by.username if t.sold_by else '?'
    by_user[u][0] += D(t.order_discount); by_user[u][1] += 1
    d = timezone.localtime(t.sold_at).date()
    by_day[d][0] += D(t.order_discount); by_day[d][1] += 1
print("--- 5. KASSIR BO'YICHA")
for u, (s, c) in sorted(by_user.items(), key=lambda x: -x[1][0]):
    print(f"    {u:<14}{s:>14,.0f}  ({c} chek)")
print()
print("--- 6. ENG KATTA 10 KUN")
for d, (s, c) in sorted(by_day.items(), key=lambda x: -x[1][0])[:10]:
    print(f"    {d}  {s:>12,.0f}  ({c} chek)")
print()

# --- 7. Sababli / sababsiz
withr = [t for t in manual if (t.discount_reason or '').strip()]
print(f"--- 7. sabab BOR: {tot(withr):,.0f} ({len(withr)} chek) | "
      f"sabab BO'SH: {tot([t for t in manual if not (t.discount_reason or '').strip()]):,.0f} "
      f"({len(manual)-len(withr)} chek)")
reasons = Counter((t.discount_reason or '').strip()[:40] for t in withr)
for r, c in reasons.most_common(10):
    print(f"    {c:>4}x  {r}")

# --- 8. Kontekst: qator chegirmasi VA tushumga nisbati
from django.db.models import Sum, F, DecimalField, ExpressionWrapper
from inventory.models import Sale
sq = Sale.objects.filter(sold_at__date__gte=start, sold_at__date__lte=today)
agg = sq.aggregate(
    yalpi=Sum(ExpressionWrapper(F('quantity') * F('sale_price'),
                                output_field=DecimalField(max_digits=18, decimal_places=2))),
    ld=Sum('line_discount'))
yalpi = D(agg['yalpi'] or 0)
ld = D(agg['ld'] or 0)
print()
print("--- 8. KONTEKST")
print(f"    yalpi (qator narxlari)      : {yalpi:>14,.0f}")
print(f"    QATOR chegirmasi (kartada YO'Q): {ld:>14,.0f}")
print(f"    CHEK chegirmasi qo'lda      : {mt:>14,.0f}"
      f"   = tushumning {(mt/(yalpi-ld)*100 if yalpi>ld else 0):.2f}%i")
zero = [t for t in manual if (D(t.gross) - D(t.line_discount_total) - D(t.order_discount)) <= 0]
print(f"    to'liq bepul ketgan chek    : {len(zero)}")

# --- 9. Butun tarix (30 kun anomalmi?)
allq = SaleTransaction.objects.filter(order_discount__gt=0)
tot_all = allq.aggregate(s=Sum('order_discount'))['s'] or 0
print()
print(f"--- 9. BUTUN TARIX: order_discount jami {D(tot_all):,.0f} ({allq.count()} chek); "
      f"shundan oxirgi 30 kun {tot(manual)+tot(exch,'exchange_credit')+tot(promo,'promo_discount'):,.0f}")
