"""Maxsus template filterlar — yurit uchun."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template
from django.utils.html import escape          # AUD-1
from django.utils.safestring import mark_safe  # AUD-1

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


@register.filter
def audit_val(value):
    """AUD-1: AuditLog.changes qiymatini o'qiladigan ko'rinishga keltiradi.

    Ilgari shablon HAR DOIM `pair.0 -> pair.1` deb chizardi. O'chirish
    yozuvida esa qiymat JUFTLIK emas, butun SNAPSHOT lug'ati bo'ladi —
    natijada "1 ta o'zgarish" deb yozilib, ichi BO'SH ko'rinardi.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2 \
            and not isinstance(value[0], (list, tuple, dict)) \
            and not isinstance(value[1], (list, tuple, dict)):
        old = '—' if value[0] in (None, '') else value[0]
        new = '—' if value[1] in (None, '') else value[1]
        return mark_safe(
            f'<span class="text-danger">{escape(str(old)[:60])}</span> '
            f'&rarr; <span class="text-success">{escape(str(new)[:60])}</span>')
    if isinstance(value, dict):
        parts = [f'{escape(str(k))}={escape(str(v)[:40])}'
                 for k, v in list(value.items())[:30]]
        return mark_safe(', '.join(parts))
    if isinstance(value, (list, tuple)):
        return mark_safe('<br>'.join(escape(str(x)[:120]) for x in value[:50]))
    return escape(str(value)[:200])


# ===========================================================================
# CORE-4 — sahifa sarlavhasi (dash-hero) bitta joyda
# ===========================================================================
# 40 ta blok, 33 ta fayl, aynan bir xil skelet. Endi bitta teg.

from django.template import Node, TemplateSyntaxError   # noqa: E402
from django.template.base import token_kwargs           # noqa: E402
from django.template.loader import render_to_string     # noqa: E402


class _HeroSlotNode(Node):
    """{% herosub %} / {% heroeyebrow %} — HTML bo'lgan matn uchun.

    Oddiy matnni teg argumenti sifatida yozish mumkin
    (`sub="..."`), lekin ichida `<span>` yoki `{{ ... }}` bo'lsa
    argumentga sig'maydi — o'sha holatda shu ichki teg ishlatiladi.
    """

    def __init__(self, slot, nodelist):
        self.slot = slot
        self.nodelist = nodelist

    def render(self, context):
        try:
            slots = context.render_context[_HERO_SLOTS]
        except KeyError:
            slots = None
        html = self.nodelist.render(context).strip()
        if slots is not None:
            slots[self.slot] = mark_safe(html)
            return ''          # amallar orasida chizilmaydi
        return html


_HERO_SLOTS = object()


class _HeroNode(Node):
    def __init__(self, kwargs, nodelist):
        self.kwargs = kwargs
        self.nodelist = nodelist

    def render(self, context):
        ctx = {k: v.resolve(context) for k, v in self.kwargs.items()}
        slots = {}
        context.render_context[_HERO_SLOTS] = slots
        try:
            body = self.nodelist.render(context).strip()
        finally:
            # RenderContext.pop() Context APIsi — kalit bo'yicha o'chirmaydi.
            try:
                del context.render_context[_HERO_SLOTS]
            except KeyError:
                pass
        ctx.update(slots)
        ctx['actions'] = mark_safe(body) if body else ''
        ctx.setdefault('title', '')
        return render_to_string('inventory/_hero.html', ctx)


def _slot_tag(name, slot):
    def compile_slot(parser, token):
        nodelist = parser.parse((f'end{name}',))
        parser.delete_first_token()
        return _HeroSlotNode(slot, nodelist)
    return compile_slot


register.tag('herosub', _slot_tag('herosub', 'sub'))
register.tag('heroeyebrow', _slot_tag('heroeyebrow', 'eyebrow'))
register.tag('herotitle', _slot_tag('herotitle', 'title'))
# {% heroextra %} — sarlavha yonidagi standart bo'lmagan blok
# (masalan "Jami ochiq qarz" ustuni). Amallar tugmasi EMAS.
register.tag('heroextra', _slot_tag('heroextra', 'extra'))


@register.tag('hero')
def hero_tag(parser, token):
    """{% hero "Sarlavha" icon=... eyebrow=... sub=... %} ... {% endhero %}"""
    bits = token.split_contents()[1:]
    kwargs = {}
    title = None
    if bits and '=' not in bits[0]:
        title = parser.compile_filter(bits.pop(0))
    kwargs = token_kwargs(bits, parser, support_legacy=False) if bits else {}
    if bits:
        raise TemplateSyntaxError(f'hero: tushunarsiz argument: {bits}')
    if title is not None:
        kwargs['title'] = title
    if 'title' not in kwargs:
        raise TemplateSyntaxError('hero: sarlavha (title) kerak')
    nodelist = parser.parse(('endhero',))
    parser.delete_first_token()
    return _HeroNode(kwargs, nodelist)


# ===========================================================================
# CORE-5 — modal qobig'i bitta joyda
# ===========================================================================

_MODAL_SLOTS = object()


class _ModalSlotNode(Node):
    def __init__(self, slot, nodelist):
        self.slot = slot
        self.nodelist = nodelist

    def render(self, context):
        html = self.nodelist.render(context).strip()
        try:
            slots = context.render_context[_MODAL_SLOTS]
        except KeyError:
            return html
        slots[self.slot] = mark_safe(html)
        return ''


class _ModalNode(Node):
    def __init__(self, kwargs, nodelist):
        self.kwargs = kwargs
        self.nodelist = nodelist

    def render(self, context):
        ctx = {k: v.resolve(context) for k, v in self.kwargs.items()}
        slots = {}
        context.render_context[_MODAL_SLOTS] = slots
        try:
            body = self.nodelist.render(context).strip()
        finally:
            try:
                del context.render_context[_MODAL_SLOTS]
            except KeyError:
                pass
        ctx.update(slots)
        ctx['body'] = mark_safe(body)
        ctx.setdefault('footer', '')
        return render_to_string('inventory/_modal.html', ctx)


@register.tag('modal')
def modal_tag(parser, token):
    bits = token.split_contents()[1:]
    kwargs = {}
    mid = None
    if bits and '=' not in bits[0]:
        mid = parser.compile_filter(bits.pop(0))
    kwargs = token_kwargs(bits, parser, support_legacy=False) if bits else {}
    if bits:
        raise TemplateSyntaxError(f'modal: tushunarsiz argument: {bits}')
    if mid is not None:
        kwargs['id'] = mid
    if 'id' not in kwargs:
        raise TemplateSyntaxError('modal: id kerak')
    nodelist = parser.parse(('endmodal',))
    parser.delete_first_token()
    return _ModalNode(kwargs, nodelist)


@register.tag('modalfooter')
def modalfooter_tag(parser, token):
    nodelist = parser.parse(('endmodalfooter',))
    parser.delete_first_token()
    return _ModalSlotNode('footer', nodelist)
