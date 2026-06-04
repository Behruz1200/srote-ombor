"""
Django settings for Srote.

Two modes, controlled by environment variables (loaded from .env or
the host platform):

  DEBUG=1        local development; SQLite; permissive CSRF/hosts
  DEBUG=0        production; Postgres via DATABASE_URL; strict origins

The defaults are dev-friendly so `runserver` works with no env set up.
"""
from pathlib import Path
import os

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
DEBUG = _bool('DEBUG', default=True)

# In production, SECRET_KEY MUST be set in env. Locally we accept the
# insecure default so 'manage.py runserver' just works.
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-c+=*&dh#&)r!htow17kij3%cax9=bgqa7=a99i#j7=2wimlf#p'
)

# Comma-separated list. Render auto-injects RENDER_EXTERNAL_HOSTNAME.
_hosts_env = os.environ.get('ALLOWED_HOSTS', '*' if DEBUG else '')
ALLOWED_HOSTS = [h.strip() for h in _hosts_env.split(',') if h.strip()] or ['*']
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)


# ---------- Telegram (optional) ----------
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
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
_default_demo_seconds = '8' if DEBUG else '0'
try:
    DEMO_AUTO_PAY_SECONDS = int(os.environ.get('DEMO_AUTO_PAY_SECONDS', _default_demo_seconds))
except ValueError:
    DEMO_AUTO_PAY_SECONDS = 0 if not DEBUG else 8


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
]

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
# Production: DATABASE_URL set by Render (postgres://...)
# Dev: fall back to SQLite
DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=not DEBUG and bool(os.environ.get('DATABASE_URL')),
    )
}


AUTH_USER_MODEL = 'inventory.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 4}},
]


# ---------- Locale & formatting ----------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Tashkent'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True
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
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.db.backends': {'level': 'WARNING'},
    },
}
