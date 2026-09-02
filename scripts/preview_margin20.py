"""FAQAT KO'RSATADI — hech narsa o'zgartirmaydi."""
from decimal import Decimal
import django
django.setup()
from django.db.models import F
from inventory.models import BranchStock, Sale

PCT = Decimal('20')
factor = Decimal('1') + PCT / Decimal('100')
q2 = Decimal('0.01')

qs = (BranchStock.objects
      .filter(cost_price__gt=0, sale_price__gt=0,
              sale_price__lt=F('cost_price') * Decimal('1.10'))
      .select_related('variant__product', 'branch')
      .order_by('variant__product__code'))

print("Marja past (<10%%) qatorlar: %d ta\n" % qs.count())
hdr = "%-11s %-26s %10s %10s %7s   %10s %7s %10s"
print(hdr % ('KOD', 'MAHSULOT', 'TANNARX', 'SOTUV', 'MARJA', 'YANGI TAN', 'MARJA', "TANNARX-Δ"))
print('-' * 108)
old_sum = Decimal('0'); new_sum = Decimal('0'); n = 0
for s in qs:
    new = (s.sale_price / factor).quantize(q2)
    if new == s.cost_price:
        continue
    m_old = (s.sale_price / s.cost_price - 1) * 100
    n += 1
    old_sum += s.cost_price
    new_sum += new
    print(hdr % (s.variant.product.code,
                 s.variant.product.name[:26],
                 f'{s.cost_price:,.0f}', f'{s.sale_price:,.0f}', f'{m_old:.1f}%',
                 f'{new:,.0f}', '20.0%', f'{new - s.cost_price:+,.0f}'))
print('-' * 108)
print("O'zgaradigan qator: %d" % n)
print("Tannarx yig'indisi : %,.0f  ->  %,.0f  (%+,.0f)".replace('%,', '%').replace(',.0f', ',.0f')
      % (old_sum, new_sum, new_sum - old_sum) if False else
      "Tannarx yig'indisi : {:,.0f}  ->  {:,.0f}  ({:+,.0f})".format(old_sum, new_sum, new_sum - old_sum))
print()
print("Tarixga ta'siri: Sale.cost_at_sale har sotuvda ALOHIDA saqlanadi,")
print("shuning uchun O'TGAN sotuvlar foydasi O'ZGARMAYDI. Faqat bundan")
print("keyingi sotuvlar yangi tannarx bilan hisoblanadi.")
print("Tekshiruv — cost_at_sale maydoni bor:", hasattr(Sale, 'cost_at_sale'))
