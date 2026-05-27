"""Boy dummy ma'lumotlar — kiyim + aksessuar + uy + parfyumeriya."""
import os, django, random
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'store_management.settings')
django.setup()

from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from decimal import Decimal

from inventory.models import (
    User, Branch, Category, Product, ProductVariant, BranchStock, Intake, Sale,
)

random.seed(42)
NOW = timezone.now()


def rand_time_on(d):
    hour = random.randint(9, 19)
    minute = random.randint(0, 59)
    return d.replace(hour=hour, minute=minute, second=random.randint(0, 59), microsecond=0)


def days_ago(n):
    return NOW - timedelta(days=n)


# ---------- ADMIN ----------
admin = User.objects.get(username='admin')
admin.role = User.Role.ADMIN
admin.save()


# ---------- 3 ta FILIAL ----------
branches_data = [
    ('Chilonzor Filiali', "Chilonzor 12-mavze, Bunyodkor 35", '+998 71 200 00 01'),
    ('Yunusobod Filiali', "Yunusobod, Amir Temur ko'chasi 45", '+998 71 200 00 02'),
    ('Sergeli Filiali',   "Sergeli, Yangihayot 7", '+998 71 200 00 03'),
]
branches = []
for name, address, phone in branches_data:
    br, _ = Branch.objects.get_or_create(
        name=name, defaults={'address': address, 'phone': phone}
    )
    branches.append(br)


# ---------- SOTUVCHILAR ----------
sellers_per_branch = {}
seller_data = [
    ('sotuvchi1', 'Aziz', 'Karimov', branches[0]),
    ('aziza', 'Aziza', 'Yusupova', branches[0]),
    ('sotuvchi2', 'Dilshod', 'Rahimov', branches[1]),
    ('munisa', 'Munisa', 'Soliyeva', branches[1]),
    ('sotuvchi3', 'Bekzod', 'Tursunov', branches[2]),
]
for username, fn, ln, branch in seller_data:
    s, created = User.objects.get_or_create(
        username=username,
        defaults={'role': User.Role.SOTUVCHI, 'branch': branch,
                  'first_name': fn, 'last_name': ln,
                  'email': f'{username}@srote.local'}
    )
    if created:
        s.set_password('sotuvchi123')
        s.save()
    sellers_per_branch.setdefault(branch.id, []).append(s)


# ---------- KATEGORIYALAR (prefix bilan) ----------
categories_spec = [
    ('Oyoq kiyimlar',      'OYO'),
    ("Ko'ylaklar",          'KOY'),
    ('Shimlar',            'SHI'),
    ('Kurtkalar',          'KUR'),
    ('Aksessuarlar',       'AKS'),
    ('Bolalar kiyimi',     'BOL'),
    ('Telefon aksessuarlari', 'TEL'),
    ('Uy va oshxona',      'UYO'),
    ('Parfyumeriya',       'PAR'),
    ('Gigiena va kosmetika', 'GIG'),
]
categories = {}
for cname, prefix in categories_spec:
    c, created = Category.objects.get_or_create(
        name=cname, defaults={'prefix': prefix}
    )
    if not c.prefix:
        c.prefix = prefix
        c.save()
    categories[cname] = c


