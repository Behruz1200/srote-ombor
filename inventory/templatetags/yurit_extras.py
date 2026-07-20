"""Maxsus template filterlar — yurit uchun."""
from django import template

register = template.Library()


@register.filter
def som(value):
    """Sonni bo'shliq bilan formatlash, masalan 1234567 → '1 234 567'

    Ajratuvchi — UZILMAYDIGAN bo'shliq (\u00a0). Oddiy bo'shliq bo'lsa,
    tor joyda (chek, etiketka) son "39" va "000" bo'lib ikki qatorga
    bo'linib ketardi.
    """
    if value is None or value == '':
        return ''
    try:
        n = round(float(value))
    except (ValueError, TypeError):
        return value
    sign = '-' if n < 0 else ''
    return sign + f'{abs(n):,}'.replace(',', '\u00a0')


@register.filter
def div(value, divisor):
    """Bo'lish: value / divisor. Templatda intensity hisoblash uchun."""
    try:
        d = float(divisor)
        if d == 0:
            return 0
        return float(value) / d
    except (ValueError, TypeError):
        return 0


@register.filter
def mul(value, factor):
    """Ko'paytirish: value * factor. Foiz hisobi uchun."""
    try:
        return float(value) * float(factor)
    except (ValueError, TypeError):
        return 0


@register.filter
def get_item(d, key):
    """dict[key] template'da. Dict bo'lmasa None qaytaradi."""
    if hasattr(d, 'get'):
        return d.get(key)
    return None


@register.simple_tag(takes_context=True)
def querystring(context, **kwargs):
    """Joriy GET parametrlarini saqlab, ba'zilarini almashtiradi.

    Foydalanish: <a href="?{% querystring sort=name page=2 %}">...
    None qiymat berilsa, o'sha parametr olib tashlanadi.
    """
    request = context.get('request')
    if not request:
        return ''
    qd = request.GET.copy()
    for k, v in kwargs.items():
        if v is None or v == '':
            qd.pop(k, None)
        else:
            qd[k] = v
    return qd.urlencode()


@register.filter
def som_with_decimals(value, places=2):
    """Decimal joylar bilan formatlash, masalan 1234.5 → '1 234.50'"""
    if value is None or value == '':
        return ''
    try:
        n = float(value)
    except (ValueError, TypeError):
        return value
    formatted = f'{n:,.{places}f}'
    int_part, _, dec_part = formatted.partition('.')
    return int_part.replace(',', '\u00a0') + ('.' + dec_part if dec_part else '')


# ── Stock-aggregation helpers used by product_list KPI strip ────────────
# Each iterates the queryset client-side once. For typical catalog sizes
# (under a few thousand items) this is cheaper than a separate DB round-
# trip and keeps the template self-contained.
def _stock_value(p):
    """Pull the annotated total_stock without crashing on weird shapes."""
    try:
        v = getattr(p, 'total_stock', None)
        return int(v) if v is not None else 0
    except (ValueError, TypeError):
        return 0


@register.filter
def out_of_stock_count(products):
    return sum(1 for p in (products or []) if _stock_value(p) == 0)


@register.filter
def low_stock_count(products):
    return sum(1 for p in (products or []) if 0 < _stock_value(p) <= 3)


@register.filter
def in_stock_count(products):
    return sum(1 for p in (products or []) if _stock_value(p) > 0)
