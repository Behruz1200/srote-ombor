from django.db import migrations, models


def seed(apps, schema_editor):
    """Hozirgi qattiq yozilgan toifalarni bazaga ko'chiramiz."""
    QuickSellItem = apps.get_model('inventory', 'QuickSellItem')
    defaults = [
        ('Paypoq', [2500, 3000, 5000], 'bi-bag', 10),
        ('Ich kiyim', [8000, 10000, 17000], 'bi-person', 20),
        ('Bosh kiyim', [], 'bi-handbag', 30),
    ]
    for name, prices, icon, order in defaults:
        QuickSellItem.objects.get_or_create(
            name=name,
            defaults={'prices': prices, 'icon': icon, 'order': order,
                      'is_active': True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0036_invoiceimage'),
    ]

    operations = [
        migrations.CreateModel(
            name='QuickSellItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('name', models.CharField(
                    help_text="POS'da tugma nomi (masalan: Paypoq)",
                    max_length=60, unique=True)),
                ('prices', models.JSONField(
                    blank=True, default=list,
                    help_text='Narx tugmalari, masalan [2500, 3000, 5000]')),
                ('icon', models.CharField(
                    blank=True, default='bi-bag',
                    help_text='Bootstrap ikonka klassi (bi-bag, bi-person, bi-handbag)',
                    max_length=40)),
                ('order', models.PositiveIntegerField(default=100)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Tezkor sotuv toifasi',
                'verbose_name_plural': 'Tezkor sotuv toifalari',
                'ordering': ['order', 'name'],
            },
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
