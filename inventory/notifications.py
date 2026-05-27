"""Telegram notifications (low-stock alerts).

Setup
-----
1. Talk to @BotFather on Telegram, run /newbot, save the token.
2. Add your bot to a group OR send it a /start in DM, then fetch chat_id:
     curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python -m json.tool
   Look for "chat":{"id": ...} in the result.
3. In your environment (or settings.py):
     TELEGRAM_BOT_TOKEN = "1234567890:ABC..."
     TELEGRAM_CHAT_IDS = "123456789,-1009876543210"  # comma-separated

If not configured, all alert functions become silent no-ops — you
will never see a crash because the bot isn't set up.

Low-stock alerts also dedupe via the Django cache (default: 6h per
variant/branch) so the same item won't spam the chat.
"""
import urllib.parse
import urllib.request
import urllib.error
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

LOW_STOCK_THRESHOLD = 3
DEDUP_TTL_SECONDS = 6 * 60 * 60  # 6 hours per (variant, branch)


def _enabled():
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
    chats = getattr(settings, 'TELEGRAM_CHAT_IDS', None)
    return bool(token and chats)


def _chat_ids():
    raw = getattr(settings, 'TELEGRAM_CHAT_IDS', '') or ''
    return [c.strip() for c in raw.split(',') if c.strip()]


def send_telegram(text, parse_mode='HTML'):
    """Send a message to every configured Telegram chat. Silent no-op if disabled."""
    if not _enabled():
        return False
    token = settings.TELEGRAM_BOT_TOKEN
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    ok = True
    for chat_id in _chat_ids():
        data = urllib.parse.urlencode({
            'chat_id': chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': 'true',
        }).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status >= 300:
                    ok = False
                    logger.warning('Telegram non-200: %s', resp.status)
        except urllib.error.URLError as e:
            ok = False
            logger.warning('Telegram send failed: %s', e)
    return ok


def maybe_low_stock_alert(branch_stock):
    """Called from a post_save signal. Sends one alert per (variant, branch)
    within DEDUP_TTL_SECONDS so we don't spam."""
    if not _enabled():
        return
    if branch_stock.stock_count > LOW_STOCK_THRESHOLD:
        # Stock went up — reset the dedup so the next dip alerts again
        cache.delete(_dedup_key(branch_stock))
        return

    if cache.get(_dedup_key(branch_stock)):
        return  # already alerted recently

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
