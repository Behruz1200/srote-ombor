"""Make Product.external_barcode unique, nullable.

Three-step migration:

1. Alter column to allow NULL (was NOT NULL with blank=True default '').
2. Data pass:
   - Convert all '' rows to NULL so the unique constraint allows
     multiple "no barcode" products — Postgres/SQLite treat NULLs
     as distinct.
   - Resolve duplicate non-empty values by keeping the lowest-id
     product and NULLing the rest. Operators see a stdout warning
     and can re-attach the EAN to the right product via the
     attach-to-existing flow.
3. Alter column to add unique=True.
"""
from collections import defaultdict
from django.db import migrations, models


def normalize_external_barcodes(apps, schema_editor):
    Product = apps.get_model('inventory', 'Product')

    Product.objects.filter(external_barcode='').update(external_barcode=None)

    by_code = defaultdict(list)
    for p in (Product.objects
              .exclude(external_barcode__isnull=True)
              .only('id', 'external_barcode', 'name', 'code')
              .order_by('id')):
        by_code[p.external_barcode].append(p)

    for ean, products in by_code.items():
        if len(products) <= 1:
            continue
        keeper = products[0]
        losers = products[1:]
        print(f"  ! external_barcode {ean!r} dup: keep "
              f"'{keeper.name}' ({keeper.code}); "
              f"NULLing {len(losers)} other(s): "
              + ', '.join(f"'{p.name}' ({p.code})" for p in losers))
        for p in losers:
            p.external_barcode = None
            p.save(update_fields=['external_barcode'])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0022_product_external_barcode'),
    ]

    operations = [
        # Step 1 — allow NULL so the data pass can clear ''.
        migrations.AlterField(
            model_name='product',
            name='external_barcode',
            field=models.CharField(
                blank=True, null=True, db_index=True, max_length=64,
                help_text='Ishlab chiqaruvchi tomonidan chop etilgan barcode '
                          '(EAN-13, UPC va h.k.). Skanerlash uchun ishlatiladi.',
            ),
        ),
        # Step 2 — '' → NULL; resolve duplicates.
        migrations.RunPython(normalize_external_barcodes, reverse_noop),
        # Step 3 — enforce uniqueness.
        migrations.AlterField(
            model_name='product',
            name='external_barcode',
            field=models.CharField(
                blank=True, null=True, unique=True, max_length=64,
                help_text='Ishlab chiqaruvchi tomonidan chop etilgan barcode '
                          '(EAN-13, UPC va h.k.). Skanerlash uchun ishlatiladi.',
            ),
        ),
    ]
