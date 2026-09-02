"""POS sahifasini HAQIQIY brauzerda ochib, JS xatosi bor-yo'qligini aytadi.

NEGA KERAK. POS sahifasidagi butun JS bitta <script> blokida. Blokning
istalgan joyidagi xato — o'sha joydan KEYINGI barcha tugmalarni o'ldiradi,
lekin xato bergan joy bilan ishlamay qolgan tugma bir-biridan uzoq
bo'lishi mumkin. 30.08 da aynan shunday bo'ldi: "Boshqa summa"
kalkulyatori ishlamay qoldi, sababi esa undan 700 qator yuqorida
`new bootstrap.Modal(...)` edi (bootstrap sahifa oxirida yuklanadi).
Kodni o'qib topish qiyin, brauzerda esa bir zumda ko'rinadi.

ISHLATISH (loyiha ildizidan, brauzerli muhitda):

    DEBUG=1 python scripts/browser_check_pos.py

Talab: playwright + chromium. Yo'q bo'lsa skript buni aytib chiqadi.
Bu TEST EMAS — ataylab chaqiriladigan asbob. Test qilib qo'yilganda
LiveServerTestCase SQLite ulanishini jonli server oqimi bilan baham
ko'rgani uchun goh yiqilib turardi; goh yiqiladigan test esa
testsizlikdan yomon.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')
os.environ.setdefault('DEBUG', '1')

import django
django.setup()

CHROME = os.environ.get(
    'YURIT_CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit('playwright yo\'q:  pip install playwright')
if not os.path.exists(CHROME):
    sys.exit(f'chromium topilmadi: {CHROME}\n'
             f'YURIT_CHROMIUM=/yo\'l/chrome bilan ko\'rsating')

from django.test.runner import DiscoverRunner
runner = DiscoverRunner(verbosity=0, interactive=False)
cfg = runner.setup_databases()
from django.test.utils import setup_test_environment
setup_test_environment()

from decimal import Decimal
from django.conf import settings
from inventory.models import (Branch, User, Product, ProductVariant,
                              BranchStock, Shift)

branch = Branch.objects.create(name='Tekshiruv filiali')
seller = User.objects.create_user(username='bc_kassir', password='x',
                                  role=User.Role.SOTUVCHI, branch=branch)
Shift.objects.create(branch=branch, opened_by=seller, opening_cash=Decimal('0'))
prod = Product.objects.create(name='Tekshiruv tovari', code='TST-0001',
                              default_sale_price=Decimal('10000'))
var = ProductVariant.objects.create(product=prod, size='M', color='Qora',
                                    barcode='2000000000017')
BranchStock.objects.create(variant=var, branch=branch, stock_count=10,
                           cost_price=Decimal('5000'),
                           sale_price=Decimal('10000'),
                           wholesale_price=Decimal('8000'))

settings.ALLOWED_HOSTS = ['*']
from django.test.testcases import LiveServerThread
from django.contrib.staticfiles.handlers import StaticFilesHandler
th = LiveServerThread('127.0.0.1', StaticFilesHandler, connections_override={})
th.daemon = True
th.start()
th.is_ready.wait()
base = f'http://127.0.0.1:{th.port}'

errors, failed = [], []
problems = 0
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=CHROME)
    page = browser.new_context().new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('requestfailed',
            lambda r: failed.append(f'{r.url[-70:]} — {r.failure}'))
    page.goto(base + '/login/')
    page.fill('input[name=username]', 'bc_kassir')
    page.fill('input[name=password]', 'x')
    page.click('button[type=submit]')
    page.wait_for_load_state('networkidle')
    page.goto(base + '/pos/')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1000)

    print('=' * 62)
    print('JS XATOLARI')
    print('=' * 62)
    if errors:
        problems += len(errors)
        for e in errors:
            print('  XATO:', e[:180])
        print('\n  DIQQAT: xatodan KEYINGI barcha tugmalar ishlamaydi.')
    else:
        print('  yo\'q')

    print()
    print('=' * 62)
    print('TUGMALAR HAQIQATAN BOG\'LANGANMI')
    print('=' * 62)

    def check(name, fn):
        global problems
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f'{type(exc).__name__}: {exc}'[:100]
        print(f'  {"OK  " if ok else "XATO"}  {name:38} {detail}')
        if not ok:
            problems += 1

    def keypad():
        # Modalni ochmaymiz — bu yerda TUGMA BOG'LANGANMI degan savol
        # tekshiriladi. JS click() bevosita tinglovchini chaqiradi:
        # xato yuqorida sodir bo'lgan bo'lsa, tinglovchi umuman
        # bog'lanmagan bo'ladi va ekrandagi son o'zgarmaydi.
        for k in ('2', '*', '3', '000', 'eq'):
            page.eval_on_selector(f'#keypadModal [data-kp="{k}"]',
                                  'e => e.click()')
        txt = page.inner_text('#kpDisplay')
        return ('6' in txt), f'2 × 3000 = {txt}'

    def scan():
        page.fill('#scanInput', '2000000000017')
        page.press('#scanInput', 'Enter')
        page.wait_for_timeout(1200)
        n = page.eval_on_selector_all('#cartBody tr:not([id])', 'e => e.length')
        return n == 1, f'savatda {n} qator'

    def wholesale():
        page.click('label[for="priceModeWholesale"]', force=True)
        page.wait_for_timeout(400)
        # DIQQAT: katakni TARTIB RAQAMI bilan tanlamaymiz.
        # Ilgari shu yerda `querySelectorAll("td")[2]` turardi va ROW-1
        # jadvalga "#" ustunini qo'shgach indeks siljib, tekshiruv MIQDOR
        # katagini o'qiy boshlagan edi ('−' qaytarardi) — testning o'zi
        # sinib, mahsulotda xato bordek ko'rinardi.
        # Endi katakning O'Z nomi bor: data-cell="price".
        cells = page.eval_on_selector_all(
            '#cartBody tr:not([id])',
            '''els => els.map(t => {
                 const c = t.querySelector('[data-cell="price"]');
                 return c ? c.textContent.trim() : '(narx katagi yo`q)';
               })''')
        got = (cells or [''])[0].replace('\u00a0', ' ')
        return ('8' in got), f'ulgurji narx: {got[:24]}'

    check('"Boshqa summa" kalkulyatori', keypad)
    check('skaner savatga qo\'shadi', scan)
    check('ulgurji rejim narxni almashtiradi', wholesale)

    if failed:
        print()
        print('  Yuklanmagan so\'rovlar:')
        for f in failed[:6]:
            print('   ', f)
    browser.close()

th.terminate()
runner.teardown_databases(cfg)
print()
print('NATIJA:', 'HAMMASI JOYIDA' if problems == 0 else f'{problems} ta muammo')
sys.exit(1 if problems else 0)
