"""Thread-local current request user/IP for audit logging.

Django signals don't have access to the HttpRequest. We stash the
authenticated user and client IP on a thread-local at the start of
each request so signals can pull them out.
"""
import threading

_local = threading.local()


def get_current_user():
    return getattr(_local, 'user', None)


def get_current_ip():
    return getattr(_local, 'ip', None)


def _client_ip(request):
    # AUTH-5: X-Forwarded-For'ning BIRINCHI elementiga ishonmaymiz — uni
    # mijoz o'zi soxta qo'yib, lockout budjetini yangilab olardi. Nginx
    # X-Real-IP'ni $remote_addr (haqiqiy mijoz) ga QAT'IY o'rnatadi va uni
    # mijoz XFF orqali almashtira olmaydi. Shu bois X-Real-IP hukmron.
    real = request.META.get('HTTP_X_REAL_IP', '').strip()
    if real:
        return real
    return request.META.get('REMOTE_ADDR')


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.user = getattr(request, 'user', None)
        _local.ip = _client_ip(request)
        try:
            return self.get_response(request)
        finally:
            _local.user = None
            _local.ip = None


# SEC-3: Content-Security-Policy. Barcha kutubxonalar o'zimizda (SEC-2) bo'lgani
# uchun tashqi skript/uslub/shrift YUKLASH TAQIQLANADI. Inline skriptlar hali
# borligi uchun 'unsafe-inline' qoladi (uni olib tashlash = ARCH-1 refaktoring),
# lekin tashqi in'ektsiya, plagin (object), base-uri, form hijack va freymга
# solish (clickjacking) bloklanadi. Kamera skaneri uchun blob/worker/wasm ruxsat.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https://api.qrserver.com",
    "font-src 'self'",
    "connect-src 'self' blob: data:",
    "worker-src 'self' blob:",
    "media-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class ContentSecurityPolicyMiddleware:
    """CSP sarlavhasini qo'shadi. Muammo bo'lsa env CSP_REPORT_ONLY=1 qo'yiб
    faqat-hisobot rejimiga o'tkazish mumkin (hech narsa bloklamaydi)."""
    def __init__(self, get_response):
        self.get_response = get_response
        from django.conf import settings
        self.report_only = bool(getattr(settings, 'CSP_REPORT_ONLY', False))
        self.header = ('Content-Security-Policy-Report-Only'
                       if self.report_only else 'Content-Security-Policy')

    def __call__(self, request):
        resp = self.get_response(request)
        # Django admin o'z inline skriptlariga ega — uni buzmaslik uchun
        # tegmaymiz (admin baribir cheklangan foydalanuvchilar uchun).
        if not request.path.startswith('/admin') and 'Content-Security-Policy' not in resp:
            resp[self.header] = _CSP
        return resp


class ShopClosedMiddleware:
    """SHOP-1/SHOP-2: ochiq (autentifikatsiyasiz) onlayn do'kon ombor
    zaxirasini bloklamaydi, pul so'ramaydi, hech kimni xabardor qilmaydi
    (notify_web_order mavjud emas), va buyurtma sahifasi mijoz ma'lumotlarini
    (ism/telefon/manzil) ochib qo'yadi. Hozircha 0 ta buyurtma — shuning uchun
    butun /shop/ kanalini YOPAMIZ (SHOP_ENABLED=False). To'g'ri qurilganда
    (zaxira bron, to'lov, xabar) env'da SHOP_ENABLED=1 bilan ochiladi."""
    def __init__(self, get_response):
        self.get_response = get_response
        from django.conf import settings
        self.enabled = bool(getattr(settings, 'SHOP_ENABLED', False))

    def __call__(self, request):
        if not self.enabled and request.path.startswith('/shop/'):
            from django.http import HttpResponseNotFound
            return HttpResponseNotFound(
                "<h3>Onlayn do'kon vaqtincha yopiq.</h3>")
        return self.get_response(request)


class RequireAdminTwoFactorMiddleware:
    """SEC-1: yoqilganда (settings.REQUIRE_ADMIN_2FA=True) 2FA o'rnatmagan
    adminni majburan 2FA sozlash sahifasiga yo'naltiradi. Standart — O'CHIQ
    (hech kim qulflanib qolmasin). Egasi tayyor bo'lganда env'da yoqadi."""
    _ALLOW = ('/security/2fa', '/logout', '/login', '/static', '/media')

    def __init__(self, get_response):
        self.get_response = get_response
        from django.conf import settings
        self.enabled = bool(getattr(settings, 'REQUIRE_ADMIN_2FA', False))

    def __call__(self, request):
        if self.enabled:
            u = getattr(request, 'user', None)
            if (u and u.is_authenticated and getattr(u, 'is_admin', lambda: False)()
                    and not getattr(u, 'totp_confirmed', False)
                    and not any(request.path.startswith(p) for p in self._ALLOW)):
                from django.shortcuts import redirect
                from django.contrib import messages
                messages.warning(request,
                    "Administrator hisobi uchun 2FA majburiy. Iltimos, sozlang.")
                return redirect('security_2fa')
        return self.get_response(request)
