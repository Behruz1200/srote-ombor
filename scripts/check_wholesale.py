from decimal import Decimal
import django
django.setup()
from django.db.models import F, Count, Q
from inventory.models import BranchStock

qs = BranchStock.objects.all()
tot = qs.count()
print('Jami qator                     :', tot)
print('ulgurji = 0 / yo\'q             :', qs.filter(wholesale_price__lte=0).count())
print('ulgurji == chakana (AYNAN teng) :', qs.filter(wholesale_price__gt=0,
      wholesale_price=F('sale_price')).count())
print('ulgurji > chakana (qimmatroq!)  :', qs.filter(wholesale_price__gt=0).filter(
      wholesale_price__gt=F('sale_price')).count())
print('ulgurji < chakana (to\'g\'ri)     :', qs.filter(wholesale_price__gt=0,
      wholesale_price__lt=F('sale_price')).count())
print()
print("Zaxirasi bor tovarlar orasida (POS'da sotiladiganlar):")
ins = qs.filter(stock_count__gt=0)
print('  zaxirada bor               :', ins.count())
print('  ulgurji yo\'q (0)           :', ins.filter(wholesale_price__lte=0).count())
print('  ulgurji == chakana         :', ins.filter(wholesale_price__gt=0,
      wholesale_price=F('sale_price')).count())
print('  ulgurji > chakana          :', ins.filter(wholesale_price__gt=0).filter(
      wholesale_price__gt=F('sale_price')).count())
print('  ulgurji < chakana          :', ins.filter(wholesale_price__gt=0,
      wholesale_price__lt=F('sale_price')).count())
print()
print("Namuna — ulgurji CHAKANADAN QIMMAT yoki TENG (zaxirada bor):")
bad = (ins.filter(wholesale_price__gt=0, wholesale_price__gte=F('sale_price'))
       .select_related('variant__product')[:12])
for s in bad:
    print('   %-11s %-26s chakana=%9s ulgurji=%9s' % (
        s.variant.product.code, s.variant.product.name[:26],
        f'{s.sale_price:,.0f}', f'{s.wholesale_price:,.0f}'))