# ---------- MAHSULOTLAR ----------
# (name, category, sizes, colors, tannarx, sotuv_narx)
products_spec = [
    # Kiyim
    ('Nike Air Sneaker',   'Oyoq kiyimlar', ['38','39','40','41','42','43','44'], ['Qora','Oq',"Ko'k"], 600000, 850000),
    ('Adidas Stan Smith',  'Oyoq kiyimlar', ['38','39','40','41','42','43'], ['Oq',"Ko'k","Yashil"], 720000, 950000),
    ('Puma Run',           'Oyoq kiyimlar', ['39','40','41','42','43'], ['Qora','Kulrang'], 540000, 780000),
    ("Ko'ylak Slim Fit",   "Ko'ylaklar",    ['S','M','L','XL','XXL'], ['Oq','Havorang','Pushti'], 180000, 280000),
    ('T-Shirt Sport',      "Ko'ylaklar",    ['S','M','L','XL'], ['Qora','Oq','Kulrang','Qizil'], 95000, 165000),
    ('Polo Klassik',       "Ko'ylaklar",    ['M','L','XL'], ['Qora',"Ko'k",'Yashil'], 145000, 240000),
    ('Jeans Classic',      'Shimlar',       ['30','32','34','36','38'], ["Ko'k","Qora"], 220000, 380000),
    ('Shim Klassik',       'Shimlar',       ['30','32','34','36','38'], ['Qora','Jigarrang'], 250000, 420000),
    ('Kurtka Qishki',      'Kurtkalar',     ['M','L','XL','XXL'], ['Qora',"Ko'k",'Yashil'], 850000, 1450000),
    ('Vetrovka Bahor',     'Kurtkalar',     ['M','L','XL'], ['Qora','Sariq'], 380000, 620000),
    ('Charm Sumka',        'Aksessuarlar',  ['Yagona'], ['Qora','Jigarrang'], 320000, 580000),
    ('Kepka',              'Aksessuarlar',  ['Yagona'], ['Qora','Oq',"Ko'k",'Qizil'], 45000, 95000),
    ('Bola Krasovka',      'Bolalar kiyimi', ['28','30','32','34','36'], ['Qizil',"Ko'k",'Pushti'], 280000, 480000),
    ("Bola Ko'ylak",       'Bolalar kiyimi', ['XS','S','M'], ['Sariq','Oq','Yashil'], 85000, 155000),

    # Telefon aksessuarlari
    ('iPhone Lightning Kabel', 'Telefon aksessuarlari', ['1m','2m','3m'], ['Oq','Qora'], 35000, 75000),
    ('Type-C Kabel',           'Telefon aksessuarlari', ['1m','2m'], ['Oq','Qora','Qizil'], 28000, 65000),
    ('Bluetooth Naushnik',     'Telefon aksessuarlari', ['Yagona'], ['Oq','Qora',"Ko'k"], 180000, 320000),
    ('Powerbank 10000mAh',     'Telefon aksessuarlari', ['10000mAh','20000mAh'], ['Qora','Oq'], 220000, 380000),
    ("Telefon g'ilof",         'Telefon aksessuarlari', ['iPhone 14','iPhone 15','Samsung S23'], ['Tiniq','Qora','Ko\'k'], 45000, 120000),
    ('Quvvatlovchi Adapter 20W','Telefon aksessuarlari', ['Yagona'], ['Oq'], 85000, 150000),

    # Uy/oshxona
    ('Elektr choynak 1.7L',    'Uy va oshxona', ['1.7L'], ['Qora','Oq'], 280000, 450000),
    ("Pichoq to'plami 5-pred", 'Uy va oshxona', ['5 dona'], ['Oddiy','Premium'], 320000, 550000),
    ("Tarelka to'plami",       'Uy va oshxona', ['6 ta','12 ta'], ['Oq','Naqshli'], 220000, 380000),
    ('Sochiq paxta',           'Uy va oshxona', ['50x90','70x140'], ['Oq',"Ko'k",'Yashil','Pushti'], 65000, 130000),
    ('Plitka tozalovchi 5L',   'Uy va oshxona', ['5L'], ['Yagona'], 45000, 85000),

    # Parfyumeriya
    ('Erkak parfyum (50ml)',   'Parfyumeriya', ['50ml','100ml'], ['Boss','Hugo','Versace','Dior'], 280000, 580000),
    ('Ayol parfyum (50ml)',    'Parfyumeriya', ['50ml','100ml'], ['Chanel','Dior','Lancome','YSL'], 320000, 680000),
    ('Tualet suvi (200ml)',    'Parfyumeriya', ['200ml'], ['Klassik','Sport','Fresh'], 95000, 180000),

    # Gigiena/kosmetika
    ('Erkak deodorant',        'Gigiena va kosmetika', ['150ml','200ml'], ['Sport','Klassik','Fresh'], 35000, 75000),
    ('Ayol deodorant',         'Gigiena va kosmetika', ['150ml','200ml'], ['Roza','Vanil','Tsitrus'], 38000, 80000),
    ('Sovun (3-dona to\'plam)','Gigiena va kosmetika', ['3 dona'], ['Yagona'], 18000, 42000),
    ('Sochga shampun 500ml',   'Gigiena va kosmetika', ['500ml','1L'], ['Quruq','Yog\'li','Normal'], 45000, 95000),
]

suppliers = ['Toshkent Distributor', 'Chorsu Bozori', "Buyuk Ipak Yo'li",
             'Olympia Trade', 'Asia Textile', 'Korea Style', 'Mega Planet',
             'Beauty World', 'Tech Supply', 'Home Plus']

products = []
print("Mahsulotlar yaratilmoqda...")

with transaction.atomic():
    for name, cat_name, sizes, colors, cost, price in products_spec:
        if Product.objects.filter(name=name).exists():
            continue
        markup = round((Decimal(str(price)) / Decimal(str(cost)) - 1) * 100, 2)
        p = Product.objects.create(
            name=name, category=categories[cat_name],
            description=f"{name} — {cat_name}",
            default_sale_price=Decimal(str(price)),
            markup_percent=markup,
        )
        products.append((p, sizes, colors, cost, price))
        print(f"  {p.code:<12} {p.name}")


# ---------- VARIANTLAR + INTAKE + STOCK ----------
print("\nVariantlar va qabullar yaratilmoqda...")
total_intakes = 0

