"""OPS-1: kunlik SHIFRLANGAN offsite baza zaxirasi -> Telegram.

Bosqichlar:
  1. pg_dump (custom, siqilgan format)  yoki  SQLite faylini nusxalash
  2. gpg --symmetric AES256 bilan shifrlash (BACKUP_GPG_PASSPHRASE)
  3. Telegram'ga sendDocument bilan yuborish (BACKUP_TELEGRAM_CHAT_ID)
  4. Mahalliy nusxalarni BACKUP_RETAIN_DAYS kundan keyin o'chirish

Ishlatish:
    python manage.py backup_db
    python manage.py backup_db --keep-local   # o'chirmasdan
    python manage.py backup_db --no-telegram  # faqat mahalliy fayl

Kron/systemd timer bilan har kecha (masalan 03:00) chaqiriladi. Har qanday
xatoda Telegram'ga OGOHLANTIRISH yuboriladi — jimgina to'xtagan zaxira eng
yomon holat.
"""
import os
import subprocess
import tempfile
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Encrypted database backup, sent offsite to Telegram (OPS-1)."

    def add_arguments(self, parser):
        parser.add_argument('--keep-local', action='store_true',
                            help="Eski mahalliy nusxalarni o'chirmaslik")
        parser.add_argument('--no-telegram', action='store_true',
                            help='Faqat mahalliy shifrlangan fayl yaratish')

    # ---- yordamchilar ----
    def _alert(self, text):
        try:
            from inventory.notifications import send_telegram
            send_telegram(f"🛑 <b>Zaxira XATOSI</b>\n{text}")
        except Exception:
            pass

    def _dump_postgres(self, db, raw_path):
        env = dict(os.environ)
        if db.get('PASSWORD'):
            env['PGPASSWORD'] = db['PASSWORD']
        cmd = ['pg_dump', '--format=custom', '--no-owner', '--no-privileges',
               '--file', raw_path]
        if db.get('NAME'):
            cmd += ['--dbname', db['NAME']]
        if db.get('USER'):
            cmd += ['--username', db['USER']]
        if db.get('HOST'):
            cmd += ['--host', db['HOST']]
        if db.get('PORT'):
            cmd += ['--port', str(db['PORT'])]
        subprocess.run(cmd, env=env, check=True, capture_output=True)

    def _dump_sqlite(self, db, raw_path):
        import gzip
        import shutil
        src = db['NAME']
        with open(src, 'rb') as fin, gzip.open(raw_path, 'wb') as fout:
            shutil.copyfileobj(fin, fout)

    def _encrypt(self, passphrase, raw_path, out_path):
        # gpg simmetrik AES256; parol stdin orqali (buyruq qatorida ko'rinmasin)
        cmd = ['gpg', '--batch', '--yes', '--quiet',
               '--pinentry-mode', 'loopback',
               '--passphrase-fd', '0',
               '--cipher-algo', 'AES256',
               '--symmetric', '--output', out_path, raw_path]
        subprocess.run(cmd, input=passphrase.encode('utf-8'),
                       check=True, capture_output=True)

    def _prune(self, backup_dir, retain_days):
        cutoff = timezone.now() - timedelta(days=retain_days)
        removed = 0
        for name in os.listdir(backup_dir):
            if not name.startswith('yurit-') or not name.endswith('.gpg'):
                continue
            p = os.path.join(backup_dir, name)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(p),
                                               tz=timezone.get_current_timezone())
                if mtime < cutoff:
                    os.remove(p); removed += 1
            except OSError:
                pass
        return removed

    # ---- asosiy ----
    def handle(self, *args, **opts):
        passphrase = getattr(settings, 'BACKUP_GPG_PASSPHRASE', '') or ''
        if not passphrase:
            raise CommandError(
                "BACKUP_GPG_PASSPHRASE o'rnatilmagan. Bazada shaxsiy ma'lumot "
                "bor — shifrlanmagan zaxira yuborilmaydi. /etc/yurit/env ga "
                "kuchli parol qo'ying.")

        backup_dir = getattr(settings, 'BACKUP_DIR', None)
        os.makedirs(backup_dir, exist_ok=True)
        retain = int(getattr(settings, 'BACKUP_RETAIN_DAYS', 30))

        db = settings.DATABASES['default']
        engine = db.get('ENGINE', '')
        stamp = timezone.localtime().strftime('%Y%m%d-%H%M%S')
        out_path = os.path.join(backup_dir, f'yurit-{stamp}.dump.gpg')

        tmp = tempfile.NamedTemporaryFile(delete=False, dir=backup_dir,
                                          suffix='.raw')
        raw_path = tmp.name
        tmp.close()
        try:
            if 'postgresql' in engine:
                self._dump_postgres(db, raw_path)
            elif 'sqlite' in engine:
                self._dump_sqlite(db, raw_path)
            else:
                raise CommandError(f"Qo'llab-quvvatlanmagan DB: {engine}")

            self._encrypt(passphrase, raw_path, out_path)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b'').decode('utf-8', 'replace')[:400]
            self._alert(f"{e.cmd[0]} muvaffaqiyatsiz: {err}")
            raise CommandError(f"Zaxira muvaffaqiyatsiz: {err}")
        except Exception as e:
            self._alert(f"Kutilmagan xato: {e}")
            raise
        finally:
            if os.path.exists(raw_path):
                os.remove(raw_path)

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(
            f"Shifrlangan zaxira: {out_path} ({size_mb:.1f} MB)"))

        if not opts['no_telegram']:
            from inventory.notifications import send_document
            cap = (f"🗄 yurit zaxira · {stamp}\n"
                   f"Hajm: {size_mb:.1f} MB · shifrlangan (AES256)")
            if send_document(out_path, caption=cap):
                self.stdout.write(self.style.SUCCESS('Telegram: yuborildi.'))
            else:
                self._alert("Telegram'ga yuborilmadi — mahalliy nusxa bor, "
                            "lekin offsite emas.")
                self.stderr.write(self.style.ERROR('Telegram yuborilmadi.'))

        if not opts['keep_local']:
            n = self._prune(backup_dir, retain)
            if n:
                self.stdout.write(f"Eski nusxalar o'chirildi: {n} ta "
                                  f"({retain} kundan eski).")
