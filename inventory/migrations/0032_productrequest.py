# Generated for ProductRequest (Mijoz so'rovlari / customer demand log)

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0031_alter_product_markup_percent'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(db_index=True, help_text="Mijoz so'ragan mahsulot nomi", max_length=200)),
                ('note', models.CharField(blank=True, help_text="Qo'shimcha: brend, o'lcham, rang yoki izoh", max_length=255)),
                ('customer_phone', models.CharField(blank=True, help_text="Mijoz telefoni — mahsulot kelganda xabar berish uchun", max_length=40)),
                ('status', models.CharField(choices=[('new', 'Kutilmoqda'), ('stocked', 'Keltirildi'), ('dismissed', 'Rad etildi')], default='new', max_length=12)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='product_requests', to='inventory.branch')),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='product_requests', to=settings.AUTH_USER_MODEL)),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='resolved_requests', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': "Mijoz so'rovi",
                'verbose_name_plural': "Mijoz so'rovlari",
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['status', '-created_at'], name='inv_prodreq_status_dt')],
            },
        ),
    ]
