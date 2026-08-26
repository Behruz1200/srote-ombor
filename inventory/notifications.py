"""Telegram notifications + bot helpers.

Setup
-----
1. Talk to @BotFather on Telegram, run /newbot, save the token.
2. Add your bot to a group OR send it /start in DM, then fetch chat_id:
     curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python -m json.tool
   Look for "chat":{"id": ...} in the result.
3. Put values in .env (project root, gitignored):
     TELEGRAM_BOT_TOKEN=...
     TELEGRAM_CHAT_IDS=123,-456     # comma-separated

If not configured, all alert functions are silent no-ops.

What lives here
---------------
- send_telegram(text, chat_id=None)  → low-level HTTP POST sender
- maybe_low_stock_alert(branch_stock) → auto-alert when stock <= 3 (with 6h dedup)
- daily_summary_text()               → today's revenue/profit/top performers
- stock_text(code)                   → /stock CODE reply
- handle_command(chat_id, text)      → parse incoming command + reply

The management commands (send_daily_summary, telegram_polling) just
orchestrate these.
"""
import urllib.parse
import urllib.request
import urllib.error
import logging
import re

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

LOW_STOCK_THRESHOLD = 3
DEDUP_TTL_SECONDS = 6 * 60 * 60


def _enabled():
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
    chats = getattr(settings, 'TELEGRAM_CHAT_IDS', None)
    return bool(token and chats)


def _chat_ids():
    raw = getattr(settings, 'TELEGRAM_CHAT_IDS', '') or ''
    return [c.strip() for c in raw.split(',') if c.strip()]


def send_telegram(text, chat_id=None, parse_mode='HTML'):
    """Send a message. If chat_id is None, broadcast to all TELEGRAM_CHAT_IDS."""
    # Test paytida hech narsa yuborilmaydi (soxta sotuvlar kanalни spam qilmasin).
    if getattr(settings, 'TESTING', False):
        return False
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
    if not token:
        return False
    targets = [chat_id] if chat_id else _chat_ids()
    if not targets:
        return False
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    ok = True
    for cid in targets:
        data = urllib.parse.urlencode({
            'chat_id': cid,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': 'true',
        }).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    ok = False
                    logger.warning('Telegram non-200: %s', resp.status)
        except urllib.error.URLError as e:
            ok = False
            logger.warning('Telegram send failed: %s', e)
    return ok


