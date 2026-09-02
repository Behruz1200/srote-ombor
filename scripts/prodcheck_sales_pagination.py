import re

import django
django.setup()
from django.test.utils import setup_test_environment
setup_test_environment()          # r.context to'lishi uchun kerak

from django.test import Client
from inventory.models import User

a = User.objects.filter(role='admin', is_active=True).first()
print('admin:', a)
c = Client()
c.force_login(a)
for url in ['/sales/', '/sales/?page=2', '/sales/?view=items',
            '/sales/?view=items&page=3', '/sales/?view=items&page=99999']:
    r = c.get(url, HTTP_HOST='koreysbozor.uz', secure=True)
    ctx = r.context or {}
    po = ctx.get('page_obj')
    body = r.content.decode('utf-8', 'ignore')
    print('%-32s %s  sahifa=%s/%s  qator=%s  jami=%s  cheklar=%s  eski300=%s' % (
        url, r.status_code,
        getattr(po, 'number', None),
        getattr(getattr(po, 'paginator', None), 'num_pages', None),
        len(ctx.get('checks') or ctx.get('sales') or []),
        ctx.get('total'), ctx.get('check_count'),
        "eng so'nggi 300" in body))
