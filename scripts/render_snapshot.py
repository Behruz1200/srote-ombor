"""Shablonlarni chizib, normallashtirilgan HTML suratini oladi.

Maqsad: shablonlarni markazlashtirganda (umumiy bloklarga ajratganda)
KO'RINISH O'ZGARMAGANINI isbotlash. Avval `--out before/` bilan
ishlatiladi, o'zgarishdan keyin `--out after/`, so'ng `diff -r`.

Bo'sh joy normallashtiriladi (include qatorlar ko'chishi tabiiy),
lekin teglar, klasslar va matn AYNAN bir xil bo'lishi shart.

    DEBUG=1 python scripts/render_snapshot.py --out /tmp/before
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')
os.environ['DEBUG'] = '1'

import django                                    # noqa: E402
django.setup()

from django.test.utils import setup_test_environment, teardown_test_environment  # noqa: E402
from django.test.runner import DiscoverRunner    # noqa: E402


def normalize(html):
    """Brauzer ko'radigan shaklga keltiradi.

    HTML'da teglar orasidagi bo'sh joy ahamiyatsiz — brauzer uni bitta
    probelga aylantiradi. Shu bois manbadagi chekinish va qator uzilishi
    FARQ SANALMAYDI, lekin teg, klass yoki matn qo'shilsa/yo'qolsa
    DARHOL ko'rinadi.
    """
    # 1) Har chizishda boshqacha bo'ladigan qiymatlarni maskalaymiz
    html = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"',
                  'name="csrfmiddlewaretoken" value="X"', html)
    html = re.sub(r'[A-Za-z0-9]{64}', 'CSRF', html)
    html = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                  r'[0-9a-f]{4}-[0-9a-f]{12}', 'UUID', html)
    html = re.sub(r'data:image/png;base64,[A-Za-z0-9+/=]+', 'PNG', html)
    html = re.sub(r'\b[A-Z2-7]{32}\b', 'SECRET', html)
    html = re.sub(r'\b\d{1,2}:\d{2}(:\d{2})?\b', 'HH:MM', html)
    html = re.sub(r'\b\d{4}-\d{2}-\d{2}T[\dTZ:.+-]*', 'ISO', html)
    # 2) Bo'sh joyni brauzer kabi yig'ishtiramiz
    html = ' '.join(html.split())
    # 3) Har bir tegni o'z qatoriga
    html = re.sub(r'\s*<', '\n<', html)
    return '\n'.join(l.strip() for l in html.split('\n') if l.strip())


def build_world():
    """Har bir shablon uchun yetarli ma'lumot."""
    from decimal import Decimal
    from django.utils import timezone
    from inventory.models import (
        Branch, BranchStock, Category, Customer, Product, ProductVariant,
        Sale, SaleTransaction, Shift, Supplier, User, Intake, Stocktake,
        Transfer, CashPayout, AuditLog, Promotion,
    )
    br = Branch.objects.create(name='Markaz', monthly_rent=Decimal('1000000'))
    admin = User.objects.create_user(username='snapadmin', password='x',
                                     role=User.Role.ADMIN, branch=br)
    User.objects.create_user(username='snapseller', password='x',
                             role=User.Role.SOTUVCHI, branch=br)
    Supplier.objects.create(name='Yetkazuvchi 1')
    cat = Category.objects.create(name='Koylak')
    Category.objects.create(name='Poyabzal')
    cust = Customer.objects.create(name='Mijoz', phone='+998901234567')
    shift = Shift.objects.create(branch=br, opened_by=admin,
                                 status=Shift.Status.OPEN,
                                 opening_cash=Decimal('100000'))
    prods = []
    for i in range(3):
        p = Product.objects.create(code=f'SNP-{i:04d}', name=f'Tovar {i}',
                                   category=cat,
                                   default_sale_price=Decimal('50000'))
        prods.append(p)
        for size in ('M', 'L'):
            v = ProductVariant.objects.create(product=p, size=size,
                                              color='Qora')
            BranchStock.objects.create(
                variant=v, branch=br, stock_count=10 - i,
                cost_price=Decimal('30000'), sale_price=Decimal('50000'),
                wholesale_price=Decimal('35000'))
            Intake.objects.create(variant=v, branch=br, quantity=10,
                                  cost_per_unit=Decimal('30000'),
                                  received_by=admin)
    txn = SaleTransaction.objects.create(
        branch=br, sold_by=admin, shift=shift, customer=cust,
        payment_method=SaleTransaction.PaymentMethod.CASH)
    for v in ProductVariant.objects.all()[:3]:
        Sale.objects.create(transaction=txn, variant=v, branch=br,
                            quantity=1, sale_price=Decimal('50000'),
                            cost_at_sale=Decimal('30000'), sold_by=admin)
    CashPayout.objects.create(branch=br, shift=shift, amount=Decimal('50000'),
                              category='other', created_by=admin)
    Stocktake.objects.create(branch=br, started_by=admin)
    Transfer.objects.create(from_branch=br, to_branch=br, created_by=admin)
    Promotion.objects.create(name='Aksiya', percent=Decimal('10'),
                             valid_from=timezone.now())
    return {'admin': admin, 'branch': br, 'product': prods[0],
            'variant': ProductVariant.objects.first(),
            'txn': txn, 'shift': shift, 'customer': cust,
            'stocktake': Stocktake.objects.first(),
            'transfer': Transfer.objects.first()}


