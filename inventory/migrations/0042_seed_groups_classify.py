"""4 ta bo'lim yaratadi va aniq kategoriyalarni avtomatik tayinlaydi.

Faqat ANIQ kategoriyalar tayinlanadi (nomida erkak/ayol/bola yoki uy-ro'zg'or
kalit so'zi bor). Aralash kiyim kategoriyalari (Mayka, Triko, Finka, Fudbolka,
Kiyim Kechak) bo'sh qoldiriladi — foydalanuvchi qo'lda tayinlaydi.
"""
from django.db import migrations

GROUPS = [
    ('men',   'Erkaklar',            1),
    ('women', 'Ayollar',             2),
    ('kids',  'Bolalar',             3),
    ('home',  'Parfumeriya va uy',   4),
]

# Tekshirish tartibi MUHIM: "bola" eng oldin (Qiz/Ogil Bolalar -> kids),
# keyin ayol, keyin erkak, oxirida uy-ro'zg'or.
KIDS = ('bola', 'detski', 'kids', 'chaqaloq', 'baby')
WOMEN = ('ayol', 'jenski', 'женск', 'dvoyka')
MEN = ('erkak', 'mujski', 'мужск')
HOME = (
    'shampun', 'sovun', 'dezodarant', 'antijir', 'antiper', 'krem', 'gel',
    'kir yuvish', 'soda', 'salfetka', 'tish', 'soch', 'parfum', 'atir',
    'rexona', 'pena', 'vada', 'stanok', 'ochistitel', 'pasuda', 'balzam',
    'kraska', 'roliki', 'vlajniy', 'antizasor', 'antizas', 'depilatsiya',
    'chotka', 'uyda', 'uy ', 'mitellyar', 'micellar', 'gigiyena',
)


def classify(name):
    n = (name or '').lower()
    if any(k in n for k in KIDS):
        return 'kids'
    if any(k in n for k in WOMEN):
        return 'women'
    if any(k in n for k in MEN):
        return 'men'
    if any(k in n for k in HOME):
        return 'home'
    return None


def seed(apps, schema_editor):
    Group = apps.get_model('inventory', 'Group')
    Category = apps.get_model('inventory', 'Category')
    by_slug = {}
    for slug, name, order in GROUPS:
        g, _ = Group.objects.get_or_create(
            slug=slug, defaults={'name': name, 'sort_order': order})
        by_slug[slug] = g
    for cat in Category.objects.all():
        slug = classify(cat.name)
        if slug:
            cat.group = by_slug[slug]
            cat.save(update_fields=['group'])


def unseed(apps, schema_editor):
    Group = apps.get_model('inventory', 'Group')
    Category = apps.get_model('inventory', 'Category')
    Category.objects.update(group=None)
    Group.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0041_group_category_group'),
    ]
    operations = [
        migrations.RunPython(seed, unseed),
    ]
