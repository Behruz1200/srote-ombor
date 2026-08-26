import os
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# SEC-1: admin manzilini o'zgartirish mumkin (env ADMIN_URL). Standart 'admin/'
# — hozircha o'zgarmaydi (hech narsa buzilmaydi). Egasi maxfiy yo'l qo'ymoqchi
# bo'lsa, serverда ADMIN_URL='<maxfiy>/' qo'yadi va qayta deploy qiladi.
_ADMIN_URL = os.environ.get('ADMIN_URL', 'admin/').lstrip('/')
if not _ADMIN_URL.endswith('/'):
    _ADMIN_URL += '/'

urlpatterns = [
    path(_ADMIN_URL, admin.site.urls),
    path('', include('inventory.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
