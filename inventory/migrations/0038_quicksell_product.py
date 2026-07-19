import django.db.models.deletion
from django.db import migrations, models

CATEGORY_NAME = 'Kiyim Kechak'


def build(apps, schema_editor):
    """Har tezkor sotuv toifasiga ombor yuritiladigan mahsulot yaratamiz.

    Har NARX alohida tur bo'ladi: 2500 so'mlik paypoq va 5000 so'mlik paypoq
    aslida boshqa-boshqa tovar. Shunda qabul (miqdor/tannarx/marja) va ombor
    oddiy mahsulotlar kabi ishlaydi.
    """
    Category = apps.get_model('inventory', 'Category')
    Product = apps.get_model('inventory', 'Product')
    ProductVariant = apps.get_model('inventory', 'ProductVariant')
    BranchStock = apps.get_model('inventory', 'BranchStock')
    Branch = apps.get_model('inventory', 'Branch')
    QuickSellItem = apps.get_model('inventory', 'QuickSellItem')

    cat, _ = Category.objects.get_or_create(
        name=CATEGORY_NAME, defaults={'prefix': 'KIY'})
    if not cat.prefix:
        cat.prefix = 'KIY'
        cat.save()

    branches = list(Branch.objects.all())

    def next_code():
        mx = 0
        for c in Product.objects.filter(code__startswith='KIY-').values_list('code', flat=True):
            try:
                mx = max(mx, int(c.split('-', 1)[1]))
            except (ValueError, IndexError):
                pass
        return f'KIY-{mx + 1:04d}'

    for item in QuickSellItem.objects.all():
        if item.product_id:
            continue
        product = Product.objects.filter(name__iexact=item.name,
                                         category=cat).first()
        if product is None:
            product = Product.objects.create(
                code=next_code(), name=item.name, category=cat,
                default_sale_price=0, markup_percent=0, is_open_price=False)
        item.product = product
        item.save(update_fields=['product'])

        for price in (item.prices or []):
            try:
                p = int(float(price))
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            variant, _ = ProductVariant.objects.get_or_create(
                product=product, size=str(p), color='')
            for b in branches:
                BranchStock.objects.get_or_create(
                    variant=variant, branch=b,
                    defaults={'stock_count': 0, 'sale_price': p,
                              'cost_price': 0, 'wholesale_price': 0})


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0037_quicksellitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='quicksellitem',
            name='product',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='quick_sell_items',
                help_text='Ombor yuritiladigan mahsulot (Kiyim Kechak kategoriyasida)',
                to='inventory.product'),
        ),
        migrations.RunPython(build, migrations.RunPython.noop),
    ]