def send_document(file_path, caption='', chat_id=None):
    """OPS-1: fayl (zaxira) yuborish — sendDocument (multipart/form-data).

    chat_id bo'lmasa BACKUP_TELEGRAM_CHAT_ID, u ham bo'lmasa TELEGRAM_CHAT_IDS[0].
    Muvaffaqiyat/xato bo'yicha True/False qaytaradi.
    """
    import os
    import uuid
    if getattr(settings, 'TESTING', False):
        return False
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
    if not token:
        return False
    target = (chat_id
              or getattr(settings, 'BACKUP_TELEGRAM_CHAT_ID', '')
              or (_chat_ids()[0] if _chat_ids() else ''))
    if not target:
        return False
    url = f'https://api.telegram.org/bot{token}/sendDocument'
    boundary = uuid.uuid4().hex
    fname = os.path.basename(file_path)
    with open(file_path, 'rb') as fh:
        payload = fh.read()

    def _part(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="{name}"\r\n\r\n{value}\r\n').encode('utf-8')

    body = b''
    body += _part('chat_id', str(target))
    if caption:
        body += _part('caption', caption)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; '
             f'name="document"; filename="{fname}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
    body += payload + b'\r\n'
    body += f'--{boundary}--\r\n'.encode('utf-8')

    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status >= 300:
                logger.warning('Telegram sendDocument non-200: %s', resp.status)
                return False
    except urllib.error.URLError as e:
        logger.warning('Telegram sendDocument failed: %s', e)
        return False
    return True


def _som(value):
    """Space-grouped integer for Uzbek-style currency."""
    try:
        return f'{int(value):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return str(value)


# ---------- Low-stock alert (auto from signals) ----------

def _variant_descr(variant):
    """Human label: size/color for clothes, else the variant barcode."""
    parts = [p for p in (variant.size, variant.color) if p]
    if parts:
        return ' / '.join(parts)
    return variant.barcode or 'asosiy'


def maybe_low_stock_alert(branch_stock, previous_count=None, created=False):
    """Alert only when stock SELLS DOWN to <= threshold.

    Never fires on intake or when a variant is first stocked (otherwise a
    small received batch would instantly look "low"). Reads the real int in
    case the count arrives as an F()-expression, and sends after the
    surrounding transaction commits.
    """
    if not _enabled():
        return
    from .models import BranchStock
    count = branch_stock.stock_count
    if not isinstance(count, int):
        try:
            count = (BranchStock.objects.only("stock_count")
                     .get(pk=branch_stock.pk).stock_count)
        except BranchStock.DoesNotExist:
            return
    key = _dedup_key(branch_stock)

    if count > LOW_STOCK_THRESHOLD:
        cache.delete(key)
        return

    # Brand-new stock or stock that went up/unchanged (intake) -> skip.
    if created:
        return
    if previous_count is not None:
        try:
            if count >= int(previous_count):
                return
        except (ValueError, TypeError):
            pass

    if cache.get(key):
        return

    variant = branch_stock.variant
    product = variant.product
    badge = '⛔' if count == 0 else '⚠️'
    brand = f"{product.brand} · " if getattr(product, 'brand', '') else ''
    text = (
        f"{badge} <b>Tovar tugab bormoqda</b>\n\n"
        f"{brand}<b>{product.name}</b>\n"
        f"<code>{product.code}</code>  ·  {_variant_descr(variant)}\n"
        f"Filial: <b>{branch_stock.branch.name}</b>\n"
        f"Qoldiq: <b>{count}</b> dona"
    )

    def _fire():
        if send_telegram(text):
            cache.set(key, True, DEDUP_TTL_SECONDS)

    from django.db import transaction
    transaction.on_commit(_fire)


def _dedup_key(bs):
    return f'low_stock_alert:{bs.variant_id}:{bs.branch_id}'


def notify_intake_session(session):
    """Telegram summary after an intake session is completed. Best-effort;
    never raises. Sends after the surrounding transaction commits."""
    if not _enabled():
        return
    try:
        from django.db import transaction
        from .models import Intake
        lines = list(Intake.objects.filter(session=session)
                     .select_related('variant__product'))
        if not lines:
            return
        total_qty = sum(l.quantity for l in lines)
        total_cost = sum(l.quantity * (l.cost_per_unit or 0) for l in lines)
        by_prod = {}
        for l in lines:
            p = l.variant.product
            d = by_prod.setdefault(p.id, {
                'name': p.name, 'brand': getattr(p, 'brand', ''),
                'variants': 0, 'qty': 0})
            d['variants'] += 1
            d['qty'] += l.quantity
        who = getattr(session.received_by, 'username', '') or '-'
        supplier = ''
        if getattr(session, 'supplier_id', None):
            supplier = session.supplier.name
        elif getattr(session, 'supplier_text', ''):
            supplier = session.supplier_text
        head = ["\U0001F4E5 <b>Yangi qabul</b>",
                f"Filial: <b>{session.branch.name}</b> \u00b7 {who}"]
        if supplier:
            head.append(f"Yetkazuvchi: {supplier}")
        prods = list(by_prod.values())
        body = []
        for d in prods[:12]:
            brand = f"{d['brand']} " if d['brand'] else ''
            body.append(f"\u2022 {brand}{d['name']} \u2014 {d['variants']} tur, {d['qty']} dona")
        if len(prods) > 12:
            body.append(f"\u2026 yana {len(prods) - 12} mahsulot")
        foot = [f"Jami: {len(lines)} tur \u00b7 {total_qty} dona \u00b7 "
                f"{_som(total_cost)} so'm"]
        text = '\n'.join(head + [''] + body + [''] + foot)
        transaction.on_commit(lambda: send_telegram(text))
    except Exception:
        pass


# ---------- Daily summary ----------

def daily_summary_text(d=None):
    """Generate today's (or given date's) summary as HTML."""
    from django.db.models import Sum, F, DecimalField, ExpressionWrapper, Count
    from django.utils import timezone
    from .models import Sale, BranchStock

    today = d or timezone.localdate()
    rev_expr = ExpressionWrapper(
        F('quantity') * F('sale_price'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )
    cost_expr = ExpressionWrapper(
        F('quantity') * F('cost_at_sale'),
        output_field=DecimalField(max_digits=14, decimal_places=2)
    )

    qs = Sale.objects.filter(sold_at__date=today)
    agg = qs.aggregate(
        rev=Sum(rev_expr), cost=Sum(cost_expr),
        qty=Sum('quantity'), n=Count('id'),
    )
    rev = agg['rev'] or 0
    cost = agg['cost'] or 0
    profit = rev - cost
    n = agg['n'] or 0
    qty = agg['qty'] or 0
    margin = (profit / rev * 100) if rev else 0

    top_branch = qs.values('branch__name').annotate(r=Sum(rev_expr)) \
        .order_by('-r').first()
    top_seller = qs.values('sold_by__username').annotate(r=Sum(rev_expr)) \
        .order_by('-r').first()

    # MAHSULOT darajasida (variant emas) — 22:00 alohida ro'yxat bilan mos
    # kelishi uchun. Do'kon 1 o'lcham=1 dona bo'lgani uchun variant soni
    # chalg'itardi (butun katalog "kam" ko'rinardi).
    from django.db.models import Sum as _Sum
    _ptot = (BranchStock.objects.exclude(variant__product__is_open_price=True)
             .values('variant__product_id').annotate(t=_Sum('stock_count')))
    out = sum(1 for r in _ptot if (r['t'] or 0) <= 0)
    low = sum(1 for r in _ptot if 0 < (r['t'] or 0) <= PRODUCT_LOW_TOTAL)
    low = low + out  # quyida `low - out` kam qolganlar sonini beradi

    if n == 0:
        return (
            f"📊 <b>Kunlik xulosa — {today:%d.%m.%Y}</b>\n\n"
            f"Bugun sotuvlar bo'lmadi."
        )

    lines = [
        f"📊 <b>Kunlik xulosa — {today:%d.%m.%Y}</b>",
        "",
        f"💰 Daromad: <b>{_som(rev)}</b> so'm",
        f"📦 Tannarx: {_som(cost)} so'm",
        f"✅ Sof foyda: <b>{_som(profit)}</b> so'm  ({margin:.1f}%)",
        f"🛒 Sotuvlar: {n} ta · {qty} dona",
    ]
    if top_branch:
        lines.append(f"\n🏆 Top filial: <b>{top_branch['branch__name']}</b> ({_som(top_branch['r'])} so'm)")
    if top_seller:
        lines.append(f"🥇 Top sotuvchi: <b>{top_seller['sold_by__username']}</b> ({_som(top_seller['r'])} so'm)")
    if out:
        lines.append(f"\n⛔ {out} ta mahsulot butunlay tugagan")
    if low > out:
        lines.append(f"⚠️ {low - out} ta mahsulot kam qoldi (≤{PRODUCT_LOW_TOTAL} dona)")
    if not low and not out:
        lines.append("\n✅ Hech narsa kritik holatda emas")
    return '\n'.join(lines)


# Mahsulot DARAJASIDA "kam qolgan" chegarasi: barcha o'lchamlar bo'yicha JAMI
# qoldiq shu sondan kam bo'lsa — reorder signal. Do'kon asosan 1 o'lcham=1 dona
# kiyim/poyabzal, shuning uchun VARIANT darajasidagi "≤3" butun katalogni
# belgilab, foydasiz bo'lardi — mahsulot jami bo'yicha hisoblaymiz.
PRODUCT_LOW_TOTAL = 3


def low_stock_report_text(threshold=None, limit_per_section=40):
    """Kam qolgan / tugagan tovarlar RO'YXATI — MAHSULOT darajasida (kunlik
    22:00 xulosaga ALOHIDA qo'shimcha xabar; har sotuvда emas).

    Har bir mahsulotning barcha o'lchamlari bo'yicha JAMI qoldiq hisoblanadi:
      ⛔ Butunlay tugagan — jami 0 dona (hech bir o'lcham qolmagan)
      ⚠️ Kam qolgan       — jami 1..threshold dona (oxirgi bir nechta dona)
    Ochiq narxli (is_open_price) tovarlar qoldiq yuritmaydi — kirmaydi.
    Hech narsa bo'lmasa None qaytaradi (xabar yuborilmaydi).
    """
    from django.db.models import Sum, Count, Q
    from django.utils import timezone
    from .models import BranchStock

    thr = PRODUCT_LOW_TOTAL if threshold is None else int(threshold)
    today = timezone.localdate()

    rows = list(BranchStock.objects
                .exclude(variant__product__is_open_price=True)
                .values('variant__product_id',
                        'variant__product__name',
                        'variant__product__code',
                        'variant__product__brand')
                .annotate(total=Sum('stock_count'),
                          sizes=Count('id', filter=Q(stock_count__gt=0)))
                .filter(total__lte=thr)
                .order_by('total', 'variant__product__name'))

    out_rows = [r for r in rows if (r['total'] or 0) <= 0]
    low_rows = [r for r in rows if 0 < (r['total'] or 0) <= thr]

    if not out_rows and not low_rows:
        return None

    def _label(r):
        brand = r['variant__product__brand']
        brand = f"{brand} · " if brand else ''
        return (f"• {brand}{r['variant__product__name']} — "
                f"<code>{r['variant__product__code']}</code>")

    lines = [f"📦 <b>Kam qolgan / tugagan tovarlar — {today:%d.%m.%Y}</b>",
             "<i>(mahsulot bo'yicha jami qoldiq)</i>"]

    if out_rows:
        lines.append(f"\n⛔ <b>Butunlay tugagan: {len(out_rows)} ta mahsulot</b>")
        for r in out_rows[:limit_per_section]:
            lines.append(_label(r))
        if len(out_rows) > limit_per_section:
            lines.append(f"… yana {len(out_rows) - limit_per_section} ta")

    if low_rows:
        lines.append(f"\n⚠️ <b>Kam qolgan (≤{thr} dona): {len(low_rows)} ta mahsulot</b>")
        for r in low_rows[:limit_per_section]:
            lines.append(f"{_label(r)} — <b>{r['total']}</b> dona "
                         f"({r['sizes']} o'lcham)")
        if len(low_rows) > limit_per_section:
            lines.append(f"… yana {len(low_rows) - limit_per_section} ta")

    lines.append("\n🔗 To'liq ro'yxat: koreysbozor.uz/reorder")
    return '\n'.join(lines)


# ---------- /stock CODE ----------

def stock_text(code):
    """Reply for /stock CODE command. Accepts internal code, manufacturer
    barcode, generated variant EAN, product name, or brand."""
    from .models import Product, ProductVariant, BranchStock
    from django.db.models import Q

    raw = (code or '').strip()
    typed = raw.upper()
    m = re.match(r'^([A-Z]+)-?(\d+)$', typed)
    if m:
        typed = f"{m.group(1)}-{int(m.group(2)):04d}"

    # 1) exact internal code, 2) manufacturer barcode, 3) generated variant EAN
    product = Product.objects.filter(
        Q(code=typed) | Q(external_barcode=raw)).first()
    if not product:
        _v = (ProductVariant.objects.filter(barcode=raw)
              .select_related("product").first())
        if _v:
            product = _v.product
    if not product:
        # 4) name / brand search
        matches = list(Product.objects.filter(
            Q(name__icontains=raw) | Q(brand__icontains=raw))[:5])
        if not matches:
            return f"❌ <code>{code}</code> topilmadi."
        if len(matches) > 1:
            lines = [f"🔍 <b>'{code}'</b> bo'yicha topilganlar:\n"]
            for p in matches:
                _b = f"{p.brand} · " if getattr(p, "brand", "") else ""
                lines.append(f"  • <code>{p.code}</code> — {_b}{p.name}")
            lines.append("\nAniq kodni yozing: <code>/stock OYO-0001</code>")
            return '\n'.join(lines)
        product = matches[0]

    stocks = (BranchStock.objects
        .filter(variant__product=product)
        .select_related('variant', 'branch')
        .order_by('branch__name', 'variant__size', 'variant__color'))

    by_branch = {}
    for s in stocks:
        by_branch.setdefault(s.branch.name, []).append(s)

    lines = [f"<b>{product.name}</b>"]
    if getattr(product, 'brand', ''):
        lines.append(f"🏷 Brend: <b>{product.brand}</b>")
    lines.append(
        f"<code>{product.code}</code>  ·  {_som(product.default_sale_price)} so'm")
    if not by_branch:
        lines.append("\nVariantlar yo'q.")
        return '\n'.join(lines)
    for bname, items in by_branch.items():
        total = sum(s.stock_count for s in items)
        emoji = "✅" if total > 10 else "⚠️" if total > 0 else "⛔"
        lines.append(f"\n{emoji} <b>{bname}</b>: {total} dona")
        for s in items:
            if s.stock_count > 0:
                lines.append(f"  · {_variant_descr(s.variant)}: {s.stock_count}")
    return '\n'.join(lines)


# ---------- Command dispatcher (used by telegram_polling) ----------

HELP_TEXT = (
    "<b>yurit bot — komandalar</b>\n\n"
    "/stock KOD — mahsulot zaxirasi\n"
    "  Kod: <code>/stock OYO-0001</code>\n"
    "  Brend/nom: <code>/stock zara</code>\n"
    "  Shtrix/EAN: <code>/stock 2000000000015</code>\n\n"
    "/today — bugungi sotuv xulosasi\n"
    "/help — bu yordam"
)


def handle_command(chat_id, text):
    """Parse a user message and reply."""
    text = (text or '').strip()
    if not text:
        return

    if text.startswith('/start'):
        send_telegram("Salom! yurit bot.\n\n" + HELP_TEXT, chat_id=chat_id)
        return

    if text.startswith('/help'):
        send_telegram(HELP_TEXT, chat_id=chat_id)
        return

    if text.startswith('/today'):
        send_telegram(daily_summary_text(), chat_id=chat_id)
        return

    if text.startswith('/stock'):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_telegram(
                "Foydalanish: <code>/stock OYO-0001</code>",
                chat_id=chat_id)
            return
        send_telegram(stock_text(parts[1]), chat_id=chat_id)
        return

    # Unknown command — only reply if it starts with /
    if text.startswith('/'):
        send_telegram(
            f"Tushunmadim. <code>/help</code> yozing.",
            chat_id=chat_id)
