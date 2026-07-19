# yurit — Ombor boshqaruv tizimi

Ko'p filialli do'kon uchun ombor va sotuv boshqaruv tizimi. Django + SQLite + Bootstrap.

## Asosiy imkoniyatlar

- **Ko'p filial** — har filialning alohida zaxirasi, narxi, sotuvchilari
- **Foydalanuvchilar** — admin va sotuvchi rollari (RBAC)
- **Mahsulot kodlari** — `OYO-0001` kabi kategoriya prefiksi + ketma-ket raqam
- **Foiz markup** — qabul paytida tannarx + foiz → avtomatik sotuv narxi
- **QR skanerlash** — telefon kamera bilan tezkor qidiruv
- **Chop etiluvchi QR etiketkalar** — A4 sahifaga ko'p mahsulotni birdaniga
- **Hisobotlar** — sotuvlar, qabullar, ombor holati, PDF/CSV eksport
- **Biznes tahlili** — top sellers, slow movers, filiallar taqqoslash, kunlik/soatlik trendlar, foyda tahlili, tovar aylanmasi
- **Uzbek (lotin) interfeysi**

## Stack

- Python 3.13, Django 6.0
- SQLite (dev) — Postgresga osongina ko'chirsa bo'ladi
- Bootstrap 5 (CDN)
- Chart.js (analytics charts)
- ReportLab (PDF eksport)
- qrcode + Pillow (QR generation)
- html5-qrcode (camera scanner)

## Ishga tushirish

```bash
# Bog'liqliklarni o'rnatish
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# DB migratsiyasi
python manage.py migrate

# Superuser yaratish
python manage.py createsuperuser

# (Ixtiyoriy) Dummy ma'lumotlar bilan to'ldirish
python seed.py

# Server
python manage.py runserver
```

http://127.0.0.1:8000 ochiladi.

## Test hisoblari (`seed.py` ishga tushirilgan bo'lsa)

| Username | Parol | Rol | Filial |
|---|---|---|---|
| admin | admin123 | Administrator | (barchasi) |
| sotuvchi1 | sotuvchi123 | Sotuvchi | Chilonzor |
| aziza | sotuvchi123 | Sotuvchi | Chilonzor |
| sotuvchi2 | sotuvchi123 | Sotuvchi | Yunusobod |
| munisa | sotuvchi123 | Sotuvchi | Yunusobod |
| sotuvchi3 | sotuvchi123 | Sotuvchi | Sergeli |

## Asosiy sahifalar

| URL | Tavsif |
|---|---|
| `/login/` | Kirish |
| `/lookup/` | Kod orqali qidiruv (kamera skaneri bilan) |
| `/dashboard/` | Admin bosh sahifasi |
| `/insights/` | Biznes tahlili — top/slow, foyda, trendlar |
| `/reports/` | Sotuvlar, qabullar hisobotlari (PDF/CSV) |
| `/products/` | Mahsulotlar ro'yxati + etiketka chop etish |
| `/branches/` | Filiallar |
| `/users/` | Foydalanuvchilar |
| `/intake/` | Yangi qabul |
| `/admin/` | Django built-in admin |

## Deploy (production)

Ishlab turgan sayt — **https://koreysbozor.uz** (o'z VPS'imizda, PostgreSQL).
Deploy bitta buyruq bilan mahalliy kompyuterdan bajariladi:

```bash
./deploy.sh
```

`deploy.sh` ketma-ketligi:
1. `manage.py check` — Django tekshiruvi
2. `manage.py check_js` — barcha sahifalardagi inline JS sintaksisi (xato bo'lsa deploy TO'XTAYDI)
3. `rsync` — kodni serverga ko'chirish
4. serverda `yurit-deploy`: deploydan oldin `pg_dump` zaxira → `migrate` → `collectstatic` → restart
5. smoke test: `https://koreysbozor.uz/login/`

> Eslatma: loyiha avval Render'da ham turgan (`render.yaml` + `build.sh`).
> Hozir production faqat VPS'da — Render ishlatilmaydi.

## Production'ga chiqarishdan oldin

- [ ] `SECRET_KEY` env variable'ga ko'chirish
- [ ] `DEBUG = False`
- [ ] `ALLOWED_HOSTS` to'g'rilash
- [ ] SQLite o'rniga PostgreSQL
- [ ] HTTPS sertifikati (kamera skaneri uchun majburiy)
- [ ] Static fayllar uchun WhiteNoise / Nginx
- [ ] Backup strategy (db + media)
