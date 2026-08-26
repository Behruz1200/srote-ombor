from django.db import migrations, models


class Migration(migrations.Migration):
    """REF-3: qaytarishда kassadan chiqqan haqiqiy naqд snapshot'i.

    Bir dona qaytганда — o'z narxi; butun chek qaytганда — chek jamisi
    (chegirма ayirilgan). Har qaytarish chek to'loviдan oshmaydi. Snapshot
    bo'lgani uchun tarix qotadi (eski cheklar qayta chop etilганда o'zgarmaydi).
    """

    dependencies = [
        ('inventory', '0058_alter_return_idempotency_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='return',
            name='refund_cash',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True,
                help_text='Qaytarishда kassadan HAQIQIY chiqqan naqd (snapshot). '
                          'Chek to\'loviдan oshmaydi.'),
        ),
        migrations.AddConstraint(
            model_name='return',
            constraint=models.CheckConstraint(
                condition=models.Q(refund_cash__isnull=True)
                          | models.Q(refund_cash__gte=0),
                name='return_refund_cash_nonneg'),
        ),
    ]
