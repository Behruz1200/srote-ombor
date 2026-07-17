from django.core.cache import cache

PENDING_REQUESTS_CACHE_KEY = 'nav:pending_requests'


def nav_badges(request):
    """Lightweight nav badges. Cached (5 min) and invalidated explicitly
    when a request is added/resolved, so pages don't pay a query each load."""
    u = getattr(request, 'user', None)
    if not (u and u.is_authenticated):
        return {}
    n = cache.get(PENDING_REQUESTS_CACHE_KEY)
    if n is None:
        from .models import ProductRequest
        n = ProductRequest.objects.filter(
            status=ProductRequest.Status.NEW
        ).count()
        cache.set(PENDING_REQUESTS_CACHE_KEY, n, 300)
    return {'pending_requests_count': n}
