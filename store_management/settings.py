"""
Django settings for yurit.

Two modes, controlled by environment variables (loaded from .env or
the host platform):

  DEBUG=1        local development; SQLite; permissive CSRF/hosts
  DEBUG=0        production; Postgres via DATABASE_URL; strict origins

The defaults are dev-friendly so `runserver` works with no env set up.
"""
from pathlib import Path
import os
import sys

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------- .env loader (local dev) ----------
# On Render / other hosts, env vars are set on the platform directly,
# so .env is missing in production -- that's fine.
_env_path = BASE_DIR / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith('#') or '=' not in _line:
            continue
        _k, _v = _line.split('=', 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def _bool(name, default=False):
    return os.environ.get(name, str(int(default))).lower() in ('1', 'true', 'yes', 'on')


# ---------- Core ----------
# XAVFSIZ DEFAULT: DEBUG default = FALSE. Bir noto'g'ri/yo'q env o'zgaruvchisi
# endi production'ni debug-rejimга tushirib qo'ymaydi (SEC-10).
DEBUG = _bool('DEBUG', default=False)

from django.core.exceptions import ImproperlyConfigured

# Production'da SECRET_KEY MAJBURIY. Yo'q bo'lsa — baland ovozda xato (jimgina
# repodagi ochiq kalit bilan ishlab ketmaydi). Faqat DEBUG'da lokal default.
SECRET_KEY = os.environ.get('SECRET_KEY', '')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-c+=*&dh#&)r!htow17kij3%cax9=bgqa7=a99i#j7=2wimlf#p'
    else:
        raise ImproperlyConfigured(
            'SECRET_KEY environment variable is required in production '
            '(DEBUG is off). Set it and redeploy.')

# Comma-separated list. Render auto-injects RENDER_EXTERNAL_HOSTNAME.
# '*' fallback FAQAT DEBUG'da — production'da host validation o'chib qolmasin.
_hosts_env = os.environ.get('ALLOWED_HOSTS', '')
ALLOWED_HOSTS = [h.strip() for h in _hosts_env.split(',') if h.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ['*'] if DEBUG else []
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)


# ---------- Payments webhook (PAY-1) ----------
# Merchant callback'ini imzolash uchun umumiy maxfiy kalit. BO'SH bo'lsa —
# webhook HAR QANDAY chaqiruvni RAD etadi (hech qanday to'lov 'paid' bo'lmaydi).
# Real provider ulanganda kalitni env'ga qo'yib, provider imzosini tekshiring.
PAYMENTS_WEBHOOK_SECRET = os.environ.get('PAYMENTS_WEBHOOK_SECRET', '')

# Test ishga tushirilганда (manage.py test) tashqi bildirishnomalar (Telegram)
# YUBORILMAYDI — aks holда testdagi soxta sotuvlar haqiqiy kanalни spam qilardi.
TESTING = ('test' in sys.argv) or ('pytest' in sys.modules)

# ---------- Telegram (optional) ----------
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')

# ---------- OPS-1: kunlik shifrlangan baza zaxirasi (offsite) ----------
# `backup_db` buyrug'i pg_dump | gzip | gpg qilib, natijani Telegram'ga
# yuboradi va mahalliy nusxani BACKUP_RETAIN_DAYS kundan keyin o'chiradi.
# BACKUP_GPG_PASSPHRASE bo'lmasa buyruq ISHLAMAYDI (bazada shaxsiy ma'lumot bor,
# shifrlanmagan dump Telegram bulutida qolishi mumkin emas).
BACKUP_DIR = os.environ.get('BACKUP_DIR', str(BASE_DIR / 'backups'))
BACKUP_RETAIN_DAYS = int(os.environ.get('BACKUP_RETAIN_DAYS', '30'))
BACKUP_GPG_PASSPHRASE = os.environ.get('BACKUP_GPG_PASSPHRASE', '')
# Zaxira alohida (maxfiy) kanalga borishi mumkin; bo'sh bo'lsa — odatdagi
# TELEGRAM_CHAT_IDS'ning birinchisiga yuboriladi.
BACKUP_TELEGRAM_CHAT_ID = os.environ.get('BACKUP_TELEGRAM_CHAT_ID', '')

# ---------- AI: faktura rasmidan qatorlarni o'qish ----------
# Kalit bo'lmasa funksiya o'chiq turadi (sahifa ogohlantirish ko'rsatadi).
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5')
# AI-1: faktura o'qishning UMUMIY vaqt budjeti (sekund). Bitta chaqiruv
# amalda ~60 s, burib qayta o'qish esa yana bittasi. Budjet gunicorn
# --timeout dan KICHIK bo'lishi shart, aks holda ishchi jarayon javob
# yetkazilmasдан o'ldiriladi va brauzer bo'sh javob oladi.
AI_INVOICE_BUDGET = float(os.environ.get('AI_INVOICE_BUDGET', '150'))
# OPS-13: shu millisekunddan uzoq ketgan so'rov logga yoziladi. 0 = o'chiq.
SLOW_REQUEST_MS = float(os.environ.get('SLOW_REQUEST_MS', '3000'))
# Burib qayta o'qish (har biri qo'shimcha chaqiruv). 0 = faqat bitta o'qish.
AI_INVOICE_ROTATE = os.environ.get('AI_INVOICE_ROTATE', '1') not in ('0', 'false', 'False')
TELEGRAM_CHAT_IDS = os.environ.get('TELEGRAM_CHAT_IDS', '')

# ---------- Soliq.uz / OFD fiscal integration (optional) ----------
# Empty / 'noop' = development mode, sales are NOT submitted to any OFD.
# 'didox' = use DidoxProvider (configure DIDOX_API_KEY etc.)
# See inventory/fiscal.py for setup steps.
FISCAL_PROVIDER = os.environ.get('FISCAL_PROVIDER', '')

# ---------- SMS receipt (optional) ----------
# Empty / 'noop' = SMS yuborilmaydi (faqat log'ga yoziladi).
# 'eskiz' = EskizProvider (ESKIZ_EMAIL / ESKIZ_PASSWORD env kerak).
# See inventory/sms.py.
SMS_PROVIDER = os.environ.get('SMS_PROVIDER', '')
ESKIZ_EMAIL = os.environ.get('ESKIZ_EMAIL', '')
ESKIZ_PASSWORD = os.environ.get('ESKIZ_PASSWORD', '')

# ---------- Payment providers (QR + muddatli to'lov) ----------
# Vergul bilan ajratilgan provider nomlari ro'yxati. Default: hamma yoqilgan
# (stub rejimda). Real merchant accountlari ulanganda bu yerda chegarlash mumkin.
# Misol: PAYMENT_PROVIDERS_ENABLED=click,payme,uzum
_pp = os.environ.get('PAYMENT_PROVIDERS_ENABLED', '')
PAYMENT_PROVIDERS_ENABLED = [p.strip() for p in _pp.split(',') if p.strip()] or None
# Per-provider credentials (real wiring uchun)
CLICK_MERCHANT_ID = os.environ.get('CLICK_MERCHANT_ID', '')
CLICK_SECRET_KEY = os.environ.get('CLICK_SECRET_KEY', '')
CLICK_SERVICE_ID = os.environ.get('CLICK_SERVICE_ID', '')
PAYME_MERCHANT_ID = os.environ.get('PAYME_MERCHANT_ID', '')
PAYME_SECRET_KEY = os.environ.get('PAYME_SECRET_KEY', '')
UZUM_MERCHANT_ID = os.environ.get('UZUM_MERCHANT_ID', '')
UZUM_API_KEY = os.environ.get('UZUM_API_KEY', '')
ANOR_API_KEY = os.environ.get('ANOR_API_KEY', '')
ALIF_API_KEY = os.environ.get('ALIF_API_KEY', '')
IMAN_API_KEY = os.environ.get('IMAN_API_KEY', '')
ZOODPAY_API_KEY = os.environ.get('ZOODPAY_API_KEY', '')

# Demo: real merchant API ulanmagan paytda PaymentIntent'lar shu soniyalar
# o'tib ketganidan keyin avtomatik 'paid' deb belgilanadi (status polling
# orqali POS shunday ko'radi).
#
# IMPORTANT: production'da bu MUST be 0 — aks holda real to'lovsiz ham
# sotuv yakunlanadi. Default xavfsizlik: DEBUG=True bo'lganda 8 (sinov uchun
# qulaylik), DEBUG=False bo'lganda 0 (real webhook'siz hech narsa paid emas).
# PAY-3: production'da HARD-PIN = 0 (env'da adashib 8 qo'yilsa ham e'tiborsiz).
# Faqat DEBUG'da env orqali sozlash mumkin.
if not DEBUG:
    DEMO_AUTO_PAY_SECONDS = 0
else:
    try:
        DEMO_AUTO_PAY_SECONDS = int(os.environ.get('DEMO_AUTO_PAY_SECONDS', '8'))
    except ValueError:
        DEMO_AUTO_PAY_SECONDS = 8


# ---------- Session / Auth security (S2-S6 hardening) ----------
# S2: Session timeout — 8 hours default (covers typical work shift).
# Production override via SESSION_COOKIE_AGE env if needed.
try:
    SESSION_COOKIE_AGE = int(os.environ.get('SESSION_COOKIE_AGE', '28800'))  # 8h
except ValueError:
    SESSION_COOKIE_AGE = 28800
SESSION_SAVE_EVERY_REQUEST = True  # extends session on activity
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# S3: Password strength — Django's built-in validators
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
]

