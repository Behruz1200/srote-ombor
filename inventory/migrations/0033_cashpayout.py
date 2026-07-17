# Generated for CashPayout (Kassa chiqimi / till cash-out log)

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0032_productrequest'),
    ]

    operations = [
        migrations.CreateModel(
            name='CashPayout',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, help_text="Kassadan olingan summa (so'm)", max_digits=12)),
                ('category', models.CharField(choices=[('lunch', 'Tushlik'), ('store', "Do'kon xarajati"), ('repair', "Ta'mirlash"), ('other', 'Boshqa')], default='other', max_length=12)),
                ('note', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cash_payouts', to='inventory.branch')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cash_payouts', to=settings.AUTH_USER_MODEL)),
                ('shift', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payouts', to='inventory.shift')),
            ],
            options={
                'verbose_name': 'Kassa chiqimi',
                'verbose_name_plural': 'Kassa chiqimlari',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['branch', '-created_at'], name='cashpayout_branch_dt')],
                'constraints': [models.CheckConstraint(condition=models.Q(amount__gt=0), name='cashpayout_amount_positive')],
            },
        ),
    ]
