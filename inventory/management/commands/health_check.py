"""Health watchdog: verifies the DB and the public web path, alerts Telegram on
failure and again when service recovers. Run via a systemd timer (~10 min).

Note: an ON-server check cannot detect full-server death — pair this with an
external uptime monitor (e.g. UptimeRobot) hitting /healthz for that case.
"""
import os
import ssl
import urllib.request
from django.core.management.base import BaseCommand

FLAG = '/tmp/yurit_health_alerted'
URL = 'https://koreysbozor.uz/healthz'


class Command(BaseCommand):
    help = "Check DB + public web; alert Telegram on failure/recovery."

    def handle(self, *args, **opts):
        problems = []
        # 1) DB reachable + a real ORM query
        try:
            from inventory.models import SaleTransaction
            SaleTransaction.objects.exists()
        except Exception as e:
            problems.append(f"DB: {str(e)[:120]}")
        # 2) Public web path (nginx -> gunicorn -> app)
        try:
            req = urllib.request.Request(URL, headers={'User-Agent': 'yurit-healthcheck'})
            with urllib.request.urlopen(req, timeout=12,
                                        context=ssl.create_default_context()) as r:
                if r.status != 200:
                    problems.append(f"web: HTTP {r.status}")
        except Exception as e:
            problems.append(f"web: {str(e)[:120]}")

        def notify(text):
            try:
                from inventory.notifications import send_telegram, _enabled
                if _enabled():
                    send_telegram(text)
            except Exception:
                pass

        if problems:
            if not os.path.exists(FLAG):   # alert once until recovery
                notify("🛑 <b>Tizim nosozligi (health check)</b>\n" + "\n".join(problems))
                try:
                    open(FLAG, 'w').close()
                except OSError:
                    pass
            self.stderr.write("UNHEALTHY: " + "; ".join(problems))
        else:
            if os.path.exists(FLAG):
                notify("✅ <b>Tizim tiklandi</b> — health check OK.")
                try:
                    os.remove(FLAG)
                except OSError:
                    pass
            self.stdout.write("OK")
