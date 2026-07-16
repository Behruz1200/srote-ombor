"""Delete AuditLog rows older than N months to keep the audit table bounded.

Usage:
    python manage.py prune_audit_log            # deletes rows older than 18 months
    python manage.py prune_audit_log --months 24
    python manage.py prune_audit_log --dry-run  # count only, no delete

Schedule monthly (systemd timer or cron).
"""
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from inventory.models import AuditLog


class Command(BaseCommand):
    help = "Prune AuditLog rows older than --months (default 18)."

    def add_arguments(self, parser):
        parser.add_argument('--months', type=int, default=18,
                            help='Retention window in months (default 18).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report how many rows would be deleted, delete nothing.')

    def handle(self, *args, **opts):
        months = max(1, opts['months'])
        cutoff = timezone.now() - timedelta(days=months * 30)
        qs = AuditLog.objects.filter(created_at__lt=cutoff)
        n = qs.count()
        if opts['dry_run']:
            self.stdout.write(
                f"[dry-run] {n} audit rows older than {cutoff:%Y-%m-%d} "
                f"({months} months) would be deleted.")
            return
        # delete in batches to avoid a huge single transaction on large tables
        total = 0
        while True:
            ids = list(qs.values_list('id', flat=True)[:5000])
            if not ids:
                break
            deleted, _ = AuditLog.objects.filter(id__in=ids).delete()
            total += deleted
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {total} audit rows older than {cutoff:%Y-%m-%d} "
            f"({months} months)."))
