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
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
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