with transaction.atomic():
    for p, sizes, colors, cost, price in products:
        for size in sizes:
            for color in colors:
                v = ProductVariant.objects.create(product=p, size=size, color=color)
                for br in branches:
                    initial_qty = random.randint(8, 22)
                    intake_date = days_ago(random.randint(35, 60))
                    BranchStock.objects.create(
                        variant=v, branch=br,
                        stock_count=initial_qty,
                        cost_price=Decimal(str(cost)),
                        sale_price=Decimal(str(price)),
                    )
                    Intake.objects.create(
                        variant=v, branch=br,
                        quantity=initial_qty,
                        cost_per_unit=Decimal(str(cost)),
                        supplier=random.choice(suppliers),
                        received_by=admin,
                        received_at=rand_time_on(intake_date),
                        note='Bosh ombor qabuli',
                    )
                    total_intakes += 1

                    if random.random() < 0.35:
                        extra_qty = random.randint(3, 10)
                        extra_date = days_ago(random.randint(5, 30))
                        bs = BranchStock.objects.get(variant=v, branch=br)
                        bs.stock_count += extra_qty
                        bs.save()
                        Intake.objects.create(
                            variant=v, branch=br,
                            quantity=extra_qty,
                            cost_per_unit=Decimal(str(cost)),
                            supplier=random.choice(suppliers),
                            received_by=admin,
                            received_at=rand_time_on(extra_date),
                            note="Qo'shimcha qabul",
                        )
                        total_intakes += 1

print(f"  Jami qabullar: {total_intakes}")


# ---------- SOTUVLAR ----------
# Idempotent: if any sales already exist, skip the historical generator
# so re-running this script doesn't pile up duplicate fake sales.
if Sale.objects.exists():
    total_sales = Sale.objects.count()
    total_revenue = 0
    print(f"\nSotuvlar tarixi allaqachon bor ({total_sales} yozuv) — o'tkazib yuborildi.")
else:
    print("\nSotuvlar yaratilmoqda (oxirgi 30 kun)...")
    total_sales = 0
    total_revenue = 0
    with transaction.atomic():
        for day_offset in range(0, 30):
            day = days_ago(day_offset)
            for br in branches:
                sales_today = random.randint(2, 12)
                sellers = sellers_per_branch.get(br.id, [])
                if not sellers:
                    continue
                for _ in range(sales_today):
                    stocks = list(BranchStock.objects.filter(
                        branch=br, stock_count__gte=1
                    ).select_related('variant__product'))
                    if not stocks:
                        continue
                    bs = random.choice(stocks)
                    qty = random.randint(1, min(3, bs.stock_count))
                    price = bs.sale_price if bs.sale_price > 0 else bs.variant.product.default_sale_price
                    if random.random() < 0.15:
                        price = price * Decimal('0.9')
                    seller = random.choice(sellers)
                    Sale.objects.create(
                        variant=bs.variant, branch=br,
                        quantity=qty, sale_price=price,
                        cost_at_sale=bs.cost_price,
                        sold_by=seller,
                        sold_at=rand_time_on(day),
                        note=random.choice(['', '', '', 'Mijoz: doimiy', 'Naqd', 'Karta']),
                    )
                    bs.stock_count -= qty
                    bs.save()
                    total_sales += 1
                    total_revenue += qty * price

print(f"  Jami sotuvlar:  {total_sales}")
print(f"  Jami daromad:   {total_revenue:,.0f} so'm")


# ---------- XULOSA ----------
print("\n" + "="*60)
print("HISOBLAR")
print("="*60)
print(f"  Admin:     admin / admin123")
for username, fn, ln, br in seller_data:
    print(f"  {username:10s} / sotuvchi123  ({br.name})")

print("\n" + "="*60)
print("STATISTIKA")
print("="*60)
print(f"  Filiallar:        {Branch.objects.count()}")
print(f"  Foydalanuvchilar: {User.objects.count()}")
print(f"  Kategoriyalar:    {Category.objects.count()}")
print(f"  Mahsulotlar:      {Product.objects.count()}")
print(f"  Variantlar:       {ProductVariant.objects.count()}")
print(f"  Zaxira yozuvlari: {BranchStock.objects.count()}")
print(f"  Qabullar:         {Intake.objects.count()}")
print(f"  Sotuvlar:         {Sale.objects.count()}")

print("\n" + "="*60)
print("KODLAR (kategoriya bo'yicha)")
print("="*60)
for c in Category.objects.all():
    products_in_cat = Product.objects.filter(category=c)
    if products_in_cat.exists():
        print(f"\n  {c.prefix}:  {c.name}")
        for p in products_in_cat[:3]:
            print(f"    {p.code:<12} {p.name}")
        if products_in_cat.count() > 3:
            print(f"    ... va yana {products_in_cat.count() - 3} ta")
