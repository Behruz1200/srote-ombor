"""Long-poll Telegram and respond to /stock, /today, /help.

Usage:
    python manage.py telegram_polling

This is a long-running process — leave it open in a terminal or
launch it under launchd / systemd. It uses Telegram's long-polling
(getUpdates with timeout=25s) so it's near-real-time and uses
very little CPU.

Authorisation: only chats whose ID is in settings.TELEGRAM_CHAT_IDS
get responses. Others receive "Ruxsat yo'q."

The "update offset" is persisted in .telegram_offset at the project
root so restarts don't replay old messages.
"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from inventory.notifications import handle_command, send_telegram


OFFSET_FILE = Path(settings.BASE_DIR) / '.telegram_offset'


def _api(method, **params):
    token = settings.TELEGRAM_BOT_TOKEN
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


class Command(BaseCommand):
    help = 'Long-poll Telegram and respond to bot commands.'

    def handle(self, *args, **opts):
        if not settings.TELEGRAM_BOT_TOKEN:
            self.stderr.write(self.style.ERROR(
                'TELEGRAM_BOT_TOKEN not set. Add it to .env first.'))
            return

        allowed = {c.strip() for c in (settings.TELEGRAM_CHAT_IDS or '').split(',')
                   if c.strip()}

        offset = 0
        if OFFSET_FILE.exists():
            try:
                offset = int(OFFSET_FILE.read_text().strip()) + 1
            except (ValueError, OSError):
                offset = 0

        self.stdout.write(self.style.SUCCESS(
            f'Telegram polling started. offset={offset}, '
            f'allowed_chats={sorted(allowed) or "(any)"}'
        ))

        while True:
            try:
                resp = _api('getUpdates', offset=offset, timeout=25,
                            allowed_updates=json.dumps(['message']))
            except (urllib.error.URLError, json.JSONDecodeError) as e:
                self.stderr.write(f'getUpdates error: {e}')
                time.sleep(3)
                continue

            if not resp.get('ok'):
                self.stderr.write(f'API error: {resp}')
                time.sleep(3)
                continue

            for upd in resp.get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message') or {}
                chat_id = str(msg.get('chat', {}).get('id', ''))
                text = (msg.get('text') or '').strip()

                if not chat_id or not text:
                    continue

                if allowed and chat_id not in allowed:
                    self.stdout.write(f'(blocked) chat={chat_id} text={text!r}')
                    send_telegram(
                        "Ruxsat yo'q. Administrator bilan bog'laning.",
                        chat_id=chat_id)
                    continue

                self.stdout.write(f'chat={chat_id} text={text!r}')
                try:
                    handle_command(chat_id, text)
                except Exception as e:
                    self.stderr.write(f'handle_command failed: {e}')
                    send_telegram(
                        f"⚠️ Ichki xato: {e}",
                        chat_id=chat_id)

                try:
                    OFFSET_FILE.write_text(str(offset - 1))
                except OSError:
                    pass
