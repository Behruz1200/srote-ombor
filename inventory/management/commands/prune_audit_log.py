"""AuditLog jadvalini cheklab turadi — lekin PUL izini o'chirmaydi.

    python manage.py prune_audit_log              # 18 oydan eski KATALOG yozuvlari
    python manage.py prune_audit_log --months 24
    python manage.py prune_audit_log --dry-run    # faqat sanaydi
    python manage.py prune_audit_log --all-models # himoyani o'chiradi (ehtiyot!)

OPS-14 — NEGA HIMOYALANGAN RO'YXAT BOR.

Ilgari bu buyruq oynadan eski HAMMA narsani o'chirardi. Lekin SEC-14 bo'yicha
audit jadvaliga ataylab PUL va OMBOR hodisalari qo'shilgan edi (qaytarish,
kassa chiqimi, smen, inventarizatsiya, xodim qarzi, to'lov) — ichki o'g'irlik
tergovi uchun. O'sha yozuvlarni 18 oydan keyin o'chirish aynan shu maqsadni
yo'q qiladi: o'g'irlik ko'pincha ancha keyin aniqlanadi.

Shuning uchun ikki toifa:
  HIMOYALANGAN — pul/ombor/xavfsizlik. Hech qachon avtomatik o'chirilmaydi.
  ODDIY        — katalog tahrirlari (nom, narx maydoni, foydalanuvchi profili).
                 Bular shovqin: hajmning katta qismi shulardan, qiymati past.

Oyiga bir marta ishlating (systemd timer).
"""
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from inventory.models import AuditLog

# Pul, ombor harakati va xavfsizlik — tergov uchun kerak, saqlanadi.
PROTECTED_MODELS = {
    'SaleTransaction', 'Sale', 'Return', 'CashPayout', 'CashIn',
    'Shift', 'Stocktake', 'EmployeeDebt', 'PaymentIntent', 'Transfer',
    'Intake', 'PriceOverride', 'OfflineConflict',
}


class Command(BaseCommand):
    help = ("AuditLog'ni tozalaydi. Pul/ombor yozuvlari saqlanadi "
            "(--all-models bilan majburlash mumkin).")

    def add_arguments(self, parser):
        parser.add_argument('--months', type=int, default=18,
                            help='Saqlash oynasi, oy (default 18).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Faqat sanaydi, hech narsa o‘chirmaydi.')
        parser.add_argument('--all-models', action='store_true',
                            help='Himoyalangan modellarni ham o‘chiradi (ehtiyot!).')

    def handle(self, *args, **opts):
        months = max(1, opts['months'])
        cutoff = timezone.now() - timedelta(days=months * 30)
        qs = AuditLog.objects.filter(created_at__lt=cutoff)

        if not opts['all_models']:
            qs = qs.exclude(model_name__in=PROTECTED_MODELS)

        n = qs.count()
        kept = (AuditLog.objects
                .filter(created_at__lt=cutoff,
                        model_name__in=PROTECTED_MODELS).count())

        if opts['dry_run']:
            self.stdout.write(
                f"[dry-run] {cutoff:%Y-%m-%d} dan eski: {n} ta yozuv "
                f"o'chirilardi; {kept} ta himoyalangan (pul/ombor) qoladi.")
            return

        # Katta jadvalda bitta ulkan tranzaksiya bo'lmasin — bo'laklab.
        total = 0
        while True:
            ids = list(qs.values_list('id', flat=True)[:5000])
            if not ids:
                break
            deleted, _ = AuditLog.objects.filter(id__in=ids).delete()
            total += deleted
        self.stdout.write(self.style.SUCCESS(
            f"O'chirildi: {total} ta ({cutoff:%Y-%m-%d} dan eski). "
            f"Himoyalangan {kept} ta yozuv saqlandi."))
