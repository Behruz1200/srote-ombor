"""Send today's sales summary to Telegram.

Usage:
    python manage.py send_daily_summary
    python manage.py send_daily_summary --date 2026-05-26
    python manage.py send_daily_summary --dry-run    # print, don't send

Schedule with launchd (macOS) or cron (Linux). Example crontab line
to send every day at 20:00:
    0 20 * * *  cd /path/to/yurit && ./venv/bin/python manage.py send_daily_summary
"""
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone

from inventory.notifications import daily_summary_text, send_telegram, _enabled


class Command(BaseCommand):
    help = "Send today's sales summary to the configured Telegram chats."

    def add_arguments(self, parser):
        parser.add_argument('--date', help='YYYY-MM-DD (defaults to today)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the message instead of sending it')

    def handle(self, *args, **opts):
        if opts['date']:
            d = datetime.strptime(opts['date'], '%Y-%m-%d').date()
        else:
            d = timezone.localdate()

        msg = daily_summary_text(d)
        if opts['dry_run']:
            self.stdout.write(msg)
            return

        if not _enabled():
            self.stderr.write(self.style.WARNING(
                'Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_IDS). '
                'Nothing sent. Run with --dry-run to preview.'))
            return

        ok = send_telegram(msg)
        if ok:
            self.stdout.write(self.style.SUCCESS(f'Daily summary for {d} sent.'))
        else:
            self.stderr.write(self.style.ERROR(f'Send failed for {d}.'))