# S6: File upload limits — protect against memory-DOS via large CSV / image uploads
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024     # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024     # 10 MB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

# S9: Basic Content Security Policy via response headers (defensive defaults)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'SAMEORIGIN'
# Force HTTPS redirect on Render (RENDER_EXTERNAL_HOSTNAME is set there)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = bool(os.environ.get('RENDER_EXTERNAL_HOSTNAME'))
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'


# ---------- CSRF ----------
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'http://192.168.68.108:8000',
    'https://*.trycloudflare.com',
    'https://*.ngrok-free.app',
    'https://*.ngrok.io',
    'https://*.onrender.com',
]
# Allow custom domain via env
_extra_csrf = os.environ.get('CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS += [o.strip() for o in _extra_csrf.split(',') if o.strip()]

# CSRF xatosini muloyim hal qilamiz (403 sahifasi o'rniga login'ga qaytarish)
CSRF_FAILURE_VIEW = 'inventory.views.csrf_failure'


# ---------- Apps & middleware ----------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'inventory',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise: serve static files directly from Django in production
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'inventory.middleware.AuditMiddleware',
    'inventory.middleware.ContentSecurityPolicyMiddleware',   # SEC-3
    'inventory.middleware.RequireAdminTwoFactorMiddleware',   # SEC-1 (gated)
    'inventory.middleware.ShopClosedMiddleware',              # SHOP-1/SHOP-2
    'inventory.middleware.SlowRequestLogMiddleware',          # OPS-13
]

