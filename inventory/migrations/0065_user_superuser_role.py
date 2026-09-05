"""ROLE-1 — uchinchi rol: SUPERUSER (do'kon egasi).

Ilgari "egasi" alohida rol emas, Django'ning is_superuser bayrog'i edi.
Shu sababli filial administratori bilan egasi bir xil `role='admin'`
qiymatiga ega bo'lib, tizim ularni ajrata olmasdi — Xonqa admini Koreys
Bozor tushumini ko'rardi.

Bu migratsiya mavjud hisoblarni to'g'ri rolga ko'chiradi:
  * is_superuser=True bo'lgan har kim -> role='superuser' (egasi);
  * qolganlari o'z rolida qoladi (admin filialiga bog'lanadi).

Orqaga qaytarish: 'superuser' rollarini 'admin' ga qaytaradi —
is_superuser bayrog'i tegilmagani uchun huquq yo'qolmaydi.
"""
from django.db import migrations, models


def promote_owners(apps, schema_editor):
    User = apps.get_model('inventory', 'User')
    User.objects.filter(is_superuser=True).exclude(role='superuser') \
        .update(role='superuser')


def demote_owners(apps, schema_editor):
    User = apps.get_model('inventory', 'User')
    User.objects.filter(role='superuser').update(role='admin')


class Migration(migrations.Migration):

    dependencies = [('inventory', '0064_auditlog_batch')]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[('superuser', 'Egasi (SuperUser)'),
                         ('admin', 'Administrator'),
                         ('sotuvchi', 'Sotuvchi')],
                default='sotuvchi', max_length=20),
        ),
        migrations.RunPython(promote_owners, demote_owners),
    ]
