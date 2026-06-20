"""
Scale the seeded DB to 20 branches x ~30 sellers + open shifts + stock everywhere.

Run AFTER seed.py to take a 3-branch baseline up to a realistic 20-branch
production-like layout, then re-run seed_load_test_data.py for the txn
volume.
"""
import random
from decimal import Decimal

from django.db import transaction
from inventory.models import (
    Branch, User, Product, ProductVariant, BranchStock, Shift,
)

random.seed(7)
TARGET_BRANCHES = 20
SELLERS_PER_BRANCH = 2

CITY_NAMES = [
    'Mirobod', 'Mirzo Ulug\'bek', 'Olmazor', 'Shayxontohur', 'Yashnobod',
    'Bektemir', 'Yangihayot', 'Uchtepa', 'Yakkasaroy', 'Andijon',
    'Buxoro', 'Farg\'ona', 'Namangan', 'Qarshi', 'Samarqand',
    'Termiz', 'Xorazm', 'Nukus', 'Jizzax',
]

with transaction.atomic():
    # Create branches up to 20
    existing = Branch.objects.count()
    new_branches = []
    for i in range(TARGET_BRANCHES - existing):
        name = f"{CITY_NAMES[i % len(CITY_NAMES)]} Filiali"
        b = Branch.objects.create(
            name=name,
            address=f"{name} ko'chasi {i + 1}",
            phone=f"+99890{200000 + i:07d}",
            is_active=True,
        )
        new_branches.append(b)
    print(f"branches: {existing} -> {Branch.objects.count()}")

    # Create sellers per branch (2 each)
    all_branches = list(Branch.objects.all())
    new_users = 0
    for b in all_branches:
        for i in range(SELLERS_PER_BRANCH):
            uname = f"sotuvchi_{b.id}_{i}"
            if not User.objects.filter(username=uname).exists():
                u = User.objects.create_user(
                    username=uname, password='sotuvchi123', role='sotuvchi', branch=b,
                )
                new_users += 1
    print(f"created {new_users} new sellers")

    # Create BranchStock for every variant × every branch we don't have one for
    variants = list(ProductVariant.objects.select_related('product').all())
    existing_stocks = set(
        BranchStock.objects.values_list('variant_id', 'branch_id')
    )
    # First pass — collect what we need to create
    needed = []
    for v in variants:
        cost = Decimal('200000') + Decimal(random.randint(0, 800)) * Decimal('1000')
        sale = cost * Decimal('1.4')
        for b in all_branches:
            if (v.id, b.id) in existing_stocks:
                continue
            needed.append(
                BranchStock(
                    variant=v, branch=b,
                    cost_price=cost, sale_price=sale, wholesale_price=sale * Decimal('0.85'),
                    stock_count=random.randint(50, 500),
                )
            )
    BranchStock.objects.bulk_create(needed, batch_size=2000)
    print(f"created {len(needed)} BranchStock rows; total: {BranchStock.objects.count()}")

    # Open shifts (1 per branch, if no open shift)
    opened_shifts = 0
    for b in all_branches:
        if Shift.objects.filter(branch=b, status='open').exists():
            continue
        admin = User.objects.filter(branch=b).first() or User.objects.first()
        Shift.objects.create(
            branch=b, opened_by=admin, opening_cash=Decimal('100000'),
            status='open',
        )
        opened_shifts += 1
    print(f"opened {opened_shifts} shifts")

print(f"\n=== summary ===")
print(f"branches: {Branch.objects.count()}")
print(f"sellers:  {User.objects.filter(role='sotuvchi').count()}")
print(f"stocks:   {BranchStock.objects.count()}")
print(f"shifts:   {Shift.objects.filter(status='open').count()}")
