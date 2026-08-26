"""Maxsus template filterlar — yurit uchun."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

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
    # MON-24: PUL yaxlitlashi — YARMI YUQORIGA, `round()` ning bank
    # yaxlitlashi EMAS. round(2.5) → 2 (juftga), kassir esa 3 kutadi; shu
    # tufayli Z-hisobotda qatorlar chop etilgan JAMIga qo'shilmasdi.
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        n = int(abs(d).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (ValueError, TypeError, InvalidOperation, ArithmeticError):
        return value
    sign = '-' if (d < 0 and n != 0) else ''   # "-0" chiqmasin
    return sign + f'{n:,}'.replace(',', '\u00a0')


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

@register.filter
def dict_get(d, key):
    """Shablonda lug'atdan kalit bo'yicha qiymat: {{ counts|dict_get:s }}"""
    try:
        return d.get(key, '')
    except AttributeError:
        return ''

# --- Do'kon sayti: rasmsiz kartochkalar chiroyli ko'rinishi uchun ---
_TILE_COLORS = [
    ('#EFEAE3', '#A97C4B'),   # iliq qum / bronza
    ('#E8E4DC', '#7A6A56'),
    ('#F1ECE5', '#96784F'),
    ('#E9E5E0', '#6E6459'),
    ('#F2EDE6', '#B08A5E'),
    ('#EAE6DF', '#857A66'),
    ('#EDE8E1', '#9C7A50'),
    ('#E6E2DA', '#77705F'),
]


def _tile_idx(text):
    h = 0
    for ch in str(text or ''):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % len(_TILE_COLORS)


@register.filter
def tile_bg(text):
    """Nom/kategoriyaga qarab barqaror ochiq fon rangi."""
    return _TILE_COLORS[_tile_idx(text)][0]


@register.filter
def tile_fg(text):
    """Shu fonga mos to'q rang."""
    return _TILE_COLORS[_tile_idx(text)][1]


@register.filter
def initials(text, count=2):
    """Nomdan bosh harflar: "Doctor S Shampun" -> "DS"."""
    words = [w for w in str(text or '').split() if w[:1].isalnum()]
    return ''.join(w[0].upper() for w in words[:int(count)]) or '?'
