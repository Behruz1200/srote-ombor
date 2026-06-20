"""
Seed realistic sale volume for benchmarking.

Generates SaleTransaction + Sale rows across all branches, distributed
over the last 90 days. Bypasses stock_count updates (this is for query
benchmarking only, not inventory simulation).

Target: ~50,000 SaleTransaction + ~75,000 Sale rows
This matches roughly 1 year of moderate POS activity at ~140 txn/day
across 3 branches, which scales to 20 branches × ~9 txn/day each.

Usage:
  ./venv/bin/python manage.py shell < scripts/seed_load_test_data.py
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone

from inventory.models import (
    Branch, BranchStock, Customer, Sale, SaleTransaction, Shift, User,
)

TARGET_TXNS = 50_000
DAYS_BACK = 90
BATCH_SIZE = 2_000
PAYMENT_METHODS = ['cash', 'card', 'transfer', 'mixed']
PAYMENT_WEIGHTS = [0.55, 0.25, 0.15, 0.05]

print(f"=== seeding ~{TARGET_TXNS} transactions across {DAYS_BACK} days ===")
print(f"Existing: {SaleTransaction.objects.count()} txns, {Sale.objects.count()} lines")

branches = list(Branch.objects.filter(is_active=True))
if not branches:
    print("no active branches; abort")
    raise SystemExit(1)

users_by_branch = {
    b.id: list(User.objects.filter(branch=b)) or list(User.objects.filter(is_superuser=True))
    for b in branches
}

stocks_by_branch = {
    b.id: list(BranchStock.objects.filter(branch=b).select_related('variant'))
    for b in branches
}
for b in branches:
    if not stocks_by_branch[b.id]:
        print(f"branch {b.name} has no stock; skipping")
        del stocks_by_branch[b.id]

branches = [b for b in branches if b.id in stocks_by_branch]
print(f"Active branches: {len(branches)} | users: {sum(len(v) for v in users_by_branch.values())}")

shifts_by_branch = {}
for b in branches:
    shifts_by_branch[b.id] = list(Shift.objects.filter(branch=b).order_by('-opened_at')[:30])

customer_names = [
    'Aziz', 'Bekzod', 'Dilshod', 'Eldor', 'Farhod', 'Gulnoza', 'Hilola',
    'Iroda', 'Jasur', 'Kamol', 'Lola', 'Madina', 'Nilufar', 'Otabek',
    'Po\'lat', 'Qodir', 'Rustam', 'Sardor', 'Temur', 'Umida', 'Vali',
    'Xurshid', 'Yulduz', 'Zafar',
]

existing_customers = list(Customer.objects.all()[:50])

now = timezone.now()
start = now - timedelta(days=DAYS_BACK)

rng = random.Random(42)

txns_to_create = []
for i in range(TARGET_TXNS):
    branch = rng.choice(branches)
    user = rng.choice(users_by_branch[branch.id])
    shift = rng.choice(shifts_by_branch[branch.id]) if shifts_by_branch[branch.id] else None
    sold_at = start + timedelta(seconds=rng.randint(0, DAYS_BACK * 24 * 3600))

    method = rng.choices(PAYMENT_METHODS, weights=PAYMENT_WEIGHTS)[0]
    breakdown = []
    if method == 'mixed':
        breakdown = [
            {'method': 'cash', 'amount': float(rng.randint(50_000, 300_000))},
            {'method': 'card', 'amount': float(rng.randint(50_000, 300_000))},
        ]

    customer = None
    customer_name = ''
    customer_phone = ''
    if rng.random() < 0.4:
        customer = rng.choice(existing_customers) if existing_customers else None
        if customer:
            customer_name = customer.name or ''
            customer_phone = customer.phone or ''
    else:
        if rng.random() < 0.5:
            customer_name = rng.choice(customer_names)
            customer_phone = '+99890' + str(rng.randint(1000000, 9999999))

    txns_to_create.append(
        SaleTransaction(
            branch=branch,
            sold_by=user,
            payment_method=method,
            payment_breakdown=breakdown,
            customer=customer,
            customer_name=customer_name,
            customer_phone=customer_phone,
            note='',
            order_discount=Decimal('0'),
            shift=shift,
            sold_at=sold_at,
        )
    )

print(f"Bulk creating {len(txns_to_create)} transactions ...")
with transaction.atomic():
    SaleTransaction.objects.bulk_create(txns_to_create, batch_size=BATCH_SIZE)
print(f"  total now: {SaleTransaction.objects.count()}")

print(f"Bulk creating sale lines (avg 1.5 per txn) ...")
new_txns = list(
    SaleTransaction.objects.order_by('-id')
    .values('id', 'branch_id', 'sold_by_id', 'sold_at')[:TARGET_TXNS]
)

lines_to_create = []
for t in new_txns:
    n_lines = rng.choices([1, 2, 3, 4], weights=[0.55, 0.30, 0.10, 0.05])[0]
    pool = stocks_by_branch[t['branch_id']]
    chosen = rng.sample(pool, min(n_lines, len(pool)))
    for stock in chosen:
        qty = rng.choices([1, 2, 3], weights=[0.75, 0.20, 0.05])[0]
        price = stock.sale_price
        line_discount = Decimal('0')
        if rng.random() < 0.08:
            line_discount = Decimal(str(round(float(price) * rng.uniform(0.02, 0.15), 0)))
        lines_to_create.append(
            Sale(
                transaction_id=t['id'],
                variant_id=stock.variant_id,
                branch_id=t['branch_id'],
                quantity=qty,
                sale_price=price,
                cost_at_sale=stock.cost_price,
                line_discount=line_discount,
                sold_by_id=t['sold_by_id'],
                sold_at=t['sold_at'],
            )
        )

print(f"  prepared {len(lines_to_create)} sale lines")
with transaction.atomic():
    Sale.objects.bulk_create(lines_to_create, batch_size=BATCH_SIZE)
print(f"  total now: {Sale.objects.count()}")

print(f"\n=== final counts ===")
print(f"SaleTransaction: {SaleTransaction.objects.count():,}")
print(f"Sale lines:      {Sale.objects.count():,}")
print(f"Customers:       {Customer.objects.count():,}")
print(f"Time range:      {SaleTransaction.objects.earliest('sold_at').sold_at.date()} ... {SaleTransaction.objects.latest('sold_at').sold_at.date()}")
