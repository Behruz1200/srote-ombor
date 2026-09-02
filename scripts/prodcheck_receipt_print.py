import django
django.setup()
from django.test.utils import setup_test_environment
setup_test_environment()
from django.test import Client
from inventory.models import User, SaleTransaction
a = User.objects.filter(role='admin', is_active=True).first()
t = SaleTransaction.objects.order_by('-id').first()
c = Client(); c.force_login(a)
r = c.get('/transaction/%s/' % t.public_id, HTTP_HOST='koreysbozor.uz', secure=True)
print('chek sahifasi     :', r.status_code)
print('X-Frame-Options   :', r.headers.get('X-Frame-Options'))
csp = r.headers.get('Content-Security-Policy') or ''
print('frame-ancestors   :', [p.strip() for p in csp.split(';') if 'frame-ancestors' in p])
body = r.content.decode('utf-8', 'ignore')
print('fit chaqirilyapti :', 'yuritReceiptPrint' in body)
print('beforeprint bor   :', 'beforeprint' in body)
print('modul yuklanadi   :', 'yurit-receipt-print.js' in body)
r2 = c.get('/pos/', HTTP_HOST='koreysbozor.uz', secure=True)
print('POS X-Frame-Opts  :', r2.headers.get('X-Frame-Options'), '(begona sayt freymlay olmaydi)')