# SHOP-1/SHOP-2: ochiq onlayn do'kon xavfsiz qurilmaguncha YOPIQ (0 buyurtma).
SHOP_ENABLED = _bool('SHOP_ENABLED', default=False)

# SEC-3: muammo chiqsa CSP_REPORT_ONLY=1 — bloklamay faqat konsolда hisobot.
CSP_REPORT_ONLY = _bool('CSP_REPORT_ONLY', default=False)
# SEC-1: adminlar uchun 2FA majburiy (standart o'chiq — lockout bo'lmasin).
REQUIRE_ADMIN_2FA = _bool('REQUIRE_ADMIN_2FA', default=False)

ROOT_URLCONF = 'store_management.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'store_management.wsgi.application'


# ---------- Database ----------
# OPS-3: production DOIM DATABASE_URL (Postgres) talab qiladi. Ilgari u
# yo'q bo'lsa jimgina BO'SH SQLite'ga tushib, real savdolarni hech kim
# zaxiralamaydigan faylga yozardi (va TLS talabini ham tashlardi). Endi
# DEBUG o'chiq bo'lsa-yu DATABASE_URL yo'q bo'lsa — baland ovoz bilan xato.
_DATABASE_URL = os.environ.get('DATABASE_URL')
if not DEBUG and not _DATABASE_URL:
    raise ImproperlyConfigured(
        'DATABASE_URL environment variable is required in production '
        '(DEBUG is off). Refusing to start on the SQLite fallback.')

DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=not DEBUG and bool(_DATABASE_URL),
    )
}

# SQLite has a single global write lock. Without a busy timeout, any
# concurrent writer immediately raises "database is locked". A 20s
# timeout lets requests queue gracefully — the same behaviour Postgres
# gives us natively via row-level locks. Production runs Postgres so
# this only affects local dev.
if DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3':
    DATABASES['default'].setdefault('OPTIONS', {})
    DATABASES['default']['OPTIONS'].setdefault('timeout', 20)

