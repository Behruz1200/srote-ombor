"""Maxsus template filterlar — Srote uchun."""
from django import template

register = template.Library()


@register.filter
def som(value):
    """Sonni bo'shliq bilan formatlash, masalan 1234567 → '1 234 567'"""
    if value is None or value == '':
        return ''
    try:
        n = round(float(value))
    except (ValueError, TypeError):
        return value
    sign = '-' if n < 0 else ''
    return sign + f'{abs(n):,}'.replace(',', ' ')


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
    return int_part.replace(',', ' ') + ('.' + dec_part if dec_part else '')
