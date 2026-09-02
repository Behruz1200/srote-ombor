"""DISC-2: 0060 backfill'ini tuzatish — sababsiz chegirma AKSIYA emas.

0060 shunday deb o'ylagan edi: "sababsiz chegirma faqat AKSIYA bo'lishi
mumkin, chunki pos_checkout qo'lda chegirma uchun sababni MAJBURIY qiladi".
Bu FAQAT 2026-08-26 dan keyingi ma'lumot uchun to'g'ri — sabab talabi aynan
o'sha kuni qo'shilgan (a2d1286). `order_discount` esa 2026-06-03 dan beri
bor. Ya'ni qariyb uch oy davomida kassir sababsiz chegirma bera olardi va
server uni qabul qilardi.

Ishlab chiqarish ma'lumoti buni tasdiqladi:
    Promotion jami: 0   (aktiv: 0)
Ya'ni _evaluate_promotions() hech qachon noldan boshqa son qaytara olmagan —
AKSIYA texnik jihatdan MUMKIN EMAS edi. Demak 0060 belgilagan 3 001 635 so'm
(289 chek) aslida KASSIR bergan chegirma.

Xato yo'nalishi xavfli edi: kassir bergan pulni egasining marketing qarori
qilib ko'rsatardi, ya'ni kuzatilishi kerak bo'lgan signalni YASHIRARDI.
Aniqlab bo'lmaganda har doim MUAMMONI KO'RSATADIGAN tomonga og'ish kerak.

Pul o'zgarmaydi: `order_discount` tegilmaydi, faqat yorliq ustuni.
"""
from django.db import migrations, models


def fix_promo_backfill(apps, schema_editor):
    Txn = apps.get_model('inventory', 'SaleTransaction')
    Promotion = apps.get_model('inventory', 'Promotion')

    # 0060 backfill'ining IZI: sabab bo'sh va promo AYNAN order_discount ga
    # teng qilib qo'yilgan. Faqat shu qatorlarga tegamiz — pos_checkout
    # haqiqiy _server_promo yozgan qatorlar (aksiya yaratilgach) tegilmaydi.
    qs = Txn.objects.filter(order_discount__gt=0, discount_reason='',
                            promo_discount=models.F('order_discount'))

    # Himoya: agar bu migratsiya ishlaganda aksiya allaqachon mavjud bo'lsa,
    # faqat ENG ERTA aksiyadan OLDINGI cheklarni tuzatamiz — o'shandan
    # keyingilari haqiqatan aksiya bo'lishi mumkin.
    first_promo = (Promotion.objects.order_by('valid_from')
                   .values_list('valid_from', flat=True).first())
    if first_promo is not None:
        qs = qs.filter(sold_at__lt=first_promo)

    qs.update(promo_discount=0)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0060_discount_split"),
    ]

    operations = [
        # Orqaga qaytarish ma'nosiz: noto'g'ri qiymatni tiklash kerak emas.
        migrations.RunPython(fix_promo_backfill, migrations.RunPython.noop),
    ]
