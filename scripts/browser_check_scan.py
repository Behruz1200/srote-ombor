"""SCAN-3 — POS qidiruv katagini KASSIR kabi sinab ko'radi.

Haqiqiy brauzerda, haqiqiy tugma bosish bilan:
  * sekin yozish (odam tezligida) matnni buzmasligi;
  * yozish paytida savatga O'ZI qo'shilmasligi;
  * Enter va SKANER esa qo'shib, katakni tozalashi;
  * topilmagan skanerdan keyin ham katak tozalanishi (SCAN-2).

    DEBUG=1 python scripts/browser_check_scan.py
"""
import os
import sys
import glob
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT); sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','store_management.settings')
os.environ['DEBUG']='1'; os.environ['DJANGO_ALLOW_ASYNC_UNSAFE']='true'
import django; django.setup()
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.test.testcases import LiveServerThread
from django.test.utils import setup_test_environment, teardown_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); runner=DiscoverRunner(verbosity=0,interactive=False)
old=runner.setup_databases(); th=None; BAD=[]
def chk(ok,label,detail=''):
    print(f'  {"OK  " if ok else "XATO"} {label:50s} {detail}')
    if not ok: BAD.append(label)
try:
    from decimal import Decimal
    from inventory.models import (Branch, User, Product, ProductVariant,
                                  BranchStock, Shift, Category)
    br = Branch.objects.create(name='SCAN3')
    u = User.objects.create_user(username='scan3', password='x',
                                 role=User.Role.SOTUVCHI, branch=br)
    Shift.objects.create(branch=br, opened_by=u, status=Shift.Status.OPEN,
                         opening_cash=Decimal('0'))
    cat = Category.objects.create(name='Sumka')
    # "hermes" prefiksi IKKI tovarga mos keladi -> takliflar chiqadi
    for code, name in (('HER-0001', 'Hermes 53 ss'), ('HER-0002', 'Hermes 40 aa')):
        p = Product.objects.create(code=code, name=name, category=cat)
        v = ProductVariant.objects.create(product=p, size='M', color='Qora',
                                          barcode='200000000001' + code[-1])
        BranchStock.objects.create(variant=v, branch=br, stock_count=5,
                                   cost_price=Decimal('1000'),
                                   sale_price=Decimal('2000'))
    th=LiveServerThread('127.0.0.1',StaticFilesHandler); th.daemon=True
    th.start(); th.is_ready.wait(); base=f'http://127.0.0.1:{th.port}'
    from playwright.sync_api import sync_playwright
    exe=next((p for p in glob.glob('/opt/pw-browsers/chromium-*/chrome-linux/chrome') if os.path.exists(p)),None)
    with sync_playwright() as pw:
        br_=pw.chromium.launch(executable_path=exe)
        ctx=br_.new_context(viewport={'width':1440,'height':900}); page=ctx.new_page()
        errs=[]; page.on('pageerror', lambda e: errs.append(str(e)))
        page.goto(base+'/login/'); page.fill('input[name=username]','scan3')
        page.fill('input[name=password]','x'); page.click('button[type=submit]')
        page.wait_for_load_state('networkidle')
        page.goto(base+'/pos/', wait_until='networkidle')

        print('\n--- 1. Sekin yozish: "hermes 53 ss" ---')
        page.click('#scanInput')
        # odam kabi: har harf orasida 120 ms, "hermes" dan keyin 600 ms tanaffus
        for ch in 'hermes':
            page.keyboard.type(ch); page.wait_for_timeout(120)
        page.wait_for_timeout(700)          # jonli qidiruv shu yerda ishlaydi
        mid = page.input_value('#scanInput')
        chk(mid == 'hermes', 'tanaffusdan keyin matn joyida', repr(mid))
        for ch in ' 53 ss':
            page.keyboard.type(ch); page.wait_for_timeout(120)
        page.wait_for_timeout(700)
        val = page.input_value('#scanInput')
        chk(val == 'hermes 53 ss', 'to\'liq nom yozildi', repr(val))
        n = page.eval_on_selector_all('#cartBody tr:not([id])','e=>e.length')
        chk(n == 0, 'jonli qidiruv savatga O\'ZI qo\'shmadi', f'{n} qator')

        print('\n--- 1b. QO\'LDA yozilsa — aniq mos kelsa ham faqat ko\'rsatadi ---')
        page.fill('#scanInput', ''); page.click('#scanInput')
        page.keyboard.type('HER-0001', delay=130); page.wait_for_timeout(900)
        n = page.eval_on_selector_all('#cartBody tr:not([id])','e=>e.length')
        chk(n == 0, 'yozish paytida savatga qo\'shilmadi', f'{n} qator')
        chk(page.input_value('#scanInput') == 'HER-0001', 'katak tegilmadi',
            repr(page.input_value('#scanInput')))
        st = page.inner_text('#searchStatus')
        chk('Enter' in st, 'xabar Enter bosishni aytadi', repr(st[:60]))

        print('\n--- 2. Enter bosilsa: qo\'shadi va tozalaydi ---')
        page.fill('#scanInput', ''); page.click('#scanInput')
        page.keyboard.type('HER-0001'); page.keyboard.press('Enter')
        page.wait_for_timeout(1200)
        chk(page.input_value('#scanInput') == '', 'Enter — katak tozalandi',
            repr(page.input_value('#scanInput')))
        n = page.eval_on_selector_all('#cartBody tr:not([id])','e=>e.length')
        chk(n == 1, 'savatga qo\'shildi', f'{n} qator')

        print('\n--- 3. SKANER (tez + Enter): topilmasa ham tozalanadi ---')
        page.click('#scanInput')
        page.keyboard.type('9999999999999', delay=8)
        page.keyboard.press('Enter'); page.wait_for_timeout(1200)
        chk(page.input_value('#scanInput') == '', 'skaner — katak tozalandi',
            repr(page.input_value('#scanInput')))
        st = page.inner_text('#searchStatus')
        chk('9999999999999' in st, 'xabarda kod ko\'rinadi', repr(st[:60]))

        print('\n--- 3b. SKANER ENTER YUBORMASA HAM qo\'shadi (SCAN-4) ---')
        # Ba'zi skanerlar oxirida Enter yubormaydi: kod katakda qolib ketardi.
        page.fill('#scanInput', ''); page.click('#scanInput')
        before = page.eval_on_selector_all('#cartBody tr:not([id])','e=>e.length')
        # HER-0002 shtrix-kodi — savatda hali yo'q, demak YANGI qator bo'ladi
        page.keyboard.type('2000000000012', delay=6)     # skaner tezligi, Enter YO'Q
        page.wait_for_timeout(1500)
        n = page.eval_on_selector_all('#cartBody tr:not([id])','e=>e.length')
        chk(n == before + 1, 'Enter\'siz skaner savatga qo\'shdi', f'{before} -> {n}')
        chk(page.input_value('#scanInput') == '', 'katak o\'zi tozalandi',
            repr(page.input_value('#scanInput')))

        print('\n--- 3c. Enter\'siz skaner TOPILMASA ham katakni tozalaydi ---')
        page.click('#scanInput')
        page.keyboard.type('3020320203203203', delay=6)  # Enter YO'Q
        page.wait_for_timeout(1500)
        chk(page.input_value('#scanInput') == '',
            'topilmagan kod katakda qolmadi', repr(page.input_value('#scanInput')))

        print('\n--- 3d. TEZ terilgan NOM skaner deb qaralmaydi ---')
        # "hermes" da raqam yo'q — qanchalik tez terilmasin, jonli qidiruv.
        page.fill('#scanInput', ''); page.click('#scanInput')
        page.keyboard.type('hermes')                      # kechikishsiz = juda tez
        page.wait_for_timeout(900)
        chk(page.input_value('#scanInput') == 'hermes',
            'tez terilgan nom o\'chirilmadi', repr(page.input_value('#scanInput')))

        print('\n--- 3e. TOZALASH tugmasi ---')
        page.fill('#scanInput', ''); page.click('#scanInput')
        page.keyboard.type('99999999999999', delay=130)   # QO'LDA -> katakda qoladi
        page.wait_for_timeout(900)
        chk(page.input_value('#scanInput') == '99999999999999',
            'topilmagan kod katakda qoldi', repr(page.input_value('#scanInput')))
        chk(page.is_visible('#scanClearBtn'), 'tozalash tugmasi ko\'rindi')
        page.click('#scanClearBtn'); page.wait_for_timeout(400)
        chk(page.input_value('#scanInput') == '', 'tugma katakni tozaladi',
            repr(page.input_value('#scanInput')))
        chk(not page.is_visible('#scanClearBtn'),
            'bo\'sh katakda tugma yashirinadi')
        chk(page.inner_text('#searchStatus').strip() == '',
            'xabar ham tozalandi', repr(page.inner_text('#searchStatus')[:40]))
        chk(page.evaluate("document.activeElement && document.activeElement.id")
            == 'scanInput', 'fokus katakda qoldi')

        print('\n--- 3f. Esc ham tozalaydi ---')
        page.keyboard.type('12345678', delay=130); page.wait_for_timeout(600)
        page.keyboard.press('Escape'); page.wait_for_timeout(400)
        chk(page.input_value('#scanInput') == '', 'Esc katakni tozaladi',
            repr(page.input_value('#scanInput')))

        print('\n--- 4. Yozish paytida "topilmadi" katakni buzmaydi ---')
        page.fill('#scanInput', ''); page.click('#scanInput')
        for ch in 'zzz':
            page.keyboard.type(ch); page.wait_for_timeout(120)
        page.wait_for_timeout(700)
        page.keyboard.type('qqq'); page.wait_for_timeout(700)
        chk(page.input_value('#scanInput') == 'zzzqqq',
            'topilmagan jonli qidiruv matnni o\'chirmadi',
            repr(page.input_value('#scanInput')))

        print('\n--- 5. Takliflardan tanlash ishlaydi ---')
        page.fill('#scanInput', ''); page.click('#scanInput')
        page.keyboard.type('hermes'); page.wait_for_timeout(900)
        vis = page.is_visible('#suggestions')
        chk(vis, 'ikki mos tovar uchun takliflar chiqdi')
        if vis:
            page.click('#suggestionList button >> nth=0'); page.wait_for_timeout(1000)
            chk(page.input_value('#scanInput') == '', 'tanlangach katak tozalandi',
                repr(page.input_value('#scanInput')))
        chk(not errs, 'JS xatosi yo\'q', errs[:1])
        br_.close()
    print('\n'+'='*66); print(f'  MUAMMO: {len(BAD)}')
    for b in BAD: print('   -', b)
    print('='*66)
finally:
    if th: th.terminate()
    runner.teardown_databases(old); teardown_test_environment()
sys.exit(1 if BAD else 0)
