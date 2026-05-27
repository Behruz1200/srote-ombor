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

    updated = 0
    for sale in Sale.objects.filter(cost_at_sale=0).iterator():
        cost = stocks.get((sale.variant_id, sale.branch_id))
        if cost is not None:
            sale.cost_at_sale = cost
            sale.save(update_fields=['cost_at_sale'])
            updated += 1
    print(f'   backfilled cost_at_sale on {updated} historical sales')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0002_sale_cost_at_sale'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
