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
