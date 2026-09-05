"""ROLE-1..6 — uchta rolni HAQIQIY brauzerda, odam kabi tekshiradi.

Testlar HTML matnini tekshiradi; bu skript esa ekranga NIMA CHIQQANINI
ko'radi: menyuda qaysi bandlar bor, filial yorlig'i turibdimi, boshqa
filial nomi biror joyda ko'rinib qolmadimi.

    DEBUG=1 python scripts/browser_check_roles.py
"""
import os
import sys
import glob
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT); sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')
os.environ['DEBUG'] = '1'; os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
import django; django.setup()
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.test.testcases import LiveServerThread
from django.test.utils import setup_test_environment, teardown_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment(); runner = DiscoverRunner(verbosity=0, interactive=False)
old = runner.setup_databases(); th = None; BAD = []


def chk(ok, label, detail=''):
    print(f'  {"OK  " if ok else "XATO"} {label:52s} {detail}')
    if not ok:
        BAD.append(label)


try:
    from decimal import Decimal
    from inventory.models import (Branch, User, Product, ProductVariant,
                                  BranchStock, Shift, Category,
                                  SaleTransaction, Sale)
    SECRET = 'XONQA-MAXFIY'
    a = Branch.objects.create(name='Koreys Bozor')
    b = Branch.objects.create(name=SECRET)
    User.objects.create_user(username='egasi', password='x',
                             role=User.Role.SUPERUSER)
    User.objects.create_user(username='filialadmin', password='x',
                             role=User.Role.ADMIN, branch=a)
    sb = User.objects.create_user(username='sotuvchib', password='x',
                                  role=User.Role.SOTUVCHI, branch=b)
    cat = Category.objects.create(name='Sumka')
    p = Product.objects.create(code='SIR-0001', name='Maxfiy sumka',
                               category=cat)
    v = ProductVariant.objects.create(product=p, size='M', color='Qora',
                                      barcode='7900000000001')
    BranchStock.objects.create(variant=v, branch=b, stock_count=4,
                               cost_price=Decimal('1000'),
                               sale_price=Decimal('9000'))
    sh = Shift.objects.create(branch=b, opened_by=sb, status=Shift.Status.OPEN,
                              opening_cash=Decimal('0'))
    tx = SaleTransaction.objects.create(branch=b, sold_by=sb, shift=sh,
                                        payment_method='cash')
    Sale.objects.create(transaction=tx, variant=v, branch=b, sold_by=sb,
                        quantity=2, sale_price=Decimal('9000'),
                        cost_at_sale=Decimal('1000'))

    th = LiveServerThread('127.0.0.1', StaticFilesHandler); th.daemon = True
    th.start(); th.is_ready.wait(); base = f'http://127.0.0.1:{th.port}'
    from playwright.sync_api import sync_playwright
    exe = next((x for x in glob.glob(
        '/opt/pw-browsers/chromium-*/chrome-linux/chrome')
        if os.path.exists(x)), None)

    with sync_playwright() as pw:
        br_ = pw.chromium.launch(executable_path=exe)

        def login(user):
            ctx = br_.new_context(viewport={'width': 1440, 'height': 900})
            page = ctx.new_page()
            page.goto(base + '/login/')
            page.fill('input[name=username]', user)
            page.fill('input[name=password]', 'x')
            page.click('button[type=submit]')
            page.wait_for_load_state('networkidle')
            return ctx, page

        # ---------------------------------------------------------- EGASI
        print('\n--- 1. EGASI (SuperUser) — hamma narsa ochiq ---')
        ctx, page = login('egasi')
        page.goto(base + '/dashboard/', wait_until='networkidle')
        nav = page.inner_text('nav')
        chk('Narxlar' in nav, 'menyuda "Narxlar" bor')
        chk(page.locator("text=Filiallar").count() > 0 or 'Boshqarish' in nav,
            'menyuda "Boshqarish" bor')
        page.goto(base + '/branches/', wait_until='networkidle')
        chk(SECRET in page.content(), 'egasi ikkala filialni ko\'radi')
        page.goto(base + '/audit/', wait_until='networkidle')
        chk(page.url.endswith('/audit/'), 'audit jurnali ochildi')
        # rol yorlig'i foydalanuvchi menyusi ICHIDA — ochib ko'ramiz
        page.click('.yurit-user-link')
        page.wait_for_timeout(250)
        chk('Egasi' in page.inner_text('.dropdown-menu.show'),
            'rol yorlig\'i "Egasi"')
        ctx.close()

        # --------------------------------------------------- FILIAL ADMIN
        print('\n--- 2. FILIAL ADMINI — faqat o\'z filiali ---')
        ctx, page = login('filialadmin')
        page.goto(base + '/dashboard/', wait_until='networkidle')
        body = page.content()
        chk(SECRET not in body, 'panelda boshqa filial ko\'rinmaydi')
        chk('Koreys Bozor' in body, 'o\'z filiali yorlig\'i ko\'rinadi')
        nav = page.inner_text('nav')
        chk('Aksiyalar' not in nav, 'menyuda "Aksiyalar" YO\'Q')

        # ROLE-7: butun tizimga tegadigan (filial o'lchovi yo'q) sahifalar
        for path, label in (('/branches/', 'Filiallar'),
                            ('/categories/', 'Kategoriyalar'),
                            ('/prices/promotions/', 'Aksiyalar'),
                            ('/products/new/', 'Yangi mahsulot')):
            page.goto(base + path, wait_until='networkidle')
            txt = page.inner_text('body')
            chk("Ruxsat yo'q" in txt, f'{label}: yopiq (butun tizim)')

        # ROLE-7: filial o'lchovi BOR — ochiq, lekin o'z filiali bilan
        chk('Narxlar' in nav, 'menyuda "Narxlar" BOR (filial narxi)')
        for path, label in (('/prices/', 'Narx jadvali'),
                            ('/audit/', 'Audit jurnali')):
            page.goto(base + path, wait_until='networkidle')
            body2 = page.content()
            chk("Ruxsat yo'q" not in page.inner_text('body'),
                f'{label}: ochiq')
            chk(SECRET not in body2, f'{label}: boshqa filial yo\'q')

        page.goto(base + '/sales/?branch=%d' % b.pk, wait_until='networkidle')
        chk(SECRET not in page.content(),
            '?branch= bilan ham boshqa filial ochilmaydi')
        page.goto(base + '/users/', wait_until='networkidle')
        c = page.content()
        chk('sotuvchib' not in c, 'boshqa filial xodimi ro\'yxatda yo\'q')
        chk('egasi' not in c, 'egasi ro\'yxatda ko\'rinmaydi')
        page.goto(base + '/users/new/', wait_until='networkidle')
        roles = page.locator('select[name=role] option').all_inner_texts()
        chk(roles == ['Sotuvchi'], 'faqat "Sotuvchi" yarata oladi', roles)
        brs = page.locator('select[name=branch] option').all_inner_texts()
        chk(brs == ['Koreys Bozor'], 'faqat o\'z filialiga', brs)
        ctx.close()

        # ------------------------------------------------------- SOTUVCHI
        print('\n--- 3. SOTUVCHI — faqat kassa ---')
        ctx, page = login('sotuvchib')
        page.goto(base + '/dashboard/', wait_until='networkidle')
        chk('Ruxsat' in page.inner_text('body') or '/pos/' in page.url
            or '/lookup/' in page.url, 'boshqaruv paneli yopiq')
        page.goto(base + '/pos/', wait_until='networkidle')
        chk(page.locator('#scanInput').count() == 1, 'kassa ochiladi')
        chk(page.locator('#branchSwitcher').count() == 0,
            'kassa filialini almashtira olmaydi')
        ctx.close()
        br_.close()

    print('\n' + '=' * 68)
    print(f'  MUAMMO: {len(BAD)}')
    for x in BAD:
        print('   -', x)
    print('=' * 68)
finally:
    if th:
        th.terminate()
    runner.teardown_databases(old); teardown_test_environment()
sys.exit(1 if BAD else 0)
