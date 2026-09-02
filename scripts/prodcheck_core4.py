"""CORE-4/6 — prodda tekshiruv: sahifa qobig'i va umumiy JS."""
import re
from django.test import Client
from inventory.models import User

u = User.objects.filter(role='admin').first()
c = Client(SERVER_NAME='koreysbozor.uz')
c.force_login(u)

URLS = ['/', '/branches/', '/sales/', '/reports/', '/insights/', '/prices/',
        '/prices/history/', '/products/', '/audit/', '/warehouse/',
        '/customers/', '/users/', '/categories/', '/shifts/', '/pos/',
        '/intake/variants/', '/intake/', '/stocktake/', '/transfers/',
        '/cart/', '/lookup/', '/price-labels/']
bad = 0
print('--- sahifalar ---')
for url in URLS:
    r = c.get(url, secure=True, follow=True)
    h = r.content.decode()
    hero = h.count('dash-hero__glow')
    js = 'js/yurit-common.js' in h
    ok = r.status_code == 200 and js
    bad += not ok
    print(f'  {"OK  " if ok else "XATO"} {url:20s} {r.status_code}  '
          f'hero={hero}  common.js={js}')

print()
print("--- eskirgan JS qoliplari chizilgan sahifada qolmadimi ---")
# DIQQAT: `<header class="dash-hero">` ni bu yerda TEKSHIRIB BO'LMAYDI —
# uni {% hero %} tegining O'ZI chizadi. Manbadagi qo'lda yozilgan hero
# uchun alohida test bor: Core4SharedPageChrome.
h_all = ''.join(c.get(u_, secure=True, follow=True).content.decode()
                for u_ in URLS)
for label, pat in (("o'z num() nusxasi", r'function num\(s\)'),
                   ("o'z qisqa formati", r"\+ 'mlrd'"),
                   ("eskirgan CSRF", r"CSRF = '\{\{")):
    n = len(re.findall(pat, h_all))
    bad += n > 0
    print(f'  {"OK  " if not n else "XATO"} {label:22s} {n} ta')

print()
print('XATO:', bad)
