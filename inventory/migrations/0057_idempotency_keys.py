# S3/S5: Return/CashPayout/CashIn uchun takroriy yozuvni bloklovchi kalit

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0056_user_last_totp_step'),
    ]

    operations = [
        migrations.AddField(
            model_name='return',
            name='idempotency_key',
            field=models.CharField(blank=True, editable=False, max_length=64,
                                   null=True, unique=True),
        ),
        migrations.AddField(
            model_name='cashpayout',
            name='idempotency_key',
            field=models.CharField(blank=True, editable=False, max_length=64,
                                   null=True, unique=True),
        ),
        migrations.AddField(
            model_name='cashin',
            name='idempotency_key',
            field=models.CharField(blank=True, editable=False, max_length=64,
                                   null=True, unique=True),
        ),
        migrations.AddField(
            model_name='intakesession',
            name='idempotency_key',
            field=models.CharField(blank=True, editable=False, max_length=64,
                                   null=True, unique=True),
        ),
    ]
