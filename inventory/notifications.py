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


def _som(value):
    """Space-grouped integer for Uzbek-style currency."""
    try:
        return f'{int(value):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return str(value)


# ---------- Low-stock alert (auto from signals) ----------

def maybe_low_stock_alert(branch_stock):
    if not _enabled():
        return
    if branch_stock.stock_count > LOW_STOCK_THRESHOLD:
        cache.delete(_dedup_key(branch_stock))
        return
    if cache.get(_dedup_key(branch_stock)):
        return
    variant = branch_stock.variant
    product = variant.product
    badge = '⛔' if branch_stock.stock_count == 0 else '⚠️'
    text = (
        f"{badge} <b>Tovar tugab bormoqda</b>\n\n"
        f"<b>{product.name}</b>\n"
        f"<code>{product.code}</code>  ·  {variant.size} / {variant.color}\n"
        f"Filial: <b>{branch_stock.branch.name}</b>\n"
        f"Qoldiq: <b>{branch_stock.stock_count}</b> dona"
    )
    if send_telegram(text):
        cache.set(_dedup_key(branch_stock), True, DEDUP_TTL_SECONDS)


def _dedup_key(bs):
    return f'low_stock_alert:{bs.variant_id}:{bs.branch_id}'


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

    low = BranchStock.objects.filter(stock_count__lte=LOW_STOCK_THRESHOLD).count()
    out = BranchStock.objects.filter(stock_count=0).count()

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
        lines.append(f"\n⛔ {out} ta variant tugagan")
    if low > out:
        lines.append(f"⚠️ {low - out} ta variant kam qoldi (≤{LOW_STOCK_THRESHOLD} dona)")
    if not low and not out:
        lines.append("\n✅ Hech narsa kritik holatda emas")
    return '\n'.join(lines)


# ---------- /stock CODE ----------

def stock_text(code):
    """Reply for /stock CODE command."""
    from .models import Product, BranchStock

    typed = (code or '').strip().upper()
    m = re.match(r'^([A-Z]+)-?(\d+)$', typed)
    if m:
        typed = f"{m.group(1)}-{int(m.group(2)):04d}"

    product = Product.objects.filter(code=typed).first()
    if not product:
        # Try name search
        matches = list(Product.objects.filter(name__icontains=code)[:5])
        if not matches:
            return f"❌ <code>{code}</code> topilmadi."
        if len(matches) > 1:
            lines = [f"🔍 <b>'{code}'</b> bo'yicha topilganlar:\n"]
            for p in matches:
                lines.append(f"  • <code>{p.code}</code> — {p.name}")
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

    lines = [
        f"<b>{product.name}</b>",
        f"<code>{product.code}</code>  ·  {_som(product.default_sale_price)} so'm",
    ]
    if not by_branch:
        lines.append("\nVariantlar yo'q.")
        return '\n'.join(lines)
    for bname, items in by_branch.items():
        total = sum(s.stock_count for s in items)
        emoji = "✅" if total > 10 else "⚠️" if total > 0 else "⛔"
        lines.append(f"\n{emoji} <b>{bname}</b>: {total} dona")
        for s in items:
            if s.stock_count > 0:
                lines.append(f"  · {s.variant.size}/{s.variant.color}: {s.stock_count}")
    return '\n'.join(lines)


# ---------- Command dispatcher (used by telegram_polling) ----------

HELP_TEXT = (
    "<b>yurit bot — komandalar</b>\n\n"
    "/stock KOD — mahsulot zaxirasi\n"
    "  Misol: <code>/stock OYO-0001</code>\n"
    "  Yoki: <code>/stock nike</code>\n\n"
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