NO_ARG = [
    'home', 'dashboard', 'lookup', 'product_list', 'product_create',
    'product_merge', 'employee_debt_list', 'intake_new', 'intake_variants',
    'clothes_intake', 'intake_mixed', 'intake_photo', 'intake_import',
    'price_list', 'price_history', 'quick_sell_settings', 'promotion_list',
    'supplier_list', 'warehouse', 'reorder_page', 'csv_import', 'sales_list',
    'cart_view', 'checkout', 'category_list', 'branch_list', 'branch_create',
    'user_list', 'user_create', 'customer_list', 'reports', 'insights',
    'audit_list', 'stocktake_list', 'stocktake_create', 'transfer_list',
    'shift_open', 'shift_close', 'reorder_page',
    'transfer_create', 'writeoff_list', 'shift_list', 'pos_terminal',
    'payment_qr_list', 'product_requests', 'security_2fa',     'web_orders', 'variant_labels',
    'price_labels',
]

WITH_ARG = [
    ('product_detail', lambda w: [w['product'].code]),
    ('product_edit', lambda w: [w['product'].code]),
    ('product_variants_edit', lambda w: [w['product'].code]),
    ('transaction_detail', lambda w: [w['txn'].public_id]),
    ('shift_receipt', lambda w: [w['shift'].pk]),
    ('shift_detail', lambda w: [w['shift'].pk]),
    ('customer_detail', lambda w: [w['customer'].pk]),
    ('stocktake_detail', lambda w: [w['stocktake'].pk]),
    ('transfer_detail', lambda w: [w['transfer'].pk]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old = runner.setup_databases()
    try:
        from django.test import Client
        from django.urls import NoReverseMatch, reverse
        world = build_world()
        c = Client(SERVER_NAME='testserver')
        c.force_login(world['admin'])

        ok = skipped = 0
        for name in NO_ARG:
            try:
                url = reverse(name)
            except NoReverseMatch:
                print('  ?', name, '— URL yo\'q'); skipped += 1; continue
            r = c.get(url, follow=True)
            if r.status_code != 200:
                print(f'  ! {name}: {r.status_code}'); skipped += 1; continue
            open(os.path.join(args.out, f'{name}.html'), 'w',
                 encoding='utf-8').write(normalize(r.content.decode()))
            ok += 1
        for name, argf in WITH_ARG:
            try:
                url = reverse(name, args=argf(world))
            except NoReverseMatch:
                print('  ?', name, '— URL yo\'q'); skipped += 1; continue
            r = c.get(url, follow=True)
            if r.status_code != 200:
                print(f'  ! {name}: {r.status_code}'); skipped += 1; continue
            open(os.path.join(args.out, f'{name}.html'), 'w',
                 encoding='utf-8').write(normalize(r.content.decode()))
            ok += 1
        print(f'\nSurat olindi: {ok} ta sahifa, {skipped} ta o\'tkazildi -> {args.out}')
    finally:
        runner.teardown_databases(old)
        teardown_test_environment()


if __name__ == '__main__':
    main()
