"""Backfill Sale.cost_at_sale for historical rows.

For each existing Sale with cost_at_sale = 0, copy the current
BranchStock.cost_price for that (variant, branch) pair. This is the
best approximation available — older sales may have actually had a
different cost at the time, but we have no history before this field
existed.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Sale = apps.get_model('inventory', 'Sale')
    BranchStock = apps.get_model('inventory', 'BranchStock')

    stocks = {
        (bs.variant_id, bs.branch_id): bs.cost_price
        for bs in BranchStock.objects.all()
    }

    # bulk_update in batches — original code did N individual UPDATEs which
    # locks the Sale table for minutes on a million-row production DB.
    BATCH = 1000
    pending = []
    updated = 0
    for sale in Sale.objects.filter(cost_at_sale=0).only('id', 'variant_id', 'branch_id', 'cost_at_sale').iterator():
        cost = stocks.get((sale.variant_id, sale.branch_id))
        if cost is None:
            continue
        sale.cost_at_sale = cost
        pending.append(sale)
        if len(pending) >= BATCH:
            Sale.objects.bulk_update(pending, ['cost_at_sale'])
            updated += len(pending)
            pending = []
    if pending:
        Sale.objects.bulk_update(pending, ['cost_at_sale'])
        updated += len(pending)
    print(f'   backfilled cost_at_sale on {updated} historical sales (bulk batches of {BATCH})')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0002_sale_cost_at_sale'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
