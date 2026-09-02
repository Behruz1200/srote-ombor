from decimal import Decimal
import django
django.setup()
from django.db.models import F
from django.test.utils import setup_test_environment
setup_test_environment()
from django.test import Client
from inventory.models import User, AuditLog

a = User.objects.filter(role='admin', is_active=True).order_by('pk').first()
c = Client(); c.force_login(a)
r = c.get('/prices/?q=&category=&issue=low', HTTP_HOST='koreysbozor.uz', secure=True)
ctx = r.context or {}
print('sahifa:', r.status_code)
for k in ('c_no_cost', 'c_zero', 'c_loss', 'c_no_sale', 'c_low', 'c_no_ws'):
    if k in ctx:
        print('  %-12s %s' % (k, ctx[k]))
print('  filtrda topildi:', ctx.get('total_count'))
print()
print("Oxirgi narx o'zgarishlari (tarix):")
for lg in (AuditLog.objects.filter(model_name='BranchStock')
           .filter(changes__has_any_keys=['cost_price'])
           .order_by('-created_at')[:4]):
    ch = lg.changes.get('cost_price')
    print('  %s  %-22s %s -> %s   (%s)' % (
        lg.created_at.strftime('%d.%m %H:%M'), lg.object_repr[:22],
        ch[0], ch[1], lg.username_snapshot or '—'))
