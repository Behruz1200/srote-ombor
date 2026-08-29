"""OPS-15 — fon ishlarini bajaradi. systemd timer har daqiqada chaqiradi."""
from django.core.management.base import BaseCommand
from inventory.jobs import run_once


class Command(BaseCommand):
    help = "Navbatdagi fon ishlarini bajaradi."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20,
                            help='Bir aylanishda nechta ish (default 20).')

    def handle(self, *args, **opts):
        ok, err = run_once(limit=opts['limit'])
        if ok or err:
            self.stdout.write(f"bajarildi: {ok}, xato: {err}")
