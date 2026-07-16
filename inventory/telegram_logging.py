"""Logging handler that pushes ERROR-level records (unhandled 500s) to Telegram.

Deduplicated (same logger+line within 10 min is skipped) and a silent no-op
when Telegram isn't configured. Never raises — logging must not crash the app.
"""
import logging


class TelegramErrorHandler(logging.Handler):
    def emit(self, record):
        try:
            from django.core.cache import cache
            from .notifications import send_telegram, _enabled
            if not _enabled():
                return
            key = f"tg_err:{record.name}:{getattr(record, 'lineno', 0)}"
            if cache.get(key):
                return
            cache.set(key, True, 600)  # 10-minute dedup window
            msg = self.format(record)[:1400].replace('<', '&lt;').replace('>', '&gt;')
            send_telegram("🛑 <b>Server xatosi (500)</b>\n<code>" + msg + "</code>")
        except Exception:
            pass
