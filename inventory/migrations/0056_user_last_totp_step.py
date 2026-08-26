# AUTH-3: oxirgi qabul qilingan TOTP vaqt-qadami (replay bloklash uchun)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0055_alter_intake_variant'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='last_totp_step',
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
    ]
