"""14 ta past marjali qatorda tannarx = sotuv / 1.20.

Skript o'zi hisoblamaydi — ilovaning O'Z endpointini (/prices/apply/)
admin sifatida chaqiradi. Ya'ni tugmani bosgan bilan bir xil yo'l:
sinovdan o'tgan kod, bir xil yaxlitlash, bo'laklangan qulf (STK-15) va
narx tarixiga KIM o'zgartirgani yoziladi.
"""
from decimal import Decimal
import django
django.setup()
from django.db.models import F
from django.test import Client
from django.test.utils import setup_test_environment
setup_test_environment()
from inventory.models import BranchStock, User, AuditLog

LOW = dict(cost_price__gt=0, sale_price__gt=0)

def snap():
    qs = BranchStock.objects.filter(**LOW).filter(
        sale_price__lt=F('cost_price') * Decimal('1.10'))
    return {s.pk: (s.cost_price, s.sale_price) for s in qs}

before = snap()
print("Avval — marja past (<10%%): %d ta qator" % len(before))

admin = User.objects.filter(role='admin', is_active=True).order_by('pk').first()
print("Kim nomidan:", admin.username)

audit_before = AuditLog.objects.count()

c = Client()
c.force_login(admin)
r = c.post('/prices/apply/', {
    'back': '/prices/?q=&category=&issue=low',
    'mode': 'bulk',
    'op': 'cost_from_sale',      # tannarx = sotuv / (1 + %)
    'scope': 'filtered',         # filtrdagi HAMMASIGA
    'pct': '20',
    'q': '', 'category': '', 'issue': 'low',
}, HTTP_HOST='koreysbozor.uz', secure=True, follow=False)
print("Javob:", r.status_code, '->', r.headers.get('Location'))

after = {s.pk: (s.cost_price, s.sale_price)
         for s in BranchStock.objects.filter(pk__in=list(before))}
n = 0
print()
print("%-9s %12s %12s %12s %9s" % ('ID', 'ESKI TAN', 'YANGI TAN', 'SOTUV', 'YANGI M'))
for pk, (oc, os_) in sorted(before.items()):
    nc, ns = after[pk]
    if nc != oc:
        n += 1
        m = (ns / nc - 1) * 100
        print("%-9d %12s %12s %12s %8.1f%%" %
              (pk, f'{oc:,.0f}', f'{nc:,.0f}', f'{ns:,.0f}', m))
print()
print("O'zgargan qator:", n)
print("Narx tarixiga yozildi:", AuditLog.objects.count() - audit_before, "ta yozuv")
still = BranchStock.objects.filter(**LOW).filter(
    sale_price__lt=F('cost_price') * Decimal('1.10')).count()
print("Hozir marja past (<10%) qolgan:", still)
