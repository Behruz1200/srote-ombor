"""CORE-3 — prodda tekshiruv: sahifalash filtrni saqlaydimi."""
import re
from django.test import Client
from inventory.models import User

u = User.objects.filter(role='admin').first()
c = Client(SERVER_NAME='koreysbozor.uz')
c.force_login(u)

CASES = [
    ('/audit/', {'model': 'BranchStock', 'date_from': '2026-08-01'}),
    ('/audit/', {'q': 'a'}),
    ('/prices/history/', {}),
    ('/prices/', {'issue': 'no_cost'}),
    ('/sales/', {'date_from': '2026-08-01'}),
]
LINK = re.compile(r'class="page-link"\s+href="([^"]+)"')
bad = 0
for url, p in CASES:
    r = c.get(url, p, secure=True)
    h = r.content.decode()
    links = [x for x in LINK.findall(h) if 'page=' in x]
    if not links:
        print(f'{url:20s} {p} -> bitta sahifa (status {r.status_code})')
        continue
    link = links[0]
    miss = [k for k in p if f'{k}=' not in link]
    flag = 'XATO' if miss else 'OK  '
    bad += bool(miss)
    print(f'{flag} {url:20s} {p}')
    print(f'      havola: {link[:150]}')
    if miss:
        print(f'      YO\'QOLGAN: {miss}')
print()
print('XATO:', bad)
