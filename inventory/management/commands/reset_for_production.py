"""
Wipe all business data before a real production launch.

USE WITH CARE. This deletes every sale, transaction, shift, transfer,
intake, stocktake, product, variant, stock, customer, payment intent,
promotion, return, parked sale, audit log entry, and payment QR.

Users are ALWAYS preserved. Branches and Categories are kept by default
(they are structural, not seed data) unless you pass --wipe-branches /
--wipe-categories.

Refuses to run without --yes so it cannot go off by accident.

Usage on Render:
  python manage.py reset_for_production --yes
  python manage.py reset_for_production --yes --wipe-branches --wipe-categories

Local test:
  ./venv/bin/python manage.py reset_for_production --yes
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import (
    AuditLog, Branch, BranchStock, Category, Customer, Intake, ParkedSale,
    PaymentIntent, PaymentQR, Product, ProductVariant, Promotion, Return, Sale,
    SaleTransaction, Shift, Stocktake, StocktakeCount, Supplier, Transfer,
    TransferLine, User,
)


BUSINESS_MODELS_ORDERED = [
    # Delete in an order that respects FK dependencies (children first).
    ('AuditLog',       AuditLog),
    ('Return',         Return),
    ('Sale',           Sale),
    ('ParkedSale',     ParkedSale),
    ('PaymentIntent',  PaymentIntent),
    ('SaleTransaction', SaleTransaction),
    ('Shift',          Shift),
    ('StocktakeCount', StocktakeCount),
    ('Stocktake',      Stocktake),
    ('TransferLine',   TransferLine),
    ('Transfer',       Transfer),
    ('Intake',         Intake),
    ('BranchStock',    BranchStock),
    ('ProductVariant', ProductVariant),
    ('Promotion',      Promotion),
    ('Product',        Product),
    ('PaymentQR',      PaymentQR),
    ('Customer',       Customer),
    ('Supplier',       Supplier),
]


class Command(BaseCommand):
    help = 'Wipe all business data before a real production launch. Users always preserved.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes', action='store_true',
            help='Actually run the wipe. Without this, the command dry-runs.',
        )
        parser.add_argument(
            '--wipe-branches', action='store_true',
            help='Also delete Branch records (default: keep).',
        )
        parser.add_argument(
            '--wipe-categories', action='store_true',
            help='Also delete Category records (default: keep).',
        )

    def handle(self, *args, **opts):
        dry_run = not opts['yes']
        wipe_branches = opts['wipe_branches']
        wipe_categories = opts['wipe_categories']

        header = 'DRY RUN — no changes' if dry_run else '⚠️  LIVE WIPE — deleting data'
        self.stdout.write(self.style.WARNING(f'\n=== {header} ==='))
        self.stdout.write(f'Users preserved:      always ({User.objects.count()} accounts)')
        self.stdout.write(f'Branches:             {"WIPE" if wipe_branches else "keep"} '
                          f'({Branch.objects.count()} rows)')
        self.stdout.write(f'Categories:           {"WIPE" if wipe_categories else "keep"} '
                          f'({Category.objects.count()} rows)')
        self.stdout.write('')

        counts_before = {name: model.objects.count() for name, model in BUSINESS_MODELS_ORDERED}
        for name, _ in BUSINESS_MODELS_ORDERED:
            self.stdout.write(f'  {name:<18} {counts_before[name]:>10,}')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                '\nDry run only. Rerun with --yes to actually delete.'
            ))
            return

        with transaction.atomic():
            deleted_total = 0
            for name, model in BUSINESS_MODELS_ORDERED:
                deleted, _ = model.objects.all().delete()
                deleted_total += deleted
                if deleted:
                    self.stdout.write(f'  ✂  {name:<18} -{deleted:,}')
            if wipe_branches:
                d, _ = Branch.objects.all().delete()
                deleted_total += d
                self.stdout.write(f'  ✂  Branch             -{d:,}')
            if wipe_categories:
                d, _ = Category.objects.all().delete()
                deleted_total += d
                self.stdout.write(f'  ✂  Category           -{d:,}')

        self.stdout.write(self.style.SUCCESS(
            f'\n✔ done. Total rows removed: {deleted_total:,}. '
            f'Users still: {User.objects.count()}.'
        ))