# OPS-14: Postgres uchun timeout'lar. Ilgari faqat SQLite'да timeout bor edi —
# sekin so'rov (yoki bloklangan DB) HAR bir worker'ni cheksiz ushlab, kassa
# butunlay to'xtardi, xatosiz. Endi ulanish 10s, so'rov 30s dan oshsa uziladi.
if DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql':
    DATABASES['default'].setdefault('OPTIONS', {})
    DATABASES['default']['OPTIONS'].setdefault('connect_timeout', 10)
    DATABASES['default']['OPTIONS'].setdefault('options', '-c statement_timeout=30000')


# ---------- Cache ----------
# Per-process local memory cache. Each Gunicorn worker keeps its own copy —
# acceptable here because we cache short-TTL aggregates (60s) where slight
# drift between workers is fine. If we move to multi-host Render we should
# switch to a shared backend (Redis / memcached / DB cache).
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'yurit-default',
        'TIMEOUT': 60,
        'OPTIONS': {'MAX_ENTRIES': 5000},
    }
}


AUTH_USER_MODEL = 'inventory.User'

# AUTH-1: bu yerда AUTH_PASSWORD_VALIDATORS QAYTA belgilanib, yuqoridagi kuchli
# ro'yxatni (min_length=8 + common/numeric/similarity) 4 belgigа tushirib
# yuborardi ('1234' admin paroli sifatida o'tardi). OLIB TASHLANDI — yuqoridagi
# (142-qator) kuchli ro'yxat amal qiladi.


# ---------- Locale & formatting ----------
# E3/V6: UI butunlay o'zbekcha — Django'ning o'rnatilgan forma xatolari ham
# o'zbekcha chiqsin ("Ensure this value is less than..." emas). Raqam formati
# DECIMAL_SEPARATOR='.' va USE_THOUSAND_SEPARATOR=False bilan qat'iy, shu bois
# lokal o'zgargani bilan pul maydonlari NUQTA-o'nlik bo'lib qoladi.
LANGUAGE_CODE = 'uz'
TIME_ZONE = 'Asia/Tashkent'
USE_I18N = True
USE_TZ = True
# FIX: L10N yoqilganda uz lokali VERGUL-o'nlik beradi va settings'dagi
# DECIMAL_SEPARATOR e'tiborsiz qoladi ("133000,00"). Bu count-up JS'ni va
# type="number" inputlarni buzardi (100× xato). FORMAT_MODULE_PATH eng yuqori
# ustunlikка ega — uz uchun NUQTA-o'nlik majburlaymiz (xabarlar uzbekcha qoladi).
FORMAT_MODULE_PATH = 'store_management.formats'
USE_THOUSAND_SEPARATOR = False  # IDs/PKs must render raw (int); money uses the `som` filter (spaces)
THOUSAND_SEPARATOR = ' '
NUMBER_GROUPING = 3
DECIMAL_SEPARATOR = '.'


# ---------- Static & media ----------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

# WhiteNoise: hashed filenames + immutable cache headers in production
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
            if not DEBUG else
            'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'


# ---------- Production hardening ----------
if not DEBUG:
    # Trust Render's X-Forwarded-Proto so request.is_secure() works
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = _bool('SECURE_SSL_REDIRECT', default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'


# ---------- Messages tags -> Bootstrap classes ----------
from django.contrib.messages import constants as messages_constants
MESSAGE_TAGS = {
    messages_constants.DEBUG: 'secondary',
    messages_constants.INFO: 'info',
    messages_constants.SUCCESS: 'success',
    messages_constants.WARNING: 'warning',
    messages_constants.ERROR: 'danger',
}


# ---------- Logging ----------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
        'telegram': {
            'class': 'inventory.telegram_logging.TelegramErrorHandler',
            'level': 'ERROR', 'formatter': 'simple',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.db.backends': {'level': 'WARNING'},
        # Unhandled 500s -> Telegram (production only)
        'django.request': {
            'handlers': ['console'] + ([] if DEBUG else ['telegram']),
            'level': 'ERROR', 'propagate': False,
        },
        # OPS-12: ilova o'z logger.exception chaqiruvlari (fiskal, to'lov, SMS
        # xatolari) ilgari HECH QAYERDA ko'rinmasdi — endi ular ham prodда
        # Telegram'ga chiqadi.
        'inventory': {
            'handlers': ['console'] + ([] if DEBUG else ['telegram']),
            'level': 'ERROR', 'propagate': False,
        },
    },
}
