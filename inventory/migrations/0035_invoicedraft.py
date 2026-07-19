import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0034_intakesession_agent'),
    ]

    operations = [
        migrations.CreateModel(
            name='InvoiceDraft',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('supplier_text', models.CharField(blank=True, max_length=200)),
                ('invoice_number', models.CharField(blank=True, max_length=80)),
                ('image', models.ImageField(blank=True, null=True,
                                            upload_to='invoices/drafts/')),
                ('payload', models.JSONField(
                    default=dict,
                    help_text='Jadval qatorlari va sarlavha maydonlari')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='invoice_drafts', to='inventory.branch')),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='invoice_drafts',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Faktura qoralamasi',
                'verbose_name_plural': 'Faktura qoralamalari',
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='invoicedraft',
            index=models.Index(fields=['-updated_at'], name='invdraft_updated_dt'),
        ),
    ]
