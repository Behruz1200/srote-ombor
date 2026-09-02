from django.db import migrations, models


class Migration(migrations.Migration):
    """REF-3: qaytarishda kassadan chiqqan haqiqiy naqd snapshot'i.

    Bir dona qaytganda — o'z narxi; butun chek qaytganda — chek jamisi
    (chegirma ayirilgan). Har qaytarish chek to'lovidan oshmaydi. Snapshot
    bo'lgani uchun tarix qotadi (eski cheklar qayta chop etilganda o'zgarmaydi).
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
                help_text='Qaytarishda kassadan HAQIQIY chiqqan naqd (snapshot). '
                          'Chek to\'lovidan oshmaydi.'),
        ),
        migrations.AddConstraint(
            model_name='return',
            constraint=models.CheckConstraint(
                condition=models.Q(refund_cash__isnull=True)
                          | models.Q(refund_cash__gte=0),
                name='return_refund_cash_nonneg'),
        ),
    ]
