import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0035_invoicedraft'),
    ]

    operations = [
        migrations.CreateModel(
            name='InvoiceImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='invoices/pages/')),
                ('order', models.PositiveIntegerField(default=1,
                                                      help_text='Sahifa raqami')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('draft', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='pages', to='inventory.invoicedraft')),
                ('session', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='pages', to='inventory.intakesession')),
            ],
            options={
                'verbose_name': 'Faktura sahifasi',
                'verbose_name_plural': 'Faktura sahifalari',
                'ordering': ['order', 'id'],
            },
        ),
    ]
