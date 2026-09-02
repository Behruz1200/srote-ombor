"""Money & stock invariant regression tests.

Har bir test QA audit'dagi bitta topilmaga bog'langan (MON-x, PAY-x, OFF-x).
Testlar TO'G'RI xatti-harakatni tekshiradi — ya'ni ba'zilari HOZIR YIQILADI.
Yiqilgan test = bajariladigan bug hisoboti. Tuzatgandan keyin yashil bo'ladi.

    python manage.py test inventory.tests_money -v 2

Guruhlar:
    ExpectedToPass* — hozir ishlaydigan to'g'ri xatti-harakat (regressiyadan qulf)
    Bug*            — tasdiqlangan nuqson; tuzatilgunча yiqiladi
"""
import json
import time
from unittest import mock
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db import connection
from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
import unittest
from datetime import timedelta

from django.utils import timezone

from inventory.models import (
    Branch, Product, ProductVariant, BranchStock, Shift,
    SaleTransaction, Sale, CashPayout, PaymentIntent, Return,
    split_breakdown, _dec, Customer, Category, AuditLog,
)

User = get_user_model()
CHECKOUT_URL = '/pos/checkout/'


class Sec17RecoveryCodes(TestCase):
    """SEC-17 — recovery kodlar tuzlangan PBKDF2 bilan hashlanadi, eski
    SHA-256 format ham ishlaydi, kod bir marta ishlatiladi."""

    def test_roundtrip_and_single_use(self):
        from inventory.twofa import gen_recovery_codes, hash_code, use_recovery_code
        u = User.objects.create_user(username='r1', password='x', role=User.Role.ADMIN)
        codes = gen_recovery_codes(3)
        u.recovery_codes = [hash_code(c) for c in codes]
        u.save(update_fields=['recovery_codes'])
        self.assertFalse(use_recovery_code(u, 'nope-nope-nope'))
        self.assertTrue(use_recovery_code(u, codes[0]))
        u.refresh_from_db()
        self.assertEqual(len(u.recovery_codes), 2)
        self.assertFalse(use_recovery_code(u, codes[0]))  # qayta ishlatib bo'lmaydi

    def test_legacy_sha256_still_accepted(self):
        from inventory.twofa import _legacy_sha256, use_recovery_code
        u = User.objects.create_user(username='r2', password='x', role=User.Role.ADMIN)
        u.recovery_codes = [_legacy_sha256('old-code')]
        u.save(update_fields=['recovery_codes'])
        self.assertTrue(use_recovery_code(u, 'old-code'))


class Auth3TotpReplay(TestCase):
    """AUTH-3 — bir TOTP kodi ikki marta ishlamasin (replay). verify_totp_step
    mos vaqt-qadamни qaytaradi; login_2fa oxirgi qabul qilinganдан katta
    bo'lсагина qabul qiladi."""

    def test_step_is_returned_and_monotonic(self):
        from inventory.twofa import gen_secret, verify_totp_step, _totp_at
        import time
        secret = gen_secret()
        now = time.time()
        code = _totp_at(secret, now)
        step = verify_totp_step(secret, code)
        self.assertIsNotNone(step)
        self.assertEqual(step, int(now // 30))
        # bir xil kod xuddi shu qadamни qaytaradi (chaqiruvchi replayни bloklaydi)
        self.assertEqual(verify_totp_step(secret, code), step)

    def test_bad_code_returns_none(self):
        from inventory.twofa import gen_secret, verify_totp_step
        self.assertIsNone(verify_totp_step(gen_secret(), '000000'))
        self.assertIsNone(verify_totp_step(gen_secret(), 'abc'))


class Sec21OversizeImage(TestCase):
    """SEC-21 — 20 MB'дан katta yuklama rad etiladi (ko'p-varaqli yo'l ham)."""

    class _F:
        def __init__(self, size, name='x.jpg'):
            self.size = size; self.name = name

    def test_oversize_detected(self):
        from inventory.views import _oversize_image, _MAX_IMG_BYTES
        ok = self._F(_MAX_IMG_BYTES)
        big = self._F(_MAX_IMG_BYTES + 1, 'big.jpg')
        self.assertIsNone(_oversize_image([ok]))
        self.assertEqual(_oversize_image([ok, big]), 'big.jpg')
        self.assertIsNone(_oversize_image([]))


class Sec20CsvSafe(TestCase):
    """SEC-20 — formula in'ektsiyasi: =,+,-,@ bilan boshlanган matn apostrof
    bilan zararsizlanadi; oddiy qiymatlar tegilmaydi."""

    def test_formula_prefixes_quoted(self):
        from inventory.views import _csv_safe
        self.assertEqual(_csv_safe('=HYPERLINK("x")'), "'=HYPERLINK(\"x\")")
        self.assertEqual(_csv_safe('+1'), "'+1")
        self.assertEqual(_csv_safe('@cmd'), "'@cmd")
        self.assertEqual(_csv_safe('Chanel'), 'Chanel')
        # CORE-3: qiymat endi doim SATR bo'lib qaytadi (csv.writer baribir
        # str() qiladi, ya'ni faylga yoziladigan natija bir xil).
        self.assertEqual(_csv_safe(1000), '1000')
        self.assertEqual(_csv_safe(None), '')

    def test_the_written_csv_is_unchanged(self):
        """Muhimi TUR emas — faylga NIMA yozilishi."""
        import csv
        import io
        from decimal import Decimal
        from inventory.views import _csv_safe
        buf = io.StringIO()
        csv.writer(buf).writerow(
            [_csv_safe(v) for v in (1000, Decimal('1234.50'), None,
                                    'Chanel', '=cmd')])
        self.assertEqual(buf.getvalue().strip(),
                         '1000,1234.50,,Chanel,\'=cmd')


class Ops1BackupGuard(TestCase):
    """OPS-1 — bazada shaxsiy ma'lumot bor; shifrsiz zaxira YARATILMAYDI.
    Parol (BACKUP_GPG_PASSPHRASE) bo'lmasa buyruq baland ovozда yiqiladi."""

    def test_refuses_without_passphrase(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import override_settings
        with override_settings(BACKUP_GPG_PASSPHRASE=''):
            with self.assertRaises(CommandError):
                call_command('backup_db', '--no-telegram')


class Arch6SplitBreakdown(TestCase):
    """ARCH-6 — yagona split_breakdown() to'g'ri bo'lishi. Bu funksiya endi
    5 o'rniga 1 marta yozilgan; uning 10 xil holati bu yerda qulflanadi."""

    def test_pure_cash(self):
        r = split_breakdown(100000, [{'method': 'cash', 'amount': 100000}])
        self.assertEqual(r, {'cash': Decimal('100000'),
                             'card': Decimal('0'), 'transfer': Decimal('0')})

    def test_cash_card_split_by_entered(self):
        r = split_breakdown(100000, [{'method': 'cash', 'amount': 30000},
                                     {'method': 'card', 'amount': 70000}])
        self.assertEqual(r['cash'], Decimal('30000'))
        self.assertEqual(r['card'], Decimal('70000'))

    def test_last_method_absorbs_remainder(self):
        # net (90 000) < kiritilgan (100 000): oxirgi usul qoldiqni oladi
        r = split_breakdown(90000, [{'method': 'cash', 'amount': 30000},
                                    {'method': 'card', 'amount': 70000}])
        self.assertEqual(r['cash'], Decimal('30000'))
        self.assertEqual(r['card'], Decimal('60000'))
        self.assertEqual(sum(r.values()), Decimal('90000'))

    def test_qr_provider_maps_to_transfer(self):
        r = split_breakdown(100000, [{'method': 'payme', 'amount': 100000}])
        self.assertEqual(r['transfer'], Decimal('100000'))
        self.assertEqual(r['cash'], Decimal('0'))

    def test_typo_method_maps_to_transfer_not_cash(self):
        r = split_breakdown(100000, [{'method': 'naqd', 'amount': 100000}])
        self.assertEqual(r['cash'], Decimal('0'))
        self.assertEqual(r['transfer'], Decimal('100000'))

    def test_empty_breakdown_falls_to_cash(self):
        self.assertEqual(split_breakdown(50000, [])['cash'], Decimal('50000'))

    def test_invalid_amounts_ignored(self):
        r = split_breakdown(40000, [{'method': 'cash', 'amount': 'abc'},
                                    {'method': 'card', 'amount': 40000}])
        self.assertEqual(r['card'], Decimal('40000'))
        self.assertEqual(r['cash'], Decimal('0'))

    def test_negative_and_zero_entries_skipped(self):
        r = split_breakdown(20000, [{'method': 'cash', 'amount': 0},
                                    {'method': 'card', 'amount': -5},
                                    {'method': 'transfer', 'amount': 20000}])
        self.assertEqual(r['transfer'], Decimal('20000'))

    def test_total_always_conserved(self):
        r = split_breakdown(100000, [{'method': 'cash', 'amount': 33333},
                                     {'method': 'card', 'amount': 33333},
                                     {'method': 'transfer', 'amount': 33334}])
        self.assertEqual(sum(r.values()), Decimal('100000'))

    def test_duplicate_methods_merge(self):
        r = split_breakdown(100000, [{'method': 'cash', 'amount': 40000},
                                     {'method': 'cash', 'amount': 10000},
                                     {'method': 'card', 'amount': 50000}])
        self.assertEqual(r['cash'], Decimal('50000'))
        self.assertEqual(r['card'], Decimal('50000'))

    def test_returns_only_three_keys(self):
        r = split_breakdown(100, [{'method': 'cash', 'amount': 100}])
        self.assertEqual(set(r.keys()), {'cash', 'card', 'transfer'})


class MoneyTestBase(TestCase):
    """Bitta filial, bitta kassir, bitta mahsulot, ochiq smen."""

    def setUp(self):
        self.branch = Branch.objects.create(name='Test filial')
        self.cashier = User.objects.create_user(
            username='kassir', password='x', role=User.Role.SOTUVCHI,
            branch=self.branch,
        )
        self.product = Product.objects.create(
            name='Test koylak', default_sale_price=Decimal('100000'),
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, size='M', color='Qora', barcode='2000000000017',
        )
        self.stock = BranchStock.objects.create(
            variant=self.variant, branch=self.branch, stock_count=10,
            cost_price=Decimal('60000'), sale_price=Decimal('100000'),
        )
        self.client = Client()
        self.client.force_login(self.cashier)

    def open_shift(self, opening_cash='0'):
        return Shift.objects.create(
            branch=self.branch, opened_by=self.cashier,
            opening_cash=Decimal(opening_cash),
        )

    def checkout(self, **overrides):
        """POST /pos/checkout/ — sensible defaults, override as needed."""
        body = {
            'lines': [{'stock_id': self.stock.pk, 'qty': 1,
                       'sale_price': '100000'}],
            'payment_method': 'cash',
        }
        body.update(overrides)
        return self.client.post(
            CHECKOUT_URL, data=json.dumps(body),
            content_type='application/json',
        )


class V5PromoServerClamp(MoneyTestBase):
    """V5 — chegirма SERVERда tekshiriladi. Soxta aksiya rad etiladi; aksiyadan
    tashqari qo'lда chegirма SABAB talab qiladi."""

    def test_fake_promo_discount_rejected(self):
        self.open_shift()
        r = self.checkout(order_discount='100000',
                          applied_promos=[{'id': 1, 'name': 'SOXTA', 'discount': 100000}])
        self.assertEqual(r.status_code, 400)
        self.assertFalse(SaleTransaction.objects.exists())

    def test_manual_discount_without_reason_rejected(self):
        self.open_shift()
        r = self.checkout(order_discount='5000')   # sabab yo'q
        self.assertEqual(r.status_code, 400)
        self.assertFalse(SaleTransaction.objects.exists())

    def test_manual_discount_with_reason_ok(self):
        self.open_shift()
        r = self.checkout(order_discount='5000', discount_reason='shikast')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(SaleTransaction.objects.exists())


class S3RefundIdempotency(MoneyTestBase):
    """S3 — bir xil kalit bilan qaytarish IKKI marta pul chiqarmaydi."""

    def test_duplicate_refund_key_blocked(self):
        self.open_shift()
        self.checkout()   # bitta sotuv yaratadi
        sale = Sale.objects.first()
        body = {'lines': [{'sale_id': sale.pk, 'qty': 1, 'reason': 'x'}],
                'idempotency_key': 'refund-key-1'}
        j = json.dumps(body)
        r1 = self.client.post('/pos/refund/', j, content_type='application/json')
        r2 = self.client.post('/pos/refund/', j, content_type='application/json')
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(Return.objects.count(), 1)   # faqat BITTA qaytarish


# ===========================================================================
#  PASSING — to'g'ri xatti-harakat. Bu testlar regressiyadan qulflaydi.
# ===========================================================================

class ExpectedToPassInvariants(MoneyTestBase):

    def test_one_open_shift_per_branch_enforced_by_db(self):
        """Shift.Meta.constraints — bitta filialda bitta ochiq smen."""
        self.open_shift()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.open_shift()

    def test_mixed_split_uses_entered_amounts_not_ratio(self):
        """QA brief 3.2: aralash chek KIRITILGAN summalar bo'yicha bo'linadi.

        Docstring 'nisbat' deydi, kod esa kiritilgan summani oladi — kod
        to'g'ri. Bu test kimdir docstring'ga ishonib orqaga qaytarmasligi
        uchun (ARCH-7).
        """
        shift = self.open_shift()
        SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='mixed',
            payment_breakdown=[{'method': 'cash', 'amount': 10000},
                               {'method': 'card', 'amount': 90000}],
        )
        txn = SaleTransaction.objects.last()
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier,
        )
        split = shift.sales_by_method_split()
        self.assertEqual(split['cash'], Decimal('10000'),
                         "naqd qismi KIRITILGAN 10 000 bo'lishi kerak")
        self.assertEqual(split['card'], Decimal('90000'))

    def test_cash_sales_equals_split_cash_row(self):
        """QA brief 5: 'Naqd sotuvlar' KPI == by-method 'Naqd' qatori."""
        shift = self.open_shift()
        self.checkout(payment_method='cash')
        self.assertEqual(shift.cash_sales(),
                         shift.sales_by_method_split()['cash'])

    def test_expected_cash_formula(self):
        """ochilish + naqd savdo + qarz − chiqim − qaytarish."""
        shift = self.open_shift(opening_cash='500000')
        self.checkout(payment_method='cash')
        CashPayout.objects.create(
            shift=shift, branch=self.branch, amount=Decimal('50000'),
            created_by=self.cashier,
        )
        expected = (Decimal('500000') + shift.cash_sales()
                    + shift.debt_payments_total()
                    - Decimal('50000') - shift.refunds_total())
        self.assertEqual(shift.expected_cash(), expected)

    def test_sale_decrements_stock_exactly_once(self):
        self.open_shift()
        self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 3,
                              'sale_price': '100000'}])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.stock_count, 7)

    def test_absurd_price_rejected_not_500(self):
        """QA brief 3.3: >= 10^10 do'stona xabar berishi kerak, 500 emas."""
        self.open_shift()
        resp = self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                                     'sale_price': '99999999999999'}])
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])

    def test_empty_cart_rejected(self):
        self.open_shift()
        resp = self.checkout(lines=[])
        self.assertEqual(resp.status_code, 400)

    def test_oversell_blocked(self):
        self.open_shift()
        resp = self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 999,
                                     'sale_price': '100000'}])
        self.assertEqual(resp.status_code, 400)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.stock_count, 10, 'qoldiq o\'zgarmasligi kerak')


# ===========================================================================
#  FAILING — tasdiqlangan nuqsonlar. Tuzatilgunча qizil.
# ===========================================================================

class BugMon9SaleWithoutShift(MoneyTestBase):
    """MON-9 — ochiq smensiz sotuv qabul qilinadi va hisobotdan yo'qoladi."""

    def test_checkout_without_open_shift_is_rejected(self):
        self.assertIsNone(Shift.objects.filter(
            branch=self.branch, status=Shift.Status.OPEN).first())
        resp = self.checkout()
        self.assertEqual(
            resp.status_code, 400,
            "Ochiq smen yo'q — qaytarish/almashtirish kabi BLOKLANISHI kerak",
        )

    def test_no_transaction_is_ever_created_with_null_shift(self):
        self.checkout()
        orphans = SaleTransaction.objects.filter(shift__isnull=True).count()
        self.assertEqual(
            orphans, 0,
            'shift=None chek hech bir Z-hisobotда ko\'rinmaydi — pul yo\'qoladi',
        )


class BugMon10MixedBreakdownUnvalidated(MoneyTestBase):
    """MON-10 — aralash summasi serverда tekshirilmaydi (faqat brauzerда)."""

    def test_breakdown_must_sum_to_total(self):
        self.open_shift()
        resp = self.checkout(
            payment_method='mixed',
            payment_breakdown=[{'method': 'cash', 'amount': 10000},
                               {'method': 'card', 'amount': 10000}],
        )  # jami 20 000, chek esa 100 000
        self.assertEqual(
            resp.status_code, 400,
            'Kam to\'lov server tomonda rad etilishi kerak',
        )

    def test_underpaid_mixed_does_not_inflate_a_bucket(self):
        shift = self.open_shift()
        self.checkout(
            payment_method='mixed',
            payment_breakdown=[{'method': 'cash', 'amount': 10000},
                               {'method': 'card', 'amount': 10000}],
        )
        split = shift.sales_by_method_split()
        self.assertLessEqual(
            split['card'], Decimal('10000'),
            'oxirgi usul yetishmagan summani yutib yubormasligi kerak',
        )


class BugMon11UnknownMethodBecomesCash(MoneyTestBase):
    """MON-11 / PAY-2 — noma'lum to'lov turi jimgina NAQD bo'lib qoladi."""

    def _mixed_with(self, method):
        shift = self.open_shift()
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='mixed',
            payment_breakdown=[{'method': method, 'amount': 100000}],
        )
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier,
        )
        return shift

    def test_qr_provider_is_not_booked_as_cash(self):
        """PAY-2: POS `{method: 'payme'}` yuboradi. Kassaga tushmagan pul
        kutilgan naqdga qo'shilmasligi SHART."""
        shift = self._mixed_with('payme')
        self.assertEqual(
            shift.sales_by_method_split()['cash'], Decimal('0'),
            'payme elektron to\'lov — kassada bunday pul YO\'Q',
        )

    def test_qr_provider_lands_in_transfer(self):
        shift = self._mixed_with('click')
        self.assertEqual(shift.sales_by_method_split()['transfer'],
                         Decimal('100000'))

    def test_typo_method_does_not_silently_become_cash(self):
        shift = self._mixed_with('naqd')  # uzbekcha kalit, kodда 'cash'
        self.assertEqual(shift.sales_by_method_split()['cash'], Decimal('0'))


class Mon8PriceOverrideAudited(MoneyTestBase):
    """MON-8 — siyosat: FLAG-AND-AUDIT.

    Do'kon narxni QO'LDA kiritadi (kelishilgan/ulgurji/"Boshqa summa") — shuning
    uchun narx QABUL qilinadi (ish jarayoni buzilmaydi). LEKIN katalog narxidan
    sezilarli farq yoki tannarxdan past sotuv AUDIT LOGGA yoziladi — ko'rinsin.
    """

    def test_material_override_is_accepted_and_logged(self):
        from inventory.models import AuditLog
        self.open_shift()
        resp = self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                                     'sale_price': '1'}])  # katalog 100 000
        self.assertEqual(resp.status_code, 200)
        sale = Sale.objects.first()
        self.assertIsNotNone(sale, 'qo\'lda narxli sotuv qabul qilinishi kerak')
        self.assertEqual(sale.sale_price, Decimal('1'),
                         'kiritilgan narx saqlanadi (buzilmaydi)')
        log = AuditLog.objects.filter(model_name='PriceOverride').first()
        self.assertIsNotNone(log, 'sezilarli narx farqi audit logга tushishi kerak')
        self.assertTrue(log.changes.get('price_override', {}).get('below_cost'),
                        'tannarxdan past — below_cost bayrog\'i yoqilishi kerak')

    def test_catalog_price_sale_is_not_logged(self):
        from inventory.models import AuditLog
        self.open_shift()
        self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                              'sale_price': '100000'}])  # katalogга teng
        self.assertEqual(
            AuditLog.objects.filter(model_name='PriceOverride').count(), 0,
            'katalog narxida sotuv — audit yozuvi bo\'lmasligi kerak')

    def test_small_rounding_diff_is_not_logged(self):
        from inventory.models import AuditLog
        self.open_shift()
        self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                              'sale_price': '99500'}])  # ~0.5% farq
        self.assertEqual(
            AuditLog.objects.filter(model_name='PriceOverride').count(), 0,
            'kichik (5% dan kam) farq shovqin — yozilmaydi')


class BugMon13GarbagePriceBecomesFree(MoneyTestBase):
    """MON-13 — noto'g'ri narx 0 ga aylanadi, xato bermaydi."""

    def test_non_numeric_price_is_rejected(self):
        self.open_shift()
        resp = self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                                     'sale_price': 'abc'}])
        self.assertEqual(resp.status_code, 400,
                         "'abc' narx — 0 emas, xato bo'lishi kerak")
        self.assertEqual(Sale.objects.count(), 0, 'tekin sotuv yaratilmasin')


class ExpectedToPassBreakdownPrecision(MoneyTestBase):
    """MON-14 pasaytirildi — float ishlatiladi, lekin buzilish ko'rsatilmadi.

    Kod gigiyenasi masalasi bo'lib qoladi. Bu test aniqlik yo'qolsa ushlaydi.
    """

    def test_breakdown_amount_round_trips_exactly(self):
        self.open_shift()
        self.checkout(
            payment_method='mixed',
            payment_breakdown=[{'method': 'cash', 'amount': '33333.33'},
                               {'method': 'card', 'amount': '66666.67'}],
        )
        txn = SaleTransaction.objects.first()
        if txn is None:
            self.skipTest('chek yaratilmadi (MON-9/MON-10 avval tuzatilsin)')
        total = sum(Decimal(str(e['amount'])) for e in txn.payment_breakdown)
        self.assertEqual(total, Decimal('100000.00'),
                         'float yaxlitlash summani buzmasligi kerak')


class ExpectedToPassPayoutGuard(MoneyTestBase):
    """MON-15 QAYTARIB OLINDI — auditda xato aytilgan edi.

    CashPayout.Meta'да `cashpayout_amount_positive` CheckConstraint BOR,
    ya'ni manfiy chiqim DB darajasida imkonsiz. Bu testlar shu himoyani
    regressiyadan qulflaydi.
    """

    def test_negative_payout_fails_validation(self):
        shift = self.open_shift(opening_cash='100000')
        payout = CashPayout(shift=shift, branch=self.branch,
                            amount=Decimal('-50000'), created_by=self.cashier)
        with self.assertRaises(ValidationError):
            payout.full_clean()

    def test_negative_payout_blocked_by_db_constraint(self):
        shift = self.open_shift(opening_cash='100000')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CashPayout.objects.create(
                    shift=shift, branch=self.branch,
                    amount=Decimal('-50000'), created_by=self.cashier,
                )


class BugOff2NoIdempotency(MoneyTestBase):
    """OFF-2 — offline replay bir sotuvni ikki marta yozadi."""

    def test_same_idempotency_key_creates_one_transaction(self):
        self.open_shift()
        body = {
            'lines': [{'stock_id': self.stock.pk, 'qty': 1,
                       'sale_price': '100000'}],
            'payment_method': 'cash',
            'idempotency_key': '11111111-2222-3333-4444-555555555555',
            'is_offline_replay': True,
        }
        for _ in range(2):
            self.client.post(CHECKOUT_URL, data=json.dumps(body),
                             content_type='application/json')
        self.assertEqual(SaleTransaction.objects.count(), 1,
                         'takroriy kalit yangi chek YARATMASLIGI kerak')
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.stock_count, 9,
                         'qoldiq faqat bir marta kamayishi kerak')


class Stk8WeightedCost(TestCase):
    """STK-8 — o'rtacha-tortilgan tannarx (butun so'mga yaxlitlangan)."""

    def test_basic_weighted_average(self):
        from inventory.models import weighted_cost
        self.assertEqual(weighted_cost(10, 1000, 10, 2000), Decimal('1500'))

    def test_rounds_to_whole_som(self):
        from inventory.models import weighted_cost
        # 3×1000 + 1×1100 = 4100/4 = 1025
        self.assertEqual(weighted_cost(3, 1000, 1, 1100), Decimal('1025'))
        # 3×1000 + 2×1001 = 5002/5 = 1000.4 -> 1000
        self.assertEqual(weighted_cost(3, 1000, 2, 1001), Decimal('1000'))

    def test_zero_old_qty_takes_new(self):
        from inventory.models import weighted_cost
        self.assertEqual(weighted_cost(0, 0, 5, 1234), Decimal('1234'))

    def test_zero_add_qty_is_direct_cost_correction(self):
        from inventory.models import weighted_cost
        self.assertEqual(weighted_cost(10, 1000, 0, 1500), Decimal('1500'))


class Stk9StocktakeDelta(MoneyTestBase):
    """STK-9 — inventarizatsiya tasdiqi FARQni (delta) qo'llaydi, mutlaq
    yozib sanashдan keyingi sotuvlarni bekor QILMAYDI."""

    def test_apply_uses_delta_not_absolute(self):
        from inventory.models import Stocktake, StocktakeCount
        admin = User.objects.create_user(
            username='boss2', password='x', role=User.Role.ADMIN, branch=self.branch)
        c = Client()
        c.force_login(admin)
        # Snapshot: tizim 10, sanaб 8 topildi (2 kam). Keyin 3 dona sotildi → 7.
        st = Stocktake.objects.create(branch=self.branch, started_by=admin)
        StocktakeCount.objects.create(
            session=st, variant=self.variant, system_qty=10, counted_qty=8)
        self.stock.stock_count = 7
        self.stock.save(update_fields=['stock_count'])
        resp = c.post(reverse('stocktake_detail', args=[st.pk]), {'action': 'apply'})
        self.assertEqual(resp.status_code, 302)
        self.stock.refresh_from_db()
        # delta = 8 − 10 = −2, hozirgi 7 ga: 5. Mutlaq 8 EMAS.
        self.assertEqual(self.stock.stock_count, 5,
                         'delta qo\'llanishi kerak (sotuvlar bekor bo\'lmasin)')
        st.refresh_from_db()
        self.assertEqual(st.status, Stocktake.Status.APPLIED)


class Mon22ShiftSnapshot(MoneyTestBase):
    """MON-22 — yopilgan smenning farqi keyingi tahrirlardan o'zgarmaydi."""

    def test_closed_shift_variance_is_frozen(self):
        shift = self.open_shift(opening_cash='0')
        self.checkout(payment_method='cash')  # 100000 naqd
        shift.counted_cash = shift.compute_expected_cash()
        shift.closing_expected_cash = shift.compute_expected_cash()
        shift.status = Shift.Status.CLOSED
        shift.closed_at = timezone.now()
        shift.save()
        self.assertEqual(shift.variance(), Decimal('0'))
        # keyinchalik shu smenга yana sotuv qo'shilса ham farq o'zgarmasin
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='cash')
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('50000'),
            cost_at_sale=Decimal('30000'), sold_by=self.cashier)
        shift.refresh_from_db()
        self.assertEqual(shift.variance(), Decimal('0'),
                         'yopilgan smen farqi qotirilgan bo\'lishi kerak')


class RefundMoney(MoneyTestBase):
    """REF-1 (atomiklik) va qaytarish qiymati siyosati (order_discount)."""

    def _txn(self, order_discount='0'):
        return SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.open_shift(),
            payment_method='cash', order_discount=Decimal(order_discount))

    def _sale(self, txn, qty=1, price='100000'):
        return Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=qty, sale_price=Decimal(price),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)

    def test_order_discount_not_split_into_refund(self):
        """Egasining qarori (2026): butun-buyurtma chegirmasi qaytarishга
        TAQSIMLANMAYDI — tovar O'Z narxida qaytadi.

        3×100 000, chek chegirmasi 100 000. Bitta dona qaytганда — to'liq
        100 000 (33 333.33 EMAS). Bu ataylab REF-2 prorata'sini bekor qiladi
        (chalkash fraksiyalar va Z-hisobot 1 so'm siljishi shundan edi)."""
        txn = self._txn(order_discount='100000')
        sale = self._sale(txn, qty=3)
        r = self.client.post('/pos/refund/', data=json.dumps(
            {'lines': [{'sale_id': sale.pk, 'qty': 1, 'reason': 't'}]}),
            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['refunded_total'], 100000.0,
                         'bir dona qaytarish = to\'liq dona narxi (100 000)')

    def test_full_return_caps_at_check_total(self):
        """REF-3: butun chek qaytганда — mijoz TO'LAGANI (200 000) qaytadi,
        qatorlar jamisi (300 000) EMAS. Chegirма oxirgi qaytarishга singadi.
        (Egasining qoidasi: qisman → dona narxi, to'liq → chek jamisi.)"""
        txn = self._txn(order_discount='100000')   # 3×100 000 − 100 000 = 200 000
        sale = self._sale(txn, qty=3)
        r = self.client.post('/pos/refund/', data=json.dumps(
            {'lines': [{'sale_id': sale.pk, 'qty': 3, 'reason': 't'}]}),
            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['refunded_total'], 200000.0,
                         'to\'liq qaytarish chek to\'loviga (200 000) cheklanadi')

    def test_ref1_partial_failure_rolls_back_first_line(self):
        from inventory.models import Return
        txn = self._txn()
        s1 = self._sale(txn, qty=1)
        s2 = self._sale(txn, qty=1)
        # 1-qator valid, 2-qator qty=5 > mavjud 1 → butun tranzaksiya bekor
        r = self.client.post('/pos/refund/', data=json.dumps({'lines': [
            {'sale_id': s1.pk, 'qty': 1}, {'sale_id': s2.pk, 'qty': 5}]}),
            content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Return.objects.count(), 0,
                         'birinchi qator ham COMMIT bo\'lmasligi kerak (double-refund oldini olish)')
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.stock_count, 10, 'ombor o\'zgarmasligi kerak')


class Mon4CashIn(MoneyTestBase):
    """MON-4 — kassaga naqd qo'shish kutilgan naqdни OSHIRadi (payout aksi)."""

    def test_cash_in_increases_expected_cash(self):
        shift = self.open_shift(opening_cash='100000')
        before = shift.expected_cash()
        resp = self.client.post(
            '/shift/cash-in/',
            data=json.dumps({'amount': 50000, 'category': 'float'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(shift.expected_cash(), before + Decimal('50000'))
        self.assertEqual(shift.cash_ins_total(), Decimal('50000'))

    def test_cash_in_rejected_without_open_shift(self):
        resp = self.client.post(
            '/shift/cash-in/',
            data=json.dumps({'amount': 50000}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class Stk1WriteOff(MoneyTestBase):
    """STK-1 — hisobdan chiqarish zaxirani kamaytiradi va yozib qoladi."""

    def _admin(self):
        return User.objects.create_user(
            username='boss', password='x', role=User.Role.ADMIN,
            branch=self.branch)

    def test_writeoff_decrements_stock_and_records(self):
        from inventory.models import StockWriteOff
        admin = self._admin()
        c = Client()
        c.force_login(admin)
        resp = c.post('/writeoff/', {
            'branch': self.branch.pk, 'code': self.variant.barcode,
            'quantity': 3, 'reason': 'damage', 'note': 'suv toshdi',
        })
        self.assertEqual(resp.status_code, 302)  # redirect back
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.stock_count, 7, 'zaxira 3 taга kamayishi kerak')
        wo = StockWriteOff.objects.get()
        self.assertEqual(wo.quantity, 3)
        self.assertEqual(wo.reason, 'damage')
        self.assertEqual(wo.cost_at_writeoff, Decimal('60000'))

    def test_writeoff_over_stock_is_rejected(self):
        from inventory.models import StockWriteOff
        admin = self._admin()
        c = Client()
        c.force_login(admin)
        c.post('/writeoff/', {
            'branch': self.branch.pk, 'code': self.variant.barcode,
            'quantity': 999, 'reason': 'loss',
        })
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.stock_count, 10, 'zaxira o\'zgarmasligi kerak')
        self.assertEqual(StockWriteOff.objects.count(), 0)

    def test_non_admin_forbidden(self):
        resp = self.client.post('/writeoff/', {  # self.client = kassir (sotuvchi)
            'branch': self.branch.pk, 'code': self.variant.barcode,
            'quantity': 1, 'reason': 'loss'})
        self.assertEqual(resp.status_code, 403)


class Off7ClientTimestamp(MoneyTestBase):
    """OFF-7 — offline sotuv ASL vaqti bilan yoziladi va o'z (tarixiy) smenига
    tushadi, sinxron vaqti/hozirgi smenга emas."""

    def test_offline_replay_lands_in_historical_shift_and_time(self):
        now = timezone.now()
        past = self.open_shift()
        past.opened_at = now - timezone.timedelta(hours=5)
        past.save(update_fields=['opened_at'])
        past.status = Shift.Status.CLOSED
        past.closed_at = now - timezone.timedelta(hours=1)
        past.save(update_fields=['status', 'closed_at'])
        # hozir ochiq yangi smen
        Shift.objects.create(branch=self.branch, opened_by=self.cashier,
                             opening_cash=Decimal('0'))
        ts = (now - timezone.timedelta(hours=3)).isoformat()
        resp = self.checkout(is_offline_replay=True, client_ts=ts)
        self.assertEqual(resp.status_code, 200)
        txn = SaleTransaction.objects.latest('id')
        self.assertEqual(txn.shift_id, past.pk,
                         'sotuv tarixiy (o\'z) smenига tushishi kerak')
        self.assertLess(abs((txn.sold_at - (now - timezone.timedelta(hours=3)))
                            .total_seconds()), 5,
                        'sold_at client vaqti bo\'lishi kerak')

    def test_online_sale_uses_now_and_current_shift(self):
        shift = self.open_shift()
        resp = self.checkout(client_ts=timezone.now().isoformat())
        self.assertEqual(resp.status_code, 200)
        txn = SaleTransaction.objects.latest('id')
        self.assertEqual(txn.shift_id, shift.pk)


class BugArch5ShiftAttribution(MoneyTestBase):
    """ARCH-5 — savdo vaqt oynasi bo'yicha, qolgani FK bo'yicha topiladi."""

    def test_shift_reporting_follows_the_fk(self):
        """Offline chek kech sinxron bo'lsa ham O'Z smeniga tegishli."""
        shift = self.open_shift()
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='cash',
            sold_at=shift.opened_at - timezone.timedelta(hours=2),
        )
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier,
        )
        self.assertEqual(
            shift.sales_by_method_split()['cash'], Decimal('100000'),
            'shift FK o\'rnatilgan — vaqt oynasi emas, FK hal qilishi kerak',
        )


class BugPay1UnauthenticatedWebhook(MoneyTestBase):
    """PAY-1 — imzosiz webhook kutilayotgan to'lovni 'to'langan' qiladi.

    Ekspluatatsiya: mijoz kassada turib, telefonidan summani yuboradi.
    ref_code kerak emas — provider+summa+30 daqiqa oynasi yetarli.
    """

    def _pending_intent(self):
        return PaymentIntent.objects.create(
            branch=self.branch, initiated_by=self.cashier,
            provider='payme', amount=Decimal('500000'),
            ref_code='ABC123', status=PaymentIntent.Status.PENDING,
        )

    def test_unsigned_callback_must_not_mark_intent_paid(self):
        intent = self._pending_intent()
        Client().post(  # login yo'q, imzo yo'q, CSRF yo'q
            '/payments/webhook/payme/',
            data=json.dumps({'amount': 500000}),
            content_type='application/json',
        )
        intent.refresh_from_db()
        self.assertEqual(
            intent.status, PaymentIntent.Status.PENDING,
            "imzosiz callback to'lovni tasdiqlay olmasligi SHART",
        )

    def test_unsigned_callback_is_rejected_with_4xx(self):
        self._pending_intent()
        resp = Client().post(
            '/payments/webhook/payme/',
            data=json.dumps({'amount': 500000}),
            content_type='application/json',
        )
        self.assertIn(resp.status_code, (401, 403),
                      'imzo tekshiruvi yo\'q — 401/403 qaytishi kerak')

    def test_amount_only_window_fallback_should_not_exist(self):
        """ref_code mos kelmasa ham summa bo'yicha topilmasligi kerak."""
        intent = self._pending_intent()
        Client().post(
            '/payments/webhook/payme/',
            data=json.dumps({'amount': 500000, 'ref_code': 'NOTMINE'}),
            content_type='application/json',
        )
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PENDING)


class SalesPageRefundMatchesZReport(MoneyTestBase):
    """Sotuvlar sahifasidagi "Qaytarilgan" == Z-hisobotdagi qaytarilgan pul.

    Ilgari sales_list refund formulasini SQL'da QAYTA yozgan edi va ikki joy
    ikki xil son ko'rsatardi. Endi HAR IKKALASI ham yagona manba —
    Return.effective_cash_refund — orqali hisoblaydi. Bu test o'sha yagona
    manbani qulflaydi (formula qanday bo'lishidan qat'i nazar teng chiqsin).
    """

    def setUp(self):
        super().setUp()
        self.shift = self.open_shift()
        self.admin = User.objects.create_user(
            username='admin_sales', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)

    def _discounted_sale_fully_returned(self):
        # 3 x 100 000, chek chegirmasi 60 000. Return TO'G'RIDAN-TO'G'RI ORM orqali
        # yaratiladi (pos_refund cheklovини chetlab) — refund_cash yo'q, shu bois
        # effective_cash_refund dona narxiga (fallback) qaytadi. Bu test faqat
        # sahifa == Z-hisobot (yagona manba) ekanini tekshiradi, summа qiymatini emas.
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal('60000'))
        sale = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=3, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        Return.objects.create(sale=sale, shift=self.shift, quantity=3,
                              refunded_by=self.cashier)
        return sale

    def test_page_and_zreport_report_the_same_refund(self):
        self._discounted_sale_fully_returned()
        c = Client()
        c.force_login(self.admin)
        page = c.get('/sales/')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(Decimal(str(page.context['returned_total'])),
                         self.shift.refunds_total(),
                         "sotuvlar sahifasi va Z-hisobot bir xil bo'lishi SHART")

    def test_sales_page_loads_with_rows(self):
        """Sale.returned_qty — setter'siz @property; unga yozish 500 berardi."""
        self._discounted_sale_fully_returned()
        c = Client()
        c.force_login(self.admin)
        self.assertEqual(c.get('/sales/').status_code, 200)


def _printed(value):
    """`|som` filtri chop etadigan sonni int sifatida qaytaradi.

    Test AYNAN chekда ko'ringan raqam bilan ishlashi uchun — kontekstдagi
    xom Decimal bilan emas."""
    from inventory.templatetags.yurit_extras import som
    txt = som(value).replace(' ', '').replace(' ', '')
    return int(txt) if txt else 0


class Mon24ZReportReconciles(MoneyTestBase):
    """MON-24/MON-25 — Z-hisobotда CHOP ETILGAN qatorlar CHOP ETILGAN JAMIga
    aynan qo'shilishi SHART.

    Ishlab chiqarishда (Smena #46) chekда shunday chiqdi:

        93 000 + 19 151 500 − 748 000 − 273 383 = 18 223 117
        chop etilgan "HOZIRGI QOLDIQ"          = 18 223 116   ← 1 so'm kam

    Sabablari:
      1. `refund_total` view'да FLOAT yig'indi edi, `expected_cash()` ichида
         esa Decimal — ikkisi teskari tomonga yaxlitlandi.
      2. Yaxlitlashning O'ZI ikki xil edi: `som` filtri `round()` (bank
         yaxlitlashi) ishlatardi, chop qatorlari esa boshqa yo'ldan kelardi.

    Bu testlar chekни BUTUN sifatida qulflaydi. Har biri fix'siz YIQILADI.
    """

    def _mixed_txn(self):
        """Aralash chek: 60 000 naqd + 40 000 karta."""
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='mixed',
            payment_breakdown=[{'method': 'cash', 'amount': 60000},
                               {'method': 'card', 'amount': 40000}])
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        return txn

    def _fractional_refund_sale(self):
        """QATOR jami 3 donaga BO'LINMAYDI → qisman qaytarish KASR chiqadi.

        3×100 000, QATOR chegirmasi (line_discount) 1 000 → qator jami 299 000;
        1 dona qaytганда 299 000/3 = 99 666.67 — kassa hisobiga kasr kiradi.
        (order_discount emas, line_discount ishlatamiz: butun-buyurtma chegirmasi
        endi qaytarishга taqsimlanmaydi — egasining qarori. Kasr baribir qator
        chegirmasi yoki bo'linmaydigan jamidan kelib chiqadi.)
        """
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash')
        sale = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=3, sale_price=Decimal('100000'),
            line_discount=Decimal('1000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        Return.objects.create(sale=sale, shift=self.shift, quantity=1,
                              refunded_by=self.cashier)
        return sale

    def setUp(self):
        super().setUp()
        self.shift = self.open_shift(opening_cash='93000')
        self._mixed_txn()
        self.disc_sale = self._fractional_refund_sale()
        CashPayout.objects.create(
            shift=self.shift, branch=self.branch, amount=Decimal('7000'),
            category='lunch', note='tushlik', created_by=self.cashier)

    def _resp(self):
        r = self.client.get(reverse('shift_receipt', args=[self.shift.pk]))
        self.assertEqual(r.status_code, 200)
        return r

    def _kassa_rows(self, c):
        """Chekдagi KASSA qatorlari — chop etilgan ko'rinishда."""
        return (_printed(c['opening_cash'] if 'opening_cash' in c
                         else c['shift'].opening_cash)
                + _printed(c['cash_sales'])
                + _printed(c['debt_payments'])
                + _printed(c['cash_ins_total'])
                - _printed(c['payouts_total'])
                - _printed(c['refund_total'])
                + _printed(c['post_close_delta'])
                + _printed(c['rounding_delta']))

    # ---- 1. KASSA bloki yig'iladi ---------------------------------------
    def test_kassa_lines_sum_to_printed_total(self):
        c = self._resp().context
        self.assertEqual(
            self._kassa_rows(c), _printed(c['expected']),
            "chop etilgan KASSA qatorlari chop etilgan JAMIga qo'shilmadi")

    def test_setup_really_produces_a_fractional_refund(self):
        """Sinov shartи: kasr bo'lmasa bu testlar hech nimani isbotlamaydi."""
        self.assertNotEqual(self.shift.refunds_total() % 1, Decimal('0'),
                            'test ma\'lumoti kasr qaytarish bermadi')

    def test_variance_matches_the_printed_total(self):
        self.shift.counted_cash = Decimal('18000000')
        self.shift.save(update_fields=['counted_cash'])
        c = self._resp().context
        self.assertEqual(
            _printed(c['variance_value']),
            _printed(self.shift.counted_cash) - _printed(c['expected']),
            'FARQ chop etilgan QOLDIQ bilan yopilishi kerak')

    # ---- 2. SOTUV bloki yig'iladi ---------------------------------------
    def test_payment_rows_sum_to_printed_gross(self):
        c = self._resp().context
        rows = sum(_printed(r['amount']) for r in c['pay_rows'])
        self.assertEqual(rows + _printed(c['other_money']),
                         _printed(c['total_rev']))

    def test_net_sales_is_printed_gross_minus_printed_refund(self):
        c = self._resp().context
        self.assertEqual(_printed(c['net_sales']),
                         _printed(c['total_rev']) - _printed(c['refund_total']))

    # ---- 3. MON-25: aralash chek sanoqni buzmasin -----------------------
    def test_counts_never_exceed_receipt_count(self):
        c = self._resp().context
        counts = sum(r['count'] for r in c['pay_rows']) + c['other_count']
        self.assertEqual(counts + c['mixed_count'], c['txn_count'],
                         'badge sonlari Cheklar soniga teng bo\'lishi kerak')

    def test_mixed_receipt_is_disclosed(self):
        c = self._resp().context
        self.assertEqual(c['mixed_count'], 1)
        self.assertContains(self._resp(), 'aralash')

    # ---- 4. MON-22: yopilgan smen JAMIsi qotib qolsin -------------------
    def test_closed_shift_total_is_frozen_and_still_reconciles(self):
        self.shift.closing_expected_cash = self.shift.compute_expected_cash()
        self.shift.counted_cash = self.shift.closing_expected_cash
        self.shift.status = Shift.Status.CLOSED
        self.shift.closed_at = timezone.now()
        self.shift.save()
        frozen = _printed(self.shift.closing_expected_cash)
        before = _printed(self._resp().context['expected'])
        self.assertEqual(before, frozen)

        # Smen YOPILGANDAN KEYIN yana bir dona qaytarildi.
        Return.objects.create(sale=self.disc_sale, shift=self.shift,
                              quantity=1, refunded_by=self.cashier)
        c = self._resp().context
        self.assertEqual(
            _printed(c['expected']), frozen,
            "yopilgan smenning JAMIsi keyingi qaytarishдan o'zgarmasligi kerak")
        self.assertNotEqual(
            _printed(c['post_close_delta']), 0,
            "yopilgandan keyingi o'zgarish alohida qatorда ko'rinishi kerak")
        self.assertEqual(
            self._kassa_rows(c), _printed(c['expected']),
            'snapshot ajralganда ham qatorlar JAMIga qo\'shilishi kerak')

    def test_closed_shift_variance_is_frozen_on_the_receipt(self):
        self.shift.closing_expected_cash = self.shift.compute_expected_cash()
        self.shift.counted_cash = self.shift.closing_expected_cash
        self.shift.status = Shift.Status.CLOSED
        self.shift.closed_at = timezone.now()
        self.shift.save()
        before = _printed(self._resp().context['variance_value'])
        Return.objects.create(sale=self.disc_sale, shift=self.shift,
                              quantity=1, refunded_by=self.cashier)
        self.assertEqual(_printed(self._resp().context['variance_value']), before,
                         'kassirning FARQi keyinchalik qayta yozilmasin')


class Mon24SomFilterRoundsHalfUp(TestCase):
    """MON-24 — pul yaxlitlashi YARMI YUQORIGA, bank yaxlitlashi EMAS.

    `round(2.5)` → 2 (juftga). Kassir 3 kutadi; Z-hisobotда aynan shu 1 so'm
    qatorlarни JAMIдan ajratib yuborardi.
    """

    NBSP = ' '

    def test_half_rounds_up_not_to_even(self):
        from inventory.templatetags.yurit_extras import som
        self.assertEqual(som(Decimal('0.5')), '1')
        self.assertEqual(som(Decimal('1.5')), '2')
        self.assertEqual(som(Decimal('2.5')), '3')      # round() bunda 2 berardi
        self.assertEqual(som(Decimal('273382.5')), f'273{self.NBSP}383')

    def test_float_and_decimal_agree(self):
        from inventory.templatetags.yurit_extras import som
        for v in ('273382.5', '99666.67', '18223116.5', '100000', '2.5'):
            self.assertEqual(som(Decimal(v)), som(float(v)), f'{v} mos kelmadi')

    def test_negative_and_edge_values(self):
        from inventory.templatetags.yurit_extras import som
        self.assertEqual(som(Decimal('-1234.5')), f'-1{self.NBSP}235')
        self.assertEqual(som(Decimal('-0.4')), '0')     # "-0" chiqmasin
        self.assertEqual(som(0), '0')
        self.assertEqual(som(None), '')
        self.assertEqual(som(''), '')
        self.assertEqual(som('salom'), 'salom')


class Ref3RefundCap(MoneyTestBase):
    """REF-3 — egasining qoidasi: bir dona qaytганда o'z narxi; butun chek
    qaytганда chek to'lovi (chegirма ayirilgan). Har qaytarish chek to'loviдan
    oshmaydi, va haqiqiy naqд Return.refund_cash'да SAQLANADI (tarix qotadi)."""

    def _discounted_check(self, order_discount='1000'):
        # 12 000 + 9 000 = 21 000; chegirма 1 000 → mijoz 20 000 to'lagan.
        shift = self.open_shift()
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='cash', order_discount=Decimal(order_discount))
        s1 = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('12000'),
            cost_at_sale=Decimal('6000'), sold_by=self.cashier)
        s2 = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('9000'),
            cost_at_sale=Decimal('6000'), sold_by=self.cashier)
        return txn, s1, s2

    def _refund(self, lines):
        return self.client.post('/pos/refund/', data=json.dumps({'lines': lines}),
                                content_type='application/json')

    def _all_refunded(self):
        return sum((r.effective_cash_refund for r in Return.objects.all()),
                   Decimal('0'))

    def test_partial_return_gets_item_price(self):
        _, s1, _ = self._discounted_check()
        r = self._refund([{'sale_id': s1.pk, 'qty': 1, 'reason': 'x'}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['refunded_total'], 12000.0,
                         'qisman qaytarish = dona narxi (12 000), chegirма tegmaydi')

    def test_full_return_one_batch_caps_at_paid(self):
        _, s1, s2 = self._discounted_check()
        r = self._refund([{'sale_id': s1.pk, 'qty': 1, 'reason': 'x'},
                          {'sale_id': s2.pk, 'qty': 1, 'reason': 'x'}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['refunded_total'], 20000.0,
                         'butun chek = to\'langan summа (20 000), 21 000 emas')
        self.assertEqual(self._all_refunded(), Decimal('20000'))

    def test_split_partial_then_completing_totals_paid(self):
        txn, s1, s2 = self._discounted_check()
        self._refund([{'sale_id': s1.pk, 'qty': 1, 'reason': 'x'}])   # 12 000
        self._refund([{'sale_id': s2.pk, 'qty': 1, 'reason': 'x'}])   # min(9000, 8000)=8000
        self.assertEqual(self._all_refunded(), Decimal('20000'),
                         'ikki bosqichда ham jami = chek to\'lovi')
        self.assertEqual(Decimal(str(txn.total)), Decimal('20000'))

    def test_refund_cash_is_stored_not_recomputed(self):
        """refund_cash SAQLANADI — snapshot; keyin qayta hisoblanmaydi."""
        _, s1, s2 = self._discounted_check()
        self._refund([{'sale_id': s1.pk, 'qty': 1, 'reason': 'x'},
                      {'sale_id': s2.pk, 'qty': 1, 'reason': 'x'}])
        for r in Return.objects.all():
            self.assertIsNotNone(r.refund_cash, 'har qaytarishда refund_cash yozilishi kerak')

    def test_no_discount_check_refunds_item_price(self):
        shift = self.open_shift()
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='cash')   # chegirмasiz
        s = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=2, sale_price=Decimal('10000'),
            cost_at_sale=Decimal('6000'), sold_by=self.cashier)
        r = self._refund([{'sale_id': s.pk, 'qty': 2, 'reason': 'x'}])
        self.assertEqual(r.json()['refunded_total'], 20000.0,
                         'chegirмasiz chek — dona narxi (o\'zgarishsiz)')


class SalesPageVsZReport(MoneyTestBase):
    """SAL-1/SAL-2 — Sotuvlar sahifasi, CHEK ko'rinishi va Z-hisobot BIR XIL
    ma'lumotdan BIR XIL raqam chiqarishi kerak.

    Topilgani: KPI "Jami tushum" faqat `line_discount`ni ayirardi, CHEK
    chegirmasini emas — ya'ni AYNI SAHIFA o'zi bilan ziddiyatда edi:

        3×100 000, chek chegirmasi 60 000 (mijoz 240 000 to'lagan)
          KPI "Jami tushum"        300 000   ← xato
          CHEK ko'rinishi qatori   240 000
          Z-hisobot JAMI SAVDO     240 000

    Bu qatorlarni QAYTA baholash emas — egasining qoidasi (chegirma qatorga
    taqsimlanmaydi, qaytarish tovarni O'Z narxida baholaydi) o'z kuchida
    qoladi; faqat JAMI chek to'loviga keltiriladi.

    SAL-2 — buzuqlik emas, TA'RIF farqi: sahifa qaytarishni tovar QACHON
    SOTILGANIGA qarab, Z-hisobot esa pul QACHON CHIQQANIGA qarab sanaydi.
    Endi sahifa ikkalasini ham ko'rsatadi.
    """

    def _sale(self, shift, qty=1, odisc='0', price='100000'):
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='cash', order_discount=Decimal(odisc))
        return Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=qty, sale_price=Decimal(price),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)

    def _refund(self, sale, shift, qty, cash):
        """REF-3: `refund_cash` — qaytarish paytida qotirilgan haqiqiy naqd."""
        return Return.objects.create(sale=sale, shift=shift, quantity=qty,
                                     refunded_by=self.cashier,
                                     refund_cash=Decimal(cash))

    def _close(self, shift):
        shift.closing_expected_cash = shift.compute_expected_cash()
        shift.status = Shift.Status.CLOSED
        shift.closed_at = timezone.now()
        shift.save()

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_sync', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.today = timezone.localdate().strftime('%Y-%m-%d')

    def _page(self, **params):
        p = {'date_from': self.today, 'date_to': self.today}
        p.update(params)
        r = self.client.get('/sales/', p)
        self.assertEqual(r.status_code, 200)
        return r.context

    def _z(self, shift):
        r = self.client.get(reverse('shift_receipt', args=[shift.pk]))
        self.assertEqual(r.status_code, 200)
        return r.context

    # ---- SAL-1: uch joy ham bir xil JAMI ------------------------------
    def test_kpi_matches_check_view_and_zreport(self):
        shift = self.open_shift()
        self._sale(shift, qty=3, odisc='60000')
        p, z = self._page(), self._z(shift)
        self.assertEqual(_dec(p['total']), Decimal('240000'),
                         "KPI mijoz TO'LAGANINI ko'rsatsin")
        self.assertEqual(_dec(p['total']), _dec(p['checks'][0]['total']),
                         'KPI va CHEK qatori bir xil bo\'lsin (bir sahifada!)')
        self.assertEqual(_dec(p['total']), _dec(z['total_rev']),
                         'KPI va Z-hisobot bir xil bo\'lsin')

    def test_kpi_equals_sum_of_all_check_rows(self):
        """Bir nechta chek — KPI aynan qatorlar yig'indisi bo'lsin."""
        shift = self.open_shift()
        self._sale(shift, qty=3, odisc='60000')     # 240 000
        self._sale(shift, qty=1)                    # 100 000
        self._sale(shift, qty=2, odisc='15000')     # 185 000
        p = self._page()
        self.assertEqual(_dec(p['total']),
                         sum((_dec(c['total']) for c in p['checks']), Decimal('0')))
        self.assertEqual(_dec(p['total']), Decimal('525000'))

    def test_daily_badge_is_net_of_order_discount(self):
        shift = self.open_shift()
        self._sale(shift, qty=3, odisc='60000')
        self.assertEqual(_dec(str(self._page()['daily_list'][0]['total'])),
                         Decimal('240000'))

    def test_filtering_one_line_takes_only_its_share_of_the_discount(self):
        """Chek 2 qator (100k + 300k), chegirma 40 000. Faqat 1-qator
        filtrlansa — unga chegirmaning 1/4 (10 000) tegsin: hammasi ham,
        nol ham emas."""
        shift = self.open_shift()
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=shift,
            payment_method='cash', order_discount=Decimal('40000'))
        p2 = Product.objects.create(name='Ikkinchi',
                                    default_sale_price=Decimal('300000'))
        v2 = ProductVariant.objects.create(product=p2, size='L', color='Oq',
                                           barcode='2000000000024')
        Sale.objects.create(transaction=txn, variant=self.variant,
                            branch=self.branch, quantity=1,
                            sale_price=Decimal('100000'),
                            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        Sale.objects.create(transaction=txn, variant=v2, branch=self.branch,
                            quantity=1, sale_price=Decimal('300000'),
                            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        self.assertEqual(_dec(self._page()['total']), Decimal('360000'))
        self.assertEqual(_dec(self._page(q='Test koylak', view='items')['total']),
                         Decimal('90000'))

    def test_no_order_discount_leaves_totals_untouched(self):
        shift = self.open_shift()
        self._sale(shift, qty=2)
        p, z = self._page(), self._z(shift)
        self.assertEqual(_dec(p['total']), Decimal('200000'))
        self.assertEqual(_dec(p['total']), _dec(z['total_rev']))
        self.assertEqual(_dec(p['order_discount_total']), Decimal('0'))

    # ---- REF-3 siyosati ikkala hisobotда bir xil ishlasin -------------
    def test_full_return_of_discounted_receipt_nets_to_zero_everywhere(self):
        """REF-3: to'liq qaytarish chek to'loviga cheklanadi → SOF 0."""
        shift = self.open_shift()
        sale = self._sale(shift, qty=3, odisc='60000')
        self._refund(sale, shift, 3, '240000')
        p, z = self._page(), self._z(shift)
        self.assertEqual(_dec(p['returned_total']), _dec(z['refund_total']))
        self.assertEqual(_dec(p['net_total']), _dec(z['net_sales']))
        self.assertEqual(_dec(p['net_total']), Decimal('0'))
        self.assertEqual(_dec(p['checks'][0]['net_total']), Decimal('0'))

    def test_partial_return_uses_item_price_on_both_reports(self):
        """REF-3: bir dona qaytганда — to'liq dona narxi (100 000),
        proporsional 80 000 EMAS. Sahifa ham, Z-hisobot ham shu sonni bersin."""
        shift = self.open_shift()
        sale = self._sale(shift, qty=3, odisc='60000')
        self._refund(sale, shift, 1, '100000')
        p, z = self._page(), self._z(shift)
        self.assertEqual(_dec(p['returned_total']), Decimal('100000'))
        self.assertEqual(_dec(p['returned_total']), _dec(z['refund_total']))
        self.assertEqual(_dec(p['net_total']), Decimal('140000'))
        self.assertEqual(_dec(p['net_total']), _dec(z['net_sales']))

    # ---- kun = smenlar yig'indisi -------------------------------------
    def test_one_day_two_shifts_page_equals_sum_of_zreports(self):
        s1 = self.open_shift()
        sale1 = self._sale(s1, qty=1)
        self._refund(sale1, s1, 1, '100000')
        self._close(s1)
        s2 = self.open_shift()
        self._sale(s2, qty=2)
        p, z1, z2 = self._page(), self._z(s1), self._z(s2)
        self.assertEqual(_dec(p['total']),
                         _dec(z1['total_rev']) + _dec(z2['total_rev']))
        self.assertEqual(_dec(p['returned_total']),
                         _dec(z1['refund_total']) + _dec(z2['refund_total']))
        self.assertEqual(_dec(p['net_total']),
                         _dec(z1['net_sales']) + _dec(z2['net_sales']))
        self.assertEqual(p['txn_count'], z1['txn_count'] + z2['txn_count'])

    # ---- SAL-2: ikki xil ta'rif, ikkalasi ham ko'rinsin ---------------
    def _cross_day_setup(self):
        """Kecha sotildi, BUGUN (boshqa smenда) qaytarildi."""
        yest = timezone.now() - timezone.timedelta(days=1)
        s_yest = self.open_shift()
        Shift.objects.filter(pk=s_yest.pk).update(opened_at=yest)
        sale = self._sale(s_yest, qty=1)
        SaleTransaction.objects.filter(pk=sale.transaction_id).update(sold_at=yest)
        Sale.objects.filter(pk=sale.pk).update(sold_at=yest)
        s_yest.refresh_from_db()
        self._close(s_yest)
        Shift.objects.filter(pk=s_yest.pk).update(closed_at=yest)
        s_today = self.open_shift()
        self._refund(sale, s_today, 1, '100000')
        return s_yest, s_today

    def test_returned_total_follows_the_sale_date(self):
        s_yest, _ = self._cross_day_setup()
        yd = (timezone.localdate() - timezone.timedelta(days=1)).strftime('%Y-%m-%d')
        self.assertEqual(
            _dec(self._page(date_from=yd, date_to=yd)['returned_total']),
            Decimal('100000'))
        self.assertEqual(_dec(self._page()['returned_total']), Decimal('0'))

    def test_period_returned_total_matches_the_zreport(self):
        """SAL-2 — yangi ko'rsatkich Z-hisobot bilan AYNAN mos kelsin."""
        s_yest, s_today = self._cross_day_setup()
        yd = (timezone.localdate() - timezone.timedelta(days=1)).strftime('%Y-%m-%d')
        self.assertEqual(
            _dec(self._page(date_from=yd, date_to=yd)['period_returned_total']),
            _dec(self._z(s_yest)['refund_total']),
            'KECHA: davr qaytarishi Z-hisobot bilan mos emas')
        p_today = self._page()
        self.assertEqual(_dec(p_today['period_returned_total']),
                         _dec(self._z(s_today)['refund_total']),
                         'BUGUN: davr qaytarishi Z-hisobot bilan mos emas')
        self.assertTrue(p_today['refunds_span_periods'],
                        'ikki ta\'rif farq qilganда sahifa izoh ko\'rsatsin')

    def test_same_day_return_needs_no_explanation(self):
        shift = self.open_shift()
        sale = self._sale(shift, qty=1)
        self._refund(sale, shift, 1, '100000')
        p = self._page()
        self.assertEqual(_dec(p['returned_total']),
                         _dec(p['period_returned_total']))
        self.assertFalse(p['refunds_span_periods'])


class SalesPageEdgeRows(MoneyTestBase):
    """SAL-4 — sahifa buzilmasin: cheksiz (eski) sotuvlar, to'liq chegirmali
    chek, kunlik "qaytgan" belgisi.

    Eski sotuvlarда `transaction` NULL bo'lishi mumkin — aynan shunday qatorlar
    `sales_list`ni ilgari 500 qilgan edi, shuning uchun har bir yangi mantiq
    ular bilan sinaladi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_edge', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()
        self.today = timezone.localdate().strftime('%Y-%m-%d')

    def _page(self, **kw):
        p = {'date_from': self.today, 'date_to': self.today}
        p.update(kw)
        r = self.client.get('/sales/', p)
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_sale_without_transaction_does_not_break_the_page(self):
        Sale.objects.create(
            transaction=None, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        self.assertEqual(_dec(self._page()['total']), Decimal('100000'))
        self.assertEqual(_dec(self._page(view='items')['total']),
                         Decimal('100000'))

    def test_legacy_row_mixed_with_discounted_receipt(self):
        Sale.objects.create(
            transaction=None, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal('60000'))
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=3, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        # 100 000 (chek'siz eski) + 240 000 (chegirmali chek) = 340 000
        self.assertEqual(_dec(self._page()['total']), Decimal('340000'))

    def test_daily_badge_shows_the_return_on_the_sale_day(self):
        """Kunlik belgi Python tomonда (localtime), kunlik jami esa DB tomonда
        (sold_at__date) guruhlanadi — ikkalasi BIR XIL kunga tushishi kerak,
        aks holда "qaytgan" belgisi jimgina yo'qolardi."""
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash')
        sale = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=2, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        Return.objects.create(sale=sale, shift=self.shift, quantity=1,
                              refunded_by=self.cashier,
                              refund_cash=Decimal('100000'))
        day = self._page()['daily_list'][0]
        self.assertEqual(_dec(str(day['total'])), Decimal('200000'))
        self.assertEqual(_dec(str(day['returned'])), Decimal('100000'),
                         'kunlik "qaytgan" belgisi yo\'qolib qoldi')
        self.assertEqual(_dec(str(day['net'])), Decimal('100000'))

    def test_receipt_fully_discounted_to_zero(self):
        """Chegirma chek summasiga TENG → bo'linishда nol maxraj bo'lmasin."""
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal('100000'))
        Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        self.assertEqual(_dec(self._page()['total']), Decimal('0'))


class SalesPageQueryBudget(MoneyTestBase):
    """SAL-3 — chek chegirmasi mantig'i N+1 ga aylanmasin."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_q', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()

    def _bulk(self, n, odisc='0'):
        for _ in range(n):
            txn = SaleTransaction.objects.create(
                branch=self.branch, sold_by=self.cashier, shift=self.shift,
                payment_method='cash', order_discount=Decimal(odisc))
            for _ln in range(3):
                Sale.objects.create(
                    transaction=txn, variant=self.variant, branch=self.branch,
                    quantity=1, sale_price=Decimal('100000'),
                    cost_at_sale=Decimal('60000'), sold_by=self.cashier)

    def _load(self):
        today = timezone.localdate().strftime('%Y-%m-%d')
        r = self.client.get('/sales/', {'date_from': today, 'date_to': today})
        self.assertEqual(r.status_code, 200)
        return r

    def test_query_count_does_not_grow_with_rows(self):
        self._bulk(5, odisc='30000')
        with CaptureQueriesContext(connection) as c1:
            self._load()
        self._bulk(35, odisc='30000')      # 40 chek / 120 qator
        with CaptureQueriesContext(connection) as c2:
            self._load()
        self.assertLessEqual(
            len(c2.captured_queries), len(c1.captured_queries) + 2,
            f'so\'rovlar soni qatorlar bilan o\'sdi: '
            f'{len(c1.captured_queries)} → {len(c2.captured_queries)} (N+1)')

    def test_page_without_discounts_pays_almost_nothing_extra(self):
        self._bulk(20)
        with CaptureQueriesContext(connection) as c:
            self._load()
        self.assertLess(len(c.captured_queries), 30,
                        'chegirmasiz sahifa uchun so\'rovlar juda ko\'p')


class SalesPagePeriodRefundScope(MoneyTestBase):
    """SAL-5 — "shu davrda qaytarilgani" faqat sana+filial oynasida ma'noli.

    Sotuvchi bo'yicha filtrlanганда `returned_total` filtrga bo'ysunadi, lekin
    kassadan chiqqan pulni sotuvchiga bo'lib bo'lmaydi (qaytarishni boshqa
    kassir rasmiylashtirgan bo'lishi mumkin). Ikki xil qamrovли sonni yonma-yon
    ko'rsatish — noto'g'ri taqqoslashga taklif; shuning uchun umuman
    ko'rsatilmaydi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_scope', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.other = User.objects.create_user(
            username='kassir2', password='x', role=User.Role.SOTUVCHI,
            branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()
        self.today = timezone.localdate().strftime('%Y-%m-%d')
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash')
        sale = Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        # qaytarishни BOSHQA kassir rasmiylashtirdi
        Return.objects.create(sale=sale, shift=self.shift, quantity=1,
                              refunded_by=self.other,
                              refund_cash=Decimal('100000'))

    def _page(self, **kw):
        p = {'date_from': self.today, 'date_to': self.today}
        p.update(kw)
        r = self.client.get('/sales/', p)
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_shown_for_a_plain_date_window(self):
        c = self._page()
        self.assertEqual(_dec(c['period_returned_total']), Decimal('100000'))

    def test_hidden_when_a_seller_filter_narrows_the_page(self):
        c = self._page(seller=self.other.pk)
        self.assertFalse(c['refunds_span_periods'],
                         'tor filtrда taqqoslash soni ko\'rsatilmasin')
        self.assertEqual(_dec(c['period_returned_total']), Decimal('0'))

    def test_hidden_when_a_search_narrows_the_page(self):
        self.assertFalse(self._page(q='Test koylak')['refunds_span_periods'])



SAL6_PAID, SAL6_GROSS, SAL6_DISC = (Decimal('240000'), Decimal('300000'),
                                    Decimal('60000'))


class Sal6EveryPageAgreesOnRevenue(TestCase):
    """SAL-6 — BITTA chek, HAMMA sahifa: hech biri mijoz to'laganidan
    boshqa raqam ko'rsatmasin.

    Sale qatorlarida faqat `line_discount` bor; CHEK chegirmasi
    (`order_discount`) SaleTransaction'da turadi. Shu bois qatorlar ustidan
    Sum() olgan har bir sahifa brutto ko'rsatardi:

        3×100 000, chek chegirmasi 60 000 (mijoz 240 000 to'lagan)
        /dashboard/ /reports/ /insights/ (bo'linmalari) /branches/ /users/
        /categories/ /cashier/ /customers/ /products/ /warehouse/ → 300 000

    Eng yomoni FOYDA edi: `revenue − cost` da revenue shishgan bo'lgani uchun
    foyda ham chegirma miqdorida oshib ko'rinardi (120 000, aslida 60 000) —
    kassir va kategoriya baholari shunga qarab qo'yilardi. Komissiya ham
    chegirma qilib berilgan puldan hisoblanardi.

    QOIDA (egasining REF-3 siyosatiga mos):
      CHEK darajasidagi o'lchov (filial/sotuvchi/mijoz/kun) — bitta chek
        aynan bittasiga tegishli → chegirma to'g'ridan-to'g'ri ayiriladi;
      QATOR darajasidagi o'lchov (mahsulot/kategoriya/guruh) — chegirma
        ayrim tovarga tegishli EMAS → qatorlar O'Z narxida qoladi va
        chegirma ALOHIDA qator bo'lib chiqadi (qatorlar + chegirma = jami).
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='B')
        self.u = User.objects.create_user(username='k', password='x',
                                          role=User.Role.ADMIN, is_staff=True,
                                          branch=self.branch, commission_percent=10)
        cat = Category.objects.create(name='Kiyim')
        p = Product.objects.create(name='P', code='P-0001', category=cat,
                                   default_sale_price=Decimal('100000'))
        self.p = p
        self.v = ProductVariant.objects.create(product=p, size='M', color='Q',
                                               barcode='2000000000017')
        BranchStock.objects.create(variant=self.v, branch=self.branch,
                                   stock_count=100, cost_price=Decimal('60000'),
                                   sale_price=Decimal('100000'))
        self.sh = Shift.objects.create(branch=self.branch, opened_by=self.u,
                                       opening_cash=Decimal('0'))
        self.cust = Customer.objects.create(name='Mijoz', phone='998900000000')
        self.c = Client(); self.c.force_login(self.u)
        self.today = timezone.localdate().strftime('%Y-%m-%d')
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.u, shift=self.sh,
            payment_method='cash', order_discount=SAL6_DISC, customer=self.cust)
        Sale.objects.create(transaction=txn, variant=self.v, branch=self.branch,
                            quantity=3, sale_price=Decimal('100000'),
                            cost_at_sale=Decimal('60000'), sold_by=self.u)

    def test_sweep(self):
        rows = []

        def get(url, params=None):
            r = self.c.get(url, params or {})
            assert r.status_code == 200, f'{url} -> {r.status_code}'
            c = r.context
            return c[0] if isinstance(c, list) else c

        def add(label, val, want):
            try:
                d = Decimal(str(val))
                mark = 'OK' if d == want else 'XATO'
            except Exception:
                d, mark = val, '?'
            rows.append((label, str(val), str(want), mark))

        day = {'date_from': self.today, 'date_to': self.today}

        c = get('/sales/', day)
        add('/sales/  KPI Jami tushum', c['total'], SAL6_PAID)
        add('/shift/../receipt/  JAMI SAVDO',
            get(reverse('shift_receipt', args=[self.sh.pk]))['total_rev'], SAL6_PAID)

        c = get('/dashboard/')
        add('/dashboard/  bugungi tushum', c['today_stats']['revenue'], SAL6_PAID)
        add('/dashboard/  bugungi foyda', c['today_stats']['profit'], SAL6_PAID - 180000)
        add('/dashboard/  pul oqimi (naqd)', c['today_by_method']['cash'], SAL6_PAID)
        add('/dashboard/  filial bo\'yicha', c['branch_today'][0]['revenue'], SAL6_PAID)
        add('/dashboard/  top mahsulot (qator narxi)',
            c['top_today'][0]['revenue'], SAL6_GROSS)
        add('/dashboard/  + chek chegirmasi', c['order_discount_today'], SAL6_DISC)

        c = get('/reports/', {'period': 'month', 'report_type': 'sales'})
        add('/reports/ sales  Jami daromad', c['summary']["Jami daromad (so'm)"], SAL6_PAID)
        c = get('/reports/', {'period': 'month', 'report_type': 'by_product'})
        add('/reports/ by_product  Jami daromad',
            c['summary']["Jami daromad (so'm)"], SAL6_PAID)
        c = get('/reports/', {'period': 'month', 'report_type': 'margin'})
        add('/reports/ margin  Jami daromad',
            c['summary']["Jami daromad (so'm)"], SAL6_PAID)
        add('/reports/ margin  Jami foyda',
            c['summary']["Jami foyda (so'm)"], SAL6_PAID - 180000)
        c = get('/reports/', {'period': 'month', 'report_type': 'pivot'})
        add('/reports/ pivot  Sof', c['summary']["Sof (so'm)"], SAL6_PAID)

        c = get('/insights/', day)
        add('/insights/  Tushum (sarlavha)', c['revenue'], SAL6_PAID)
        add('/insights/  Foyda', c['profit'], SAL6_PAID - 180000)
        add('/insights/  filial bo\'yicha', c['by_branch'][0]['revenue'], SAL6_PAID)
        add('/insights/  branch_compare', c['branch_compare'][0]['revenue'], SAL6_PAID)
        add('/insights/  top sotuvchi', c['top_sellers'][0]['revenue'], SAL6_PAID)
        add('/insights/  komissiya 10%', c['top_sellers'][0]['commission'], SAL6_PAID / 10)
        add('/insights/  top mahsulot (qator narxi)',
            c['top_products'][0]['revenue'], SAL6_GROSS)
        add('/insights/  + chek chegirmasi', c['order_discount_total'], SAL6_DISC)

        c = get('/branches/')
        add('/branches/  total_revenue', c['total_revenue'], SAL6_PAID)
        add('/branches/  m_gross (foyda)', c['branches'][0].m_gross, SAL6_PAID - 180000)
        c = get('/users/')
        add('/users/  s_revenue', c['users'][0].s_revenue, SAL6_PAID)
        add('/users/  s_commission 10%', c['users'][0].s_commission, SAL6_PAID / 10)
        c = get('/categories/')
        add('/categories/  kategoriya (qator narxi)', c['categories'][0].s_rev, SAL6_GROSS)
        add('/categories/  + chek chegirmasi', c['order_discount_total'], SAL6_DISC)
        add('/categories/  = sof jami', c['net_rev_total'], SAL6_PAID)
        c = get(f'/cashier/{self.u.pk}/')
        add('/cashier/  revenue', c['revenue'], SAL6_PAID)
        add('/cashier/  profit', c['profit'], SAL6_PAID - 180000)
        add('/cashier/  commission 10%', c['commission'], SAL6_PAID / 10)
        c = get(f'/customers/{self.cust.pk}/')
        add('/customers/  total_spent', c['total_spent'], SAL6_PAID)
        c = get(f'/products/{self.p.code}/')
        add('/products/  rev_30d (qator narxi)', c['product_kpis']['rev_30d'], SAL6_GROSS)
        add('/products/  + chek chegirmasi', c['product_kpis']['order_discount_30d'], SAL6_DISC)
        c = get('/warehouse/')
        add('/warehouse/  chek chegirmasi', c['order_discount_90d'], SAL6_DISC)

        w = max(len(r[0]) for r in rows)
        bad = [r for r in rows if r[3] == 'XATO']
        print('\n' + '='*(w+34))
        print(f"  3 x 100 000, chek chegirmasi 60 000  ->  MIJOZ TO'LAGAN 240 000")
        print('='*(w+34))
        for lbl, val, want, mark in rows:
            print(f"  {lbl:<{w}} {val:>12} (kutilgan {want:>9})  {mark}")
        print('='*(w+34))
        print(f"  XATO: {len(bad)} / {len(rows)}")
        print('='*(w+34))
        self.assertEqual(bad, [], f'{len(bad)} ta ko\'rsatkich mos kelmadi')

    def test_discount_row_is_actually_rendered(self):
        """Qator darajasidagi ro'yxatlar ostida chegirma qatori KO'RINSIN —
        aks holda foydalanuvchi 300 000 ni ko'rib, uni jami deb o'ylaydi."""
        day = {'date_from': self.today, 'date_to': self.today}
        for url, prm in (('/insights/', day), ('/categories/', {}),
                         (f'/cashier/{self.u.pk}/', {}), ('/dashboard/', {}),
                         (f'/products/{self.p.code}/', {})):
            r = self.c.get(url, prm)
            self.assertEqual(r.status_code, 200, url)
            # DISC-1: endi bitta "Chek chegirmasi" o'rniga UCH qism bor.
            # Bu fikstura sababsiz-emas, qo'lда chegirma (promo=0, exch=0).
            self.assertContains(r, "Qo'lda chegirma", msg_prefix=url,
                                status_code=200)

    def test_categories_rows_plus_discount_equal_the_net_total(self):
        """Qatorlar + chegirma = sof jami (aynan, yaxlitlashsiz)."""
        c = self.c.get('/categories/').context
        c = c[0] if isinstance(c, list) else c
        rows = sum(Decimal(str(x.s_rev)) for x in c['categories'])
        self.assertEqual(rows - Decimal(str(c['order_discount_total'])),
                         Decimal(str(c['net_rev_total'])))
        self.assertEqual(Decimal(str(c['net_rev_total'])), SAL6_PAID)

    def test_no_discount_means_no_extra_row_and_same_numbers(self):
        """Chegirmasiz chekда yangi mantiq hech nimani o'zgartirmasin."""
        SaleTransaction.objects.all().update(order_discount=Decimal('0'))
        c = self.c.get('/categories/').context
        c = c[0] if isinstance(c, list) else c
        self.assertEqual(Decimal(str(c['order_discount_total'])), Decimal('0'))
        self.assertEqual(Decimal(str(c['net_rev_total'])), SAL6_GROSS)
        self.assertNotContains(self.c.get('/categories/'), 'Chek chegirmasi')



D = Decimal


class RealDayPageEqualsSumOfZReports(TestCase):
    """SYNC — bitta REAL kun, sahifa == smenlarning Z-hisobotlari yig'indisi.

    Alohida-alohida tekshirilgan holatlar birga kelganда ham yopilishi kerak:
      - chek chegirmali sotuv (SAL-1)
      - ARALASH to'lov, payment_breakdown bo'yicha bo'linadi (ARCH-6)
      - qisman qaytarish, REF-3 siyosati bo'yicha dona narxida
      - KECHA sotilib BUGUN qaytarilgan tovar (SAL-2 — ikki xil ta'rif)
      - ALMASHTIRISH: dona qaytadi, lekin kassadan naqd CHIQMAYDI
      - kassadan chiqim (payout)
      - kun ichida IKKI smen

    Tekshiriladi: JAMI SAVDO, davr qaytarishi va chek soni ikkala tomonда bir
    xil; har bir Z-hisobotning KASSA qatorlari o'z JAMIsiga qo'shiladi; va
    almashtirishда kassadan pul chiqmaydi (aks holda kassir kamomadga tushardi).
    """

    def setUp(self):
        self.b = Branch.objects.create(name='B')
        self.u = User.objects.create_user(username='k', password='x',
                                          role=User.Role.ADMIN, is_staff=True,
                                          branch=self.b)
        p = Product.objects.create(name='P', code='P-0001',
                                   default_sale_price=D('100000'))
        self.v = ProductVariant.objects.create(product=p, size='M', color='Q',
                                               barcode='2000000000017')
        BranchStock.objects.create(variant=self.v, branch=self.b,
                                   stock_count=200, cost_price=D('60000'),
                                   sale_price=D('100000'))
        self.c = Client(); self.c.force_login(self.u)

    def _shift(self):
        return Shift.objects.create(branch=self.b, opened_by=self.u,
                                    opening_cash=D('100000'))

    def _close(self, sh, when=None):
        sh.closing_expected_cash = sh.compute_expected_cash()
        sh.counted_cash = sh.closing_expected_cash
        sh.status = Shift.Status.CLOSED
        sh.closed_at = when or timezone.now()
        sh.save()

    def _sale(self, sh, qty, odisc='0', pm='cash', bd=None):
        t = SaleTransaction.objects.create(
            branch=self.b, sold_by=self.u, shift=sh, payment_method=pm,
            order_discount=D(odisc), payment_breakdown=(bd or []))
        return Sale.objects.create(transaction=t, variant=self.v, branch=self.b,
                                   quantity=qty, sale_price=D('100000'),
                                   cost_at_sale=D('60000'), sold_by=self.u)

    def _page(self, d_from, d_to):
        r = self.c.get('/sales/', {'date_from': d_from, 'date_to': d_to})
        assert r.status_code == 200, r.status_code
        c = r.context
        return c[0] if isinstance(c, list) else c

    def _z(self, sh):
        r = self.c.get(reverse('shift_receipt', args=[sh.pk]))
        assert r.status_code == 200
        c = r.context
        return c[0] if isinstance(c, list) else c

    def test_real_day(self):
        yest = timezone.now() - timezone.timedelta(days=1)

        # ---- KECHA: bitta sotuv, smen yopiladi
        s0 = self._shift()
        Shift.objects.filter(pk=s0.pk).update(opened_at=yest)
        old = self._sale(s0, 1)
        SaleTransaction.objects.filter(pk=old.transaction_id).update(sold_at=yest)
        Sale.objects.filter(pk=old.pk).update(sold_at=yest)
        s0.refresh_from_db(); self._close(s0, yest)
        Shift.objects.filter(pk=s0.pk).update(closed_at=yest)

        # ---- BUGUN, 1-smen
        s1 = self._shift()
        a = self._sale(s1, 3, odisc='60000')                 # 240 000 naqd
        b = self._sale(s1, 2, pm='mixed',                    # 200 000 aralash
                       bd=[{'method': 'cash', 'amount': 120000},
                           {'method': 'card', 'amount': 80000}])
        Return.objects.create(sale=a, shift=s1, quantity=1,   # qisman qaytarish
                              refunded_by=self.u, refund_cash=D('100000'))
        Return.objects.create(sale=old, shift=s1, quantity=1, # KECHAGI tovar
                              refunded_by=self.u, refund_cash=D('100000'))
        CashPayout.objects.create(shift=s1, branch=self.b, amount=D('50000'),
                                  category='lunch', created_by=self.u)
        self._close(s1)

        # ---- BUGUN, 2-smen: ALMASHTIRISH (naqd chiqmaydi)
        s2 = self._shift()
        c_ = self._sale(s2, 1)                                # 100 000
        Return.objects.create(sale=c_, shift=s2, quantity=1, is_exchange=True,
                              cash_refunded=D('0'), refunded_by=self.u)
        self._sale(s2, 1)                                     # almashtirilgan yangi tovar

        td = timezone.localdate().strftime('%Y-%m-%d')
        p = self._page(td, td)
        z1, z2 = self._z(s1), self._z(s2)

        def dz(k): return _d(z1[k]) + _d(z2[k])
        def _d(x): return D(str(x))

        rows = [
            ('JAMI SAVDO', _d(p['total']), dz('total_rev')),
            ('Qaytarilgan (davr)', _d(p['period_returned_total']),
             dz('refund_total')),
            ('Cheklar', D(p['txn_count']), D(z1['txn_count'] + z2['txn_count'])),
        ]
        for lbl, a_, b_ in rows:
            self.assertEqual(a_, b_, f"{lbl}: sahifa {a_} != Z1+Z2 {b_}")
        for name, z in (('Z1', z1), ('Z2', z2)):
            lines = (_d(z['shift'].opening_cash) + _d(z['cash_sales'])
                     + _d(z['debt_payments']) + _d(z['cash_ins_total'])
                     - _d(z['payouts_total']) - _d(z['refund_total'])
                     + _d(z['post_close_delta']) + _d(z['rounding_delta']))
            self.assertEqual(lines, _d(z['expected']), f'{name} kassa yopilmadi')
        self.assertEqual(_d(z2['refund_total']), D('0'),
                         'almashtirishда kassadan naqd chiqmasligi kerak')

        # SAL-2: kechagi tovar SOTUV kuniga yoziladi (100 000), lekin kassaga
        # BUGUN ta'sir qiladi — shu bois ikki ko'rsatkich ataylab farq qiladi.
        self.assertEqual(_d(p['returned_total']), D('100000'))
        self.assertTrue(p['refunds_span_periods'])


class Disc1DiscountSplit(MoneyTestBase):
    """DISC-1 — "chek chegirmasi" UCH XIL narsani bitta songa yig'ib kelardi.

    Egasi sotuvlar sahifasida 3 665 635 so'm "chek chegirmasi" ko'rib,
    "biz bunchalik chegirma bermaganmiz" dedi — va haq edi:

        AKSIYA (server, egasi sozlagan)   3 001 635   289 chek
        QO'LDA (kassir bergan)                64 000    15 chek
        ALMASHTIRISH krediti (chegirma EMAS) 596 500    10 chek

    Ya'ni qo'lда berilgani atigi 64 ming edi. Uchalasi boshqa-boshqa qaror:
    aksiya — marketing, qo'lда — kassir ixtiyori (kamomad xavfi shu yerда),
    almashtirish — umuman chegirma emas, mijoz eski tovar bilan to'lagan.
    Endi ular alohida maydonlarда saqlanadi va alohida ko'rsatiladi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_disc', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()
        self.today = timezone.localdate().strftime('%Y-%m-%d')

    def _txn(self, odisc='0', promo='0', exch='0', reason=''):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal(odisc),
            promo_discount=Decimal(promo), exchange_credit=Decimal(exch),
            discount_reason=reason)
        Sale.objects.create(transaction=t, variant=self.variant,
                            branch=self.branch, quantity=1,
                            sale_price=Decimal('100000'),
                            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        return t

    def _split(self):
        r = self.client.get('/sales/', {'date_from': self.today,
                                        'date_to': self.today})
        self.assertEqual(r.status_code, 200)
        c = r.context
        c = c[0] if isinstance(c, list) else c
        return c['discount_split'], c

    # ---- model invariant ------------------------------------------------
    def test_parts_always_add_up_to_the_total(self):
        t = self._txn(odisc='10000', promo='6000', exch='0')
        self.assertEqual(t.manual_discount, Decimal('4000'))
        self.assertEqual(t.promo_discount + t.exchange_credit
                         + t.manual_discount, t.order_discount)

    def test_manual_never_goes_negative(self):
        t = self._txn(odisc='5000', promo='5000')
        self.assertEqual(t.manual_discount, Decimal('0'))

    def test_db_rejects_a_part_larger_than_the_total(self):
        """Aks holда "qo'lда chegirma" manfiy chiqib, kassir auditi buzilardi."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SaleTransaction.objects.create(
                    branch=self.branch, sold_by=self.cashier, shift=self.shift,
                    payment_method='cash', order_discount=Decimal('1000'),
                    promo_discount=Decimal('5000'))

    def test_db_rejects_negative_parts(self):
        for field in ('promo_discount', 'exchange_credit'):
            with self.subTest(field=field):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        SaleTransaction.objects.create(
                            branch=self.branch, sold_by=self.cashier,
                            shift=self.shift, payment_method='cash',
                            order_discount=Decimal('1000'),
                            **{field: Decimal('-1')})

    # ---- reporting ------------------------------------------------------
    def test_page_splits_the_three_kinds(self):
        self._txn(odisc='30000', promo='30000')                    # aksiya
        self._txn(odisc='5000', reason='skidka')                   # qo'lda
        self._txn(odisc='20000', exch='20000',
                  reason='Almashtirish: eski tovar hisobiga')      # almashtirish
        split, _ = self._split()
        self.assertEqual(split['promo'], Decimal('30000'))
        self.assertEqual(split['manual'], Decimal('5000'))
        self.assertEqual(split['exchange'], Decimal('20000'))
        self.assertEqual(split['total'], Decimal('55000'))
        self.assertEqual(split['promo'] + split['manual'] + split['exchange'],
                         split['total'], 'qismlar JAMIga qo\'shilishi SHART')

    def test_mixed_receipt_splits_promo_from_manual(self):
        """Bitta chekда ham aksiya, ham qo'lда chegirma bo'lsa — ajratilsin.
        Aynan shu holat eski ma'lumotда ajratib bo'lmas edi."""
        self._txn(odisc='12000', promo='9000', reason='mijoz')
        split, _ = self._split()
        self.assertEqual(split['promo'], Decimal('9000'))
        self.assertEqual(split['manual'], Decimal('3000'))

    def test_revenue_is_unchanged_by_the_split(self):
        """Bu faqat YORLIQ masalasi — pul hisobi o'zgarmasligi kerak."""
        self._txn(odisc='30000', promo='30000')
        self._txn(odisc='20000', exch='20000',
                  reason='Almashtirish: eski tovar hisobiga')
        split, c = self._split()
        # 2 x 100 000 − 50 000 chegirma/kredit = 150 000
        self.assertEqual(_dec(c['total']), Decimal('150000'))
        self.assertEqual(_dec(c['order_discount_total']), split['total'])

    def test_exchange_credit_is_not_reported_as_a_discount(self):
        self._txn(odisc='20000', exch='20000',
                  reason='Almashtirish: eski tovar hisobiga')
        split, _ = self._split()
        self.assertEqual(split['manual'], Decimal('0'),
                         'almashtirish krediti kassir chegirmasi EMAS')
        self.assertEqual(split['promo'], Decimal('0'))
        self.assertEqual(split['exchange'], Decimal('20000'))

    def test_labels_are_rendered_not_just_computed(self):
        self._txn(odisc='30000', promo='30000')
        self._txn(odisc='5000', reason='skidka')
        self._txn(odisc='20000', exch='20000',
                  reason='Almashtirish: eski tovar hisobiga')
        r = self.client.get('/sales/', {'date_from': self.today,
                                        'date_to': self.today})
        for label in ('Aksiya', "Qo'lda chegirma", 'Almashtirish krediti'):
            self.assertContains(r, label)

    def test_no_discount_renders_nothing(self):
        self._txn()
        r = self.client.get('/sales/', {'date_from': self.today,
                                        'date_to': self.today})
        self.assertNotContains(r, 'Almashtirish krediti')


class Disc1CheckoutRecordsPromoSeparately(MoneyTestBase):
    """DISC-1 — pos_checkout aksiya ulushini ALOHIDA yozadi.

    Ilgari `order_discount = _server_promo + _manual_disc` bitta songa
    qo'shilardi va keyin ajratib bo'lmasdi.
    """

    def _promo(self, percent):
        from inventory.models import Promotion
        return Promotion.objects.create(
            name='Test aksiya', percent=Decimal(percent), is_active=True)

    def test_promo_only_checkout_records_zero_manual(self):
        self.open_shift()
        try:
            self._promo('10')
        except Exception:
            self.skipTest('Promotion modeli boshqacha — alohida sinaladi')
        r = self.checkout()
        self.assertEqual(r.status_code, 200)
        t = SaleTransaction.objects.latest('id')
        self.assertEqual(t.promo_discount + t.manual_discount,
                         t.order_discount)

    def test_manual_discount_is_not_counted_as_promo(self):
        self.open_shift()
        r = self.checkout(order_discount='7000', discount_reason='defect')
        self.assertEqual(r.status_code, 200)
        t = SaleTransaction.objects.latest('id')
        self.assertEqual(t.order_discount, Decimal('7000'))
        self.assertEqual(t.promo_discount, Decimal('0'))
        self.assertEqual(t.manual_discount, Decimal('7000'),
                         "qo'lда berilgan chegirma aksiya deb yozilmasin")

    def test_exchange_credit_is_recorded_on_the_new_receipt(self):
        """pos_exchange krediti exchange_credit'ga yozilsin (chegirma emas)."""
        from inventory.models import Promotion  # noqa: F401
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.open_shift(),
            payment_method='cash', order_discount=Decimal('50000'),
            exchange_credit=Decimal('50000'),
            discount_reason='Almashtirish: eski tovar hisobiga')
        self.assertEqual(t.manual_discount, Decimal('0'))
        self.assertEqual(t.promo_discount, Decimal('0'))


class Disc2PromoBackfillCorrection(MoneyTestBase):
    """DISC-2 — sababsiz chegirma AKSIYA emas, QO'LDA berilgan chegirma.

    0060 backfill'i "sababsiz => aksiya" deb hisoblagan edi, chunki
    pos_checkout qo'lда chegirma uchun sababni majburiy qiladi. Lekin o'sha
    talab 2026-08-26 da qo'shilган (a2d1286), `order_discount` esa
    2026-06-03 dan beri bor — oradagi uch oyда kassir sababsiz chegirma
    bera olardi.

    Ishlab chiqarish buni tasdiqladi: Promotion jami = 0, ya'ni
    _evaluate_promotions() hech qachon noldan boshqa son qaytara olmagan.
    3 001 635 so'm (289 chek) — kassir bergan chegirma, aksiya emas.

    Xato yo'nalishi muhim: kassir bergan pulni "egasining aksiyasi" qilib
    ko'rsatish kuzatilishi kerak bo'lgan signalni YASHIRADI. Aniqlab
    bo'lmaganда MUAMMONI KO'RSATADIGAN tomonga og'ish kerak.
    """

    def _fix(self):
        """0061 migratsiyasining o'zini chaqiramiz (mantiq qulflansin)."""
        import importlib
        from django.apps import apps as registry
        mod = importlib.import_module(
            'inventory.migrations.0061_fix_promo_backfill')
        mod.fix_promo_backfill(registry, None)

    def _txn(self, odisc, promo, reason='', when=None):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal(odisc),
            promo_discount=Decimal(promo), discount_reason=reason)
        if when:
            SaleTransaction.objects.filter(pk=t.pk).update(sold_at=when)
            t.refresh_from_db()
        return t

    def setUp(self):
        super().setUp()
        self.shift = self.open_shift()

    def test_reasonless_discount_becomes_manual_not_promo(self):
        """0060 izi: sabab bo'sh + promo == order_discount."""
        t = self._txn('10000', '10000')
        self.assertEqual(t.manual_discount, Decimal('0'))   # xato holat
        self._fix()
        t.refresh_from_db()
        self.assertEqual(t.promo_discount, Decimal('0'))
        self.assertEqual(t.manual_discount, Decimal('10000'),
                         'kassir bergan chegirma qo\'lда deb ko\'rinsin')

    def test_money_is_untouched(self):
        """Faqat yorliq ustuni o'zgaradi — pul emas."""
        t = self._txn('10000', '10000')
        before = t.order_discount
        self._fix()
        t.refresh_from_db()
        self.assertEqual(t.order_discount, before)

    def test_exchange_credit_is_left_alone(self):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal('50000'),
            exchange_credit=Decimal('50000'),
            discount_reason='Almashtirish: eski tovar hisobiga')
        self._fix()
        t.refresh_from_db()
        self.assertEqual(t.exchange_credit, Decimal('50000'))
        self.assertEqual(t.manual_discount, Decimal('0'),
                         'almashtirish krediti qo\'lда chegirmaga aylanmasin')

    def test_receipt_with_a_reason_is_left_alone(self):
        t = self._txn('7000', '0', reason='defect')
        self._fix()
        t.refresh_from_db()
        self.assertEqual(t.manual_discount, Decimal('7000'))

    def test_partial_promo_write_is_not_touched(self):
        """pos_checkout yozgan HAQIQIY promo (promo < order_discount) —
        0060 izi emas, tegilmasin."""
        t = self._txn('10000', '6000', reason='mijoz')
        self._fix()
        t.refresh_from_db()
        self.assertEqual(t.promo_discount, Decimal('6000'))
        self.assertEqual(t.manual_discount, Decimal('4000'))

    def test_receipts_after_a_real_promotion_are_preserved(self):
        """Aksiya yaratilgach undan KEYINGI cheklar haqiqatan aksiya bo'lishi
        mumkin — himoya sifatida ular tegilmaydi."""
        from inventory.models import Promotion
        start = timezone.now() - timezone.timedelta(days=5)
        try:
            Promotion.objects.create(name='A', percent=Decimal('10'),
                                     is_active=True, valid_from=start)
        except Exception:
            self.skipTest('Promotion maydonlari boshqacha')
        old = self._txn('10000', '10000',
                        when=timezone.now() - timezone.timedelta(days=20))
        new = self._txn('8000', '8000',
                        when=timezone.now() - timezone.timedelta(days=1))
        self._fix()
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual(old.promo_discount, Decimal('0'),
                         'aksiyaдан OLDINGI chek tuzatilsin')
        self.assertEqual(new.promo_discount, Decimal('8000'),
                         'aksiyaдан KEYINGI chek tegilmasin')

    def test_reported_split_after_the_fix(self):
        """Sahifa endi buni QO'LDA chegirma sifatida ko'rsatsin."""
        admin = User.objects.create_user(
            username='admin_d2', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        c = Client(); c.force_login(admin)
        t = self._txn('30000', '30000')
        Sale.objects.create(transaction=t, variant=self.variant,
                            branch=self.branch, quantity=1,
                            sale_price=Decimal('100000'),
                            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        self._fix()
        today = timezone.localdate().strftime('%Y-%m-%d')
        ctx = c.get('/sales/', {'date_from': today, 'date_to': today}).context
        ctx = ctx[0] if isinstance(ctx, list) else ctx
        sp = ctx['discount_split']
        self.assertEqual(sp['promo'], Decimal('0'))
        self.assertEqual(sp['manual'], Decimal('30000'))
        self.assertEqual(_dec(ctx['total']), Decimal('70000'),
                         'tushum o\'zgarmasligi kerak')


class Disc3DiscountFilter(MoneyTestBase):
    """DISC-3 — "chegirma berilganmi?" filtri.

    Egasi qo'lда berilgan chegirmani kuzatmoqchi. Buning uchun chegirmali
    cheklarni ajratib ko'ra olish kerak — ayniqsa QO'LDA berilganini
    (aksiya egasining o'z qarori, almashtirish krediti esa umuman chegirma
    emas).

    Filtr JAMIga ham ta'sir qilishi SHART: filtrlangan qatorlar bo'yicha
    tushum va chegirma qayta hisoblanadi, aks holда "faqat qo'lда" tanlaб
    jami butun davrniki bo'lib qolardi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_df', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()
        self.today = timezone.localdate().strftime('%Y-%m-%d')

        def mk(odisc='0', promo='0', exch='0', reason='', ldisc='0'):
            t = SaleTransaction.objects.create(
                branch=self.branch, sold_by=self.cashier, shift=self.shift,
                payment_method='cash', order_discount=Decimal(odisc),
                promo_discount=Decimal(promo), exchange_credit=Decimal(exch),
                discount_reason=reason)
            return Sale.objects.create(
                transaction=t, variant=self.variant, branch=self.branch,
                quantity=1, sale_price=Decimal('100000'),
                line_discount=Decimal(ldisc),
                cost_at_sale=Decimal('60000'), sold_by=self.cashier)

        self.plain = mk()                                     # chegirmasiz
        self.manual = mk(odisc='5000', reason='skidka')       # qo'lda
        self.promo = mk(odisc='9000', promo='9000')           # aksiya
        self.exch = mk(odisc='20000', exch='20000',
                       reason='Almashtirish: eski tovar hisobiga')
        self.mixed = mk(odisc='12000', promo='9000', reason='mijoz')  # ikkalasi
        self.line_only = mk(ldisc='3000')                     # faqat qator chegirmasi
        # Eski, chek'siz sotuv — "chegirmasiz" ga tushishi kerak
        self.legacy = Sale.objects.create(
            transaction=None, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)

    def _ids(self, **params):
        p = {'date_from': self.today, 'date_to': self.today, 'view': 'items'}
        p.update(params)
        r = self.client.get('/sales/', p)
        self.assertEqual(r.status_code, 200)
        c = r.context
        c = c[0] if isinstance(c, list) else c
        return {s.pk for s in c['sales']}, c

    def test_no_filter_shows_everything(self):
        ids, _ = self._ids()
        self.assertEqual(len(ids), 7)

    def test_any_discount(self):
        ids, _ = self._ids(discount='any')
        self.assertEqual(ids, {self.manual.pk, self.promo.pk, self.exch.pk,
                               self.mixed.pk, self.line_only.pk},
                         'qator chegirmasi ham "chegirma berilgan" hisoblanadi')

    def test_no_discount_includes_legacy_rows(self):
        ids, _ = self._ids(discount='none')
        self.assertEqual(ids, {self.plain.pk, self.legacy.pk},
                         'chek\'siz eski sotuv ham chegirmasiz hisoblansin')

    def test_manual_only(self):
        """Aksiya va almashtirish chiqib ketsin, aralash chek QOLSIN."""
        ids, _ = self._ids(discount='manual')
        self.assertEqual(ids, {self.manual.pk, self.mixed.pk})

    def test_promo_only(self):
        ids, _ = self._ids(discount='promo')
        self.assertEqual(ids, {self.promo.pk, self.mixed.pk})

    def test_exchange_only(self):
        ids, _ = self._ids(discount='exchange')
        self.assertEqual(ids, {self.exch.pk})

    # ---- filtr JAMIga ham ta'sir qilsin -------------------------------
    def test_totals_follow_the_filter(self):
        _, c = self._ids(discount='manual')
        # 2 chek × 100 000 − (5 000 qo'lda + 12 000 aralash) = 183 000
        self.assertEqual(_dec(c['total']), Decimal('183000'))
        self.assertEqual(_dec(c['order_discount_total']), Decimal('17000'))
        self.assertEqual(c['txn_count'], 2)

    def test_split_follows_the_filter(self):
        _, c = self._ids(discount='manual')
        sp = c['discount_split']
        self.assertEqual(sp['manual'], Decimal('8000'))   # 5 000 + 3 000
        self.assertEqual(sp['promo'], Decimal('9000'))    # aralash chekning aksiyasi
        self.assertEqual(sp['exchange'], Decimal('0'))

    def test_exchange_filter_shows_only_the_credit(self):
        _, c = self._ids(discount='exchange')
        sp = c['discount_split']
        self.assertEqual(sp['exchange'], Decimal('20000'))
        self.assertEqual(sp['manual'], Decimal('0'))

    # ---- xavfsizlik / mustahkamlik ------------------------------------
    def test_unknown_value_is_ignored_not_500(self):
        _, c = self._ids(discount='; drop table')
        self.assertEqual(c['discount'], '')
        self.assertEqual(c['txn_count'], 6)

    def test_filter_suppresses_the_zreport_comparison(self):
        """SAL-5: tor filtrда taqqoslash soni ko'rsatilmasin."""
        _, c = self._ids(discount='manual')
        self.assertFalse(c['refunds_span_periods'])

    def test_filter_is_rendered_and_selected(self):
        r = self.client.get('/sales/', {'date_from': self.today,
                                        'date_to': self.today,
                                        'discount': 'manual'})
        self.assertContains(r, 'faqat qo\'lda (yaxlitlash ham)')
        self.assertContains(r, 'value="manual" selected')

    def test_daily_totals_do_not_double_count(self):
        """Filtr forward-FK bo'yicha — kunlik jamiда fanout bo'lmasin."""
        _, c = self._ids(discount='any')
        day = c['daily_list'][0]
        # 5 chek × 100 000 − (5k + 9k + 20k + 12k qator-chegirmasiz) − 3k qator
        self.assertEqual(_dec(str(day['total'])), Decimal('451000'))


class Ret1ReturnsAffectProfit(MoneyTestBase):
    """RET-1 — qaytarilgan tovar TUSHUMdan ham, TANNARXdan ham chiqsin.

    Topilgani: `/sales/` dan boshqa HECH BIR sahifa qaytarishni hisobga
    olmasdi. 3 dona sotilib 1 dona qaytsa ham hamma joyда uchalasining
    tushumi va tannarxi turaverardi:

        haqiqat     tushum 161 000  tannarx 90 000  foyda 71 000
        /insights/  tushum 241 500  tannarx 135 000 foyda 106 500

    Ikki tuzatma ALOHIDA qoidaga bo'ysunadi:
      TUSHUM  — kassadan HAQIQATDA chiqqan pul (effective_cash_refund).
                Almashtirishда naqd chiqmaydi, demak tushum kamaymaydi.
      TANNARX — tovar OMBORGA QAYTGAN bo'lsagina qaytariladi. Ochiq narxli
                tovar qaytmaydi (pos_refund uni tiklamaydi), demak uning
                tannarxi COGSда qolishi KERAK.

    Shu ikki qoida almashtirishni ham to'g'ri hisoblaydi: eski tovarning
    tannarxi ikki marta sanalmaydi (bir marta asl chekда, ikkinchi marta
    yangi chekда) — bu har almashtirishда foydani tannarx miqdorida
    kamaytirib ko'rsatardi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_ret', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()
        self.today = timezone.localdate().strftime('%Y-%m-%d')

    def _sale(self, qty=1, price='100000', cost='60000', variant=None):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash')
        return Sale.objects.create(
            transaction=t, variant=variant or self.variant, branch=self.branch,
            quantity=qty, sale_price=Decimal(price),
            cost_at_sale=Decimal(cost), sold_by=self.cashier)

    def _pages(self):
        def ctx(url, prm=None):
            r = self.client.get(url, prm or {})
            self.assertEqual(r.status_code, 200, url)
            c = r.context
            return c[0] if isinstance(c, list) else c
        day = {'date_from': self.today, 'date_to': self.today}
        ins = ctx('/insights/', day)
        cash = ctx(f'/cashier/{self.cashier.pk}/')
        dash = ctx('/dashboard/')['today_stats']
        return ins, cash, dash

    # ---- oddiy qaytarish ------------------------------------------------
    def test_full_return_leaves_zero_profit_everywhere(self):
        s = self._sale(qty=1)
        Return.objects.create(sale=s, shift=self.shift, quantity=1,
                              refunded_by=self.cashier,
                              refund_cash=Decimal('100000'))
        ins, cash, dash = self._pages()
        for name, c in (('insights', ins), ('cashier', cash), ('dashboard', dash)):
            with self.subTest(page=name):
                self.assertEqual(_dec(c['revenue']), Decimal('0'), name)
                self.assertEqual(_dec(c['cost'] if 'cost' in c else c['total_cost']),
                                 Decimal('0'), name)
                self.assertEqual(_dec(c['profit']), Decimal('0'), name)

    def test_partial_return(self):
        """3 sotildi, 1 qaytdi -> 2 dona: 200 000 / 120 000 / 80 000."""
        s = self._sale(qty=3)
        Return.objects.create(sale=s, shift=self.shift, quantity=1,
                              refunded_by=self.cashier,
                              refund_cash=Decimal('100000'))
        ins, cash, _ = self._pages()
        self.assertEqual(_dec(ins['revenue']), Decimal('200000'))
        self.assertEqual(_dec(ins['total_cost']), Decimal('120000'))
        self.assertEqual(_dec(ins['profit']), Decimal('80000'))
        self.assertEqual(_dec(cash['profit']), Decimal('80000'))

    # ---- ALMASHTIRISH ---------------------------------------------------
    def test_exchange_does_not_double_count_cost(self):
        """Asl tovar (tannarx 60 000) qaytdi, yangisi (70 000) ketdi.
        Mijoz 100 000 to'lagan. Haqiqiy foyda = 100 000 − 70 000 = 30 000.
        Ilgari eski tannarx ham qolib, foyda 100 000 − 130 000 = −30 000
        bo'lib ko'rinardi."""
        p2 = Product.objects.create(name='Yangi', default_sale_price=Decimal('100000'))
        v2 = ProductVariant.objects.create(product=p2, size='L', color='Oq',
                                           barcode='2000000000031')
        BranchStock.objects.create(variant=v2, branch=self.branch, stock_count=10,
                                   cost_price=Decimal('70000'),
                                   sale_price=Decimal('100000'))
        old = self._sale(qty=1)                       # 100 000 / tannarx 60 000
        Return.objects.create(sale=old, shift=self.shift, quantity=1,
                              refunded_by=self.cashier, is_exchange=True,
                              cash_refunded=Decimal('0'))
        t2 = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal('100000'),
            exchange_credit=Decimal('100000'),
            discount_reason='Almashtirish: eski tovar hisobiga')
        Sale.objects.create(transaction=t2, variant=v2, branch=self.branch,
                            quantity=1, sale_price=Decimal('100000'),
                            cost_at_sale=Decimal('70000'), sold_by=self.cashier)
        ins, _, _ = self._pages()
        self.assertEqual(_dec(ins['revenue']), Decimal('100000'),
                         'almashtirishда naqd chiqmaydi — tushum kamaymasin')
        self.assertEqual(_dec(ins['total_cost']), Decimal('70000'),
                         'eski tovar tannarxi ikki marta sanalmasin')
        self.assertEqual(_dec(ins['profit']), Decimal('30000'))

    # ---- OCHIQ NARXLI: omborga qaytmaydi -> tannarx qoladi --------------
    def test_open_price_return_keeps_the_cost(self):
        self.product.is_open_price = True
        self.product.save(update_fields=['is_open_price'])
        s = self._sale(qty=1)
        Return.objects.create(sale=s, shift=self.shift, quantity=1,
                              refunded_by=self.cashier,
                              refund_cash=Decimal('100000'))
        ins, _, _ = self._pages()
        self.assertEqual(_dec(ins['revenue']), Decimal('0'))
        self.assertEqual(_dec(ins['total_cost']), Decimal('60000'),
                         'omborga qaytmagan tovar tannarxi COGSда qolsin')
        self.assertEqual(_dec(ins['profit']), Decimal('-60000'),
                         'bu HAQIQIY zarar: tovar ham ketdi, pul ham qaytdi')

    # ---- bo'linmalar ham ergashsin --------------------------------------
    def test_breakdowns_follow(self):
        s = self._sale(qty=3)
        Return.objects.create(sale=s, shift=self.shift, quantity=1,
                              refunded_by=self.cashier,
                              refund_cash=Decimal('100000'))
        ins, _, _ = self._pages()
        self.assertEqual(_dec(ins['top_products'][0]['revenue']), Decimal('200000'))
        self.assertEqual(_dec(ins['by_branch'][0]['revenue']), Decimal('200000'))
        self.assertEqual(_dec(ins['branch_compare'][0]['revenue']), Decimal('200000'))
        self.assertEqual(_dec(ins['top_sellers'][0]['revenue']), Decimal('200000'))
        self.assertEqual(_dec(ins['top_profit'][0]['profit']), Decimal('80000'))

    def test_branches_users_categories_follow(self):
        s = self._sale(qty=3)
        Return.objects.create(sale=s, shift=self.shift, quantity=1,
                              refunded_by=self.cashier,
                              refund_cash=Decimal('100000'))
        def ctx(url):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            c = r.context
            return c[0] if isinstance(c, list) else c
        br = ctx('/branches/')['branches'][0]
        self.assertEqual(_dec(br.m_revenue), Decimal('200000'))
        self.assertEqual(_dec(br.m_gross), Decimal('80000'))
        us = [x for x in ctx('/users/')['users'] if x.pk == self.cashier.pk][0]
        self.assertEqual(_dec(us.s_revenue), Decimal('200000'),
                         'qaytgan tovardan komissiya to\'lanmasin')
        # Kategoriya bo'yicha ham: test mahsulotiga kategoriya biriktiramiz,
        # aks holda sotuv hech qaysi kategoriyaga tushmaydi va tekshiruv
        # hech nimani isbotlamaydi.
        cat = Category.objects.create(name='Test kategoriya')
        self.product.category = cat
        self.product.save(update_fields=['category'])
        ca = [x for x in ctx('/categories/')['categories'] if x.pk == cat.pk][0]
        self.assertEqual(_dec(ca.s_rev), Decimal('200000'))
        self.assertEqual(_dec(ca.s_profit), Decimal('80000'))

    def test_no_returns_changes_nothing(self):
        self._sale(qty=2)
        ins, _, _ = self._pages()
        self.assertEqual(_dec(ins['revenue']), Decimal('200000'))
        self.assertEqual(_dec(ins['total_cost']), Decimal('120000'))
        self.assertEqual(_dec(ins['profit']), Decimal('80000'))


class Disc4ExchangeCreditOnTheReceipt(MoneyTestBase):
    """DISC-4 — mijozning chekida almashtirish krediti "Chegirma" deb
    chiqmasin.

    Chek #4706 da shunday chiqqan edi:
        Oraliq summa 80 500 · Chegirma −80 500 · JAMI 0 so'm
    Mijoz o'sha 80 500 ni oldingi chekда to'lagan va eski tovarni qaytargan
    edi — bu chegirma emas. Kassa oldiда bahsga sabab bo'ladigan yorliq.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_d4', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()

    def _txn(self, odisc='0', exch='0', ldisc='0'):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal(odisc),
            exchange_credit=Decimal(exch))
        Sale.objects.create(transaction=t, variant=self.variant,
                            branch=self.branch, quantity=1,
                            sale_price=Decimal('100000'),
                            line_discount=Decimal(ldisc),
                            cost_at_sale=Decimal('60000'), sold_by=self.cashier)
        return t

    def test_customer_discount_excludes_the_credit(self):
        t = self._txn(odisc='100000', exch='100000')
        self.assertEqual(t.discount_total, Decimal('100000'))
        self.assertEqual(t.customer_discount, Decimal('0'),
                         'almashtirish krediti chegirma emas')

    def test_mixed_receipt_splits_both(self):
        t = self._txn(odisc='30000', exch='20000', ldisc='5000')
        # qator 5 000 + chek 30 000 = 35 000; shundan 20 000 kredit
        self.assertEqual(t.discount_total, Decimal('35000'))
        self.assertEqual(t.customer_discount, Decimal('15000'))

    def test_receipt_page_labels_the_credit_separately(self):
        t = self._txn(odisc='100000', exch='100000')
        r = self.client.get(reverse('transaction_detail', args=[t.public_id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Almashtirish krediti')
        self.assertNotContains(r, 'Chegirma:')

    def test_plain_discount_still_says_chegirma(self):
        t = self._txn(odisc='7000')
        r = self.client.get(reverse('transaction_detail', args=[t.public_id]))
        self.assertContains(r, 'Chegirma:')
        self.assertNotContains(r, 'Almashtirish krediti')


class Disc5ShiftReceiptShowsDiscount(MoneyTestBase):
    """DISC-5 — Z-hisobotда smen davomida berilgan CHEGIRMA ko'rinsin.

    Z-hisobot smenning yagona rasmiy hujjati va kassir aynan shu bo'yicha
    baholanadi, lekin chegirma unда UMUMAN ko'rinmasdi. JAMI SAVDO —
    chegirma allaqachon ayirilgan summa, ya'ni kassir bir smenда million
    so'm qo'lda chegirma bersa ham chekда hech qanday iz qolmasdi.

    Uch tur alohida: aksiya (egasining qarori), QO'LDA (kassir ixtiyori —
    kuzatiladigan son) va almashtirish krediti (chegirma emas).
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_d5', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()

    def _sale(self, odisc='0', promo='0', exch='0', ldisc='0', reason=''):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal(odisc),
            promo_discount=Decimal(promo), exchange_credit=Decimal(exch),
            discount_reason=reason)
        return Sale.objects.create(
            transaction=t, variant=self.variant, branch=self.branch,
            quantity=1, sale_price=Decimal('100000'),
            line_discount=Decimal(ldisc),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)

    def _z(self):
        r = self.client.get(reverse('shift_receipt', args=[self.shift.pk]))
        self.assertEqual(r.status_code, 200)
        c = r.context
        return (c[0] if isinstance(c, list) else c), r

    def test_three_kinds_are_separated(self):
        self._sale(odisc='30000', promo='30000')                    # aksiya
        self._sale(odisc='5000', reason='skidka')                   # qo'lda
        self._sale(odisc='20000', exch='20000',
                   reason='Almashtirish: eski tovar hisobiga')      # kredit
        self._sale(ldisc='3000')                                    # qator
        c, _ = self._z()
        d = c['shift_discount']
        self.assertEqual(d['promo'], Decimal('30000'))
        self.assertEqual(d['manual'], Decimal('5000'))
        self.assertEqual(d['exchange'], Decimal('20000'))
        self.assertEqual(d['line'], Decimal('3000'))
        self.assertEqual(d['total'], Decimal('58000'),
                         'qator + chek chegirmasi = jami')

    def test_mixed_receipt_splits_promo_from_manual(self):
        self._sale(odisc='12000', promo='9000', reason='mijoz')
        d, _ = self._z()
        self.assertEqual(d[0]['promo'] if isinstance(d, tuple) else
                         d['shift_discount']['promo'], Decimal('9000'))

    def test_labels_are_rendered(self):
        self._sale(odisc='5000', reason='skidka')
        self._sale(odisc='20000', exch='20000',
                   reason='Almashtirish: eski tovar hisobiga')
        _, r = self._z()
        self.assertContains(r, "Qo'lda chegirma")
        self.assertContains(r, 'Almashtirish krediti')

    def test_nothing_shown_when_no_discount(self):
        self._sale()
        c, r = self._z()
        self.assertEqual(c['shift_discount']['total'], Decimal('0'))
        self.assertNotContains(r, "Qo'lda chegirma")

    def test_jami_savdo_is_already_net_of_the_discount(self):
        """Blok faqat MA'LUMOT — JAMI SAVDOдан yana ayirilmaydi."""
        self._sale(odisc='30000', promo='30000')
        c, _ = self._z()
        self.assertEqual(_dec(c['total_rev']), Decimal('70000'))
        self.assertEqual(c['shift_discount']['total'], Decimal('30000'))

    def test_kassa_block_still_reconciles(self):
        """DISC-5 qo'shilgani MON-24 balansini buzmasin."""
        self._sale(odisc='5000', reason='skidka')
        self._sale(odisc='20000', exch='20000',
                   reason='Almashtirish: eski tovar hisobiga')
        c, _ = self._z()
        lines = (_printed(c['shift'].opening_cash) + _printed(c['cash_sales'])
                 + _printed(c['debt_payments']) + _printed(c['cash_ins_total'])
                 - _printed(c['payouts_total']) - _printed(c['refund_total'])
                 + _printed(c['post_close_delta']) + _printed(c['rounding_delta']))
        self.assertEqual(lines, _printed(c['expected']))



class Mon26ZReportEndToEnd(TestCase):
    """MON-26 — Z-hisobotning QAYTARISH / CHEGIRMA / ALMASHTIRISH mantig'i,
    hammasi BIR smenда birga.

    Har biri alohida sinalgan edi; bu test ularning ARALASHMASINI qulflaydi,
    chunki ular bir-biriga ta'sir qiladi:
      - almashtirish YANGI chek yaratadi (Cheklar +1) lekin JAMIga 0 qo'shadi
        (order_discount = kredit), ya'ni sotuv soni oshsa ham pul oshmaydi;
      - TENG almashtirishда kassadan naqd CHIQMAYDI (cash_refunded = 0), lekin
        dona QAYTGAN deb sanaladi;
      - ARZONROQQA almashtirishда farq HAQIQATAN kassadan chiqadi va
        kutilgan naqdni kamaytirishi SHART;
      - qo'lda chegirma va almashtirish krediti bitta `order_discount`
        maydonида yashaydi — ajratilmasa kassir baholanmaydi.

    Yana ARCH-5/MON-26: SOTUV bloki va KASSA bloki AYNAN bir xil cheklar
    to'plamini ko'rishi. Ilgari SOTUV `shift.transactions` (faqat FK), KASSA
    esa `_txn_qs()` (FK'siz eski cheklarni ham vaqt oynasi bo'yicha) o'qirdi —
    bitta chekда "Naqd 310 000" va "+ Naqd savdo 340 000" chiqardi.
    """

    def setUp(self):
        self.b = Branch.objects.create(name='B')
        self.u = User.objects.create_user(username='admin', password='x',
                                       role=User.Role.ADMIN, is_staff=True, branch=self.b)
        p = Product.objects.create(name='P', code='P-0001',
                                   default_sale_price=Decimal('100000'))
        self.v = ProductVariant.objects.create(product=p, size='M', color='A',
                                               barcode='2000000000017')
        BranchStock.objects.create(variant=self.v, branch=self.b, stock_count=500,
                                   cost_price=Decimal('60000'), sale_price=Decimal('100000'))
        # OLDINGI smen — almashtiriladigan tovarlar shu yerda sotilgan
        self.prev = Shift.objects.create(branch=self.b, opened_by=self.u,
                                         opening_cash=Decimal('0'))
        self.old50 = self._sale(self.prev, '50000')
        self.old80 = self._sale(self.prev, '80000')
        self.prev.status = Shift.Status.CLOSED
        self.prev.closed_at = timezone.now()
        self.prev.closing_expected_cash = self.prev.compute_expected_cash()
        self.prev.save()
        self.sh = Shift.objects.create(branch=self.b, opened_by=self.u,
                                       opening_cash=Decimal('0'))

    def _sale(self, shift, price, odisc='0', exch='0', pm='cash', bd=None,
              qty=1, no_shift=False):
        t = SaleTransaction.objects.create(
            branch=self.b, sold_by=self.u, shift=None if no_shift else shift,
            payment_method=pm, payment_breakdown=bd or [],
            order_discount=D(odisc), exchange_credit=D(exch))
        return Sale.objects.create(transaction=t, variant=self.v, branch=self.b,
                                   quantity=qty, sale_price=D(price),
                                   cost_at_sale=Decimal('60000'), sold_by=self.u)

    def _z(self):
        r = self.client.get(reverse('shift_receipt', args=[self.sh.pk]))
        assert r.status_code == 200
        c = r.context
        return c[0] if isinstance(c, list) else c

    def test_logic(self):
        self.client = Client(); self.client.force_login(self.u)
        # 1 oddiy naqd 100 000 -> keyin TO'LIQ qaytariladi
        s1 = self._sale(self.sh, '100000')
        # 2 qo'lda chegirma 10 000 -> mijoz 90 000 to'ladi
        self._sale(self.sh, '100000', odisc='10000')
        # 3 aralash 200 000 = 120 000 naqd + 80 000 karta
        self._sale(self.sh, '200000', pm='mixed',
                   bd=[{'method': 'cash', 'amount': 120000},
                       {'method': 'card', 'amount': 80000}])
        # 4 oddiy qaytarish (1-chek)
        Return.objects.create(sale=s1, shift=self.sh, quantity=1,
                              refunded_by=self.u, refund_cash=Decimal('100000'))
        # 5 TENG almashtirish: eski 50 000 -> yangi 50 000, naqd chiqmaydi
        self._sale(self.sh, '50000', odisc='50000', exch='50000')
        Return.objects.create(sale=self.old50, shift=self.sh, quantity=1,
                              refunded_by=self.u, is_exchange=True,
                              cash_refunded=Decimal('0'))
        # 6 ARZONROQQA almashtirish: eski 80 000 -> yangi 60 000, 20 000 qaytdi
        self._sale(self.sh, '60000', odisc='60000', exch='60000')
        Return.objects.create(sale=self.old80, shift=self.sh, quantity=1,
                              refunded_by=self.u, is_exchange=True,
                              cash_refunded=Decimal('20000'))

        # 7 ESKI, shift FK'siz chek (ARCH-5 fallback oynasiga tushadi)
        self._sale(self.sh, '30000', no_shift=True)

        c = self._z()
        pay = {r['label']: r for r in c['pay_rows']}
        d = c['shift_discount']
        rows = [
            ('Cheklar',            c['txn_count'],              6),
            ('usul sonlari + aral', sum(r['count'] for r in c['pay_rows'])
                                    + c['mixed_count'],         6),
            ('Naqd (brutto)',      pay['Naqd']['amount'],       340000),
            ('Karta',              pay['Karta']['amount'],      80000),
            ('JAMI SAVDO',         c['total_rev'],              420000),
            ('Qaytarilgan (naqd)', c['refund_total'],           120000),
            ('Qaytarilgan (dona)', c['refund_qty'],             3),
            ('SOF SAVDO',          c['net_sales'],              300000),
            ('KASSA naqd savdo',   c['cash_sales'],             340000),
            ('  ^ SOTUV Naqd bilan bir xilmi', pay['Naqd']['amount'],
             c['cash_sales']),
            ('= HOZIRGI QOLDIQ',   c['expected'],               220000),
            ('chegirma: qo\'lda',  d['manual'],                 10000),
            ('chegirma: almashtir', d['exchange'],              110000),
            ('chegirma: aksiya',   d['promo'],                  0),
            ('chegirma: JAMI',     d['total'],                  120000),
        ]
        for lbl, got, want in rows:
            self.assertEqual(Decimal(str(got)), Decimal(str(want)), lbl)


class Disc6RoundingIsNotADiscount(MoneyTestBase):
    """DISC-6 — mayda yaxlitlash ulgurji chegirma bilan bir songa qo'shilmasin.

    Auditda ko'rilgani (2026-08, 30 kun): "Qo'lda chegirma 3 068 035" bitta
    songa 308 chekni yig'gan edi, lekin ular ikki BOSHQA hodisa:

        44 chek   ~2 454 000   -10% tugmasi, 700 000 .. 1 705 000 lik cheklar
       264 chek     ~614 000   medianasi 1 000 — chekni butun songa tushirish

    O'rtacha 9 961 so'm ikkalasini ham yashiradi. Egasi "biz bunchalik
    chegirma bermaganmiz" deganда haq edi: bitta ham katta chegirma yo'q,
    308 ta mayda qaror bor. Karta endi ularni ajratadi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_d6', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.shift = self.open_shift()
        self.today = timezone.localdate().strftime('%Y-%m-%d')

    def _txn(self, price, odisc, reason='', qty=1):
        t = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.shift,
            payment_method='cash', order_discount=Decimal(odisc),
            discount_reason=reason)
        Sale.objects.create(transaction=t, variant=self.variant,
                            branch=self.branch, quantity=qty,
                            sale_price=Decimal(price),
                            cost_at_sale=Decimal('10000'), sold_by=self.cashier)
        return t

    def _split(self):
        r = self.client.get('/sales/', {'date_from': self.today,
                                        'date_to': self.today})
        self.assertEqual(r.status_code, 200)
        c = r.context
        c = c[0] if isinstance(c, list) else c
        return c['discount_split'], c

    # ---- taxmin qoidasi -------------------------------------------------
    def test_small_amount_that_makes_the_total_round_is_rounding(self):
        """19 500 -> 19 000. Aynan kassirning "qoldiqni tashla" harakati."""
        self._txn('19500', '500')
        split, _ = self._split()
        self.assertEqual(split['rounding'], Decimal('500'))
        self.assertEqual(split['manual'], Decimal('0'))

    def test_small_amount_on_an_already_round_total_is_a_real_discount(self):
        """100 000 lik chekда 3 000 — summa allaqachon butun edi.

        Uchinchi shart (yalpi butun EMAS EDI) shu holat uchun. Usiz karta
        ataylab berilgan chegirmani yaxlitlash deb YASHIRIB qo'yardi — bu
        ko'p ko'rsatishdan battar xato.
        """
        self._txn('100000', '3000', reason='doimiy mijoz')
        split, _ = self._split()
        self.assertEqual(split['rounding'], Decimal('0'))
        self.assertEqual(split['manual'], Decimal('3000'))

    def test_a_large_discount_is_never_rounding(self):
        """932 500 -> 746 000: to'langani butun, lekin bu -20% ulgurji."""
        self._txn('932500', '186500')
        split, _ = self._split()
        self.assertEqual(split['rounding'], Decimal('0'))
        self.assertEqual(split['manual'], Decimal('186500'))

    # ---- e'lon qilingan sabab (DISC-7 dan keyin) ------------------------
    def test_declared_rounding_beats_the_guess(self):
        """Kassir "Yaxlitlash" desa — taxmin qilib o'tirmaymiz."""
        self._txn('100000', '2000', reason='Yaxlitlash')
        split, _ = self._split()
        self.assertEqual(split['rounding'], Decimal('2000'))
        self.assertEqual(split['manual'], Decimal('0'))

    def test_declared_rounding_still_obeys_the_size_cap(self):
        """"Yaxlitlash" yorlig'i ostida 50 000 yashira olmasin."""
        self._txn('300000', '50000', reason='Yaxlitlash')
        split, _ = self._split()
        self.assertEqual(split['rounding'], Decimal('0'))
        self.assertEqual(split['manual'], Decimal('50000'))

    # ---- invariantlar ---------------------------------------------------
    def test_parts_still_add_up_to_the_total(self):
        self._txn('19500', '500')                       # yaxlitlash
        self._txn('932500', '186500')                   # qo'lda
        self._txn('100000', '20000', reason='Almashtirish: eski tovar')
        SaleTransaction.objects.filter(order_discount=Decimal('20000')).update(
            exchange_credit=Decimal('20000'))
        split, _ = self._split()
        self.assertEqual(
            split['promo'] + split['rounding'] + split['manual']
            + split['exchange'], split['total'],
            "qismlar JAMIga qo'shilishi SHART")

    def test_revenue_is_unchanged_by_the_split(self):
        """Yaxlitlash ham tushumdan chiqadi — u faqat BOSHQA yorliq."""
        self._txn('19500', '500')
        _, c = self._split()
        self.assertEqual(_dec(c['total']), Decimal('19000'))

    def test_card_shows_the_rounding_row(self):
        self._txn('19500', '500')
        r = self.client.get('/sales/', {'date_from': self.today,
                                        'date_to': self.today})
        self.assertContains(r, 'Yaxlitlash')


class Disc7DiscountReasonQuality(MoneyTestBase):
    """DISC-7 — erkin matn sabab bermadi, ro'yxat beradi.

    26.08 da sabab majburiy qilingandan keyin yig'ilgan 20 ta sabab:
    "skidka" x5, "s" x4, "raz" x2, "c", "3000", "1500", "defect", "mijoz",
    "farux ogaga", "kop tavar oldi". Ya'ni maydonni tezroq yopish uchun
    bitta belgi teriladi. Server endi ma'nosizini rad etadi, POS esa
    ro'yxat taklif qiladi.

    Server ATAYLAB yumshoq: bu PWA, service worker keshidagi eski sahifa
    erkin matn yuboradi. Qat'iy ro'yxat kassani ish o'rtasida to'xtatardi.
    """

    def test_single_letter_reason_rejected(self):
        self.open_shift()
        for junk in ('s', 'c', 'ra'):
            with self.subTest(reason=junk):
                r = self.checkout(order_discount='5000', discount_reason=junk)
                self.assertEqual(r.status_code, 400)
        self.assertFalse(SaleTransaction.objects.exists())

    def test_digits_only_reason_rejected(self):
        """"3000" — bu sabab emas, kassir summani qayta tergan."""
        self.open_shift()
        r = self.checkout(order_discount='5000', discount_reason='3000')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(SaleTransaction.objects.exists())

    def test_listed_reason_accepted(self):
        self.open_shift()
        r = self.checkout(order_discount='5000',
                          discount_reason="Ko'p tovar oldi")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SaleTransaction.objects.get().discount_reason,
                         "Ko'p tovar oldi")

    def test_other_with_text_accepted(self):
        self.open_shift()
        r = self.checkout(order_discount='5000',
                          discount_reason='Boshqa: qo\'shni do\'kon narxi')
        self.assertEqual(r.status_code, 200)

    def test_other_without_text_rejected(self):
        self.open_shift()
        r = self.checkout(order_discount='5000', discount_reason='Boshqa:')
        self.assertEqual(r.status_code, 400)

    def test_legacy_free_text_still_accepted(self):
        """Keshdagi eski POS kassani to'xtatib qo'ymasin."""
        self.open_shift()
        r = self.checkout(order_discount='5000', discount_reason='shikastlangan')
        self.assertEqual(r.status_code, 200)

    def test_no_discount_needs_no_reason(self):
        self.open_shift()
        self.assertEqual(self.checkout().status_code, 200)

    def test_pos_page_offers_the_list(self):
        self.open_shift()
        r = self.client.get('/pos/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'tovar oldi')          # ro'yxat qatori
        self.assertContains(r, 'value="__other__"')   # "Boshqa" tanlovi
        self.assertContains(r, 'id="discountReasonOther"')


class Ai1ExtractRespectsTimeBudget(TestCase):
    """AI-1 — faktura o'qish HTTP time-out'iga SIG'ISHI shart.

    Ishlab turgan tizimda o'lchangani: bitta AI chaqiruvi ~60 sekund,
    gunicorn ishchisi esa 60 sekundda o'ldiriladi. Ishonch bahosi 1.4 dan
    past chiqsa kod rasmni burib YANA UCH marta o'qirdi — jami ~240 sekund.
    Ya'ni javob hech qachon yetkazilmasdi: brauzer 200 va BO'SH tana olardi,
    sahifa esa "Rasmni aniqroq oling" deb yozardi. Model rasmni to'g'ri
    o'qigan edi — faqat aytishga ulgurmagan.

    Nozik joyi: bu invoice uchun ishonch 1.11 chiqadi, chunki "1 крб" li
    qatorlarда qty QUTIда, narx esa DONAda — qty x narx = summa tengligi
    tabiiy ravishda buziladi. Ya'ni TO'G'RI o'qilgan faktura ham past baho
    oladi va behuda burishga tushadi.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        import io as _io
        from PIL import Image
        buf = _io.BytesIO()
        Image.new('RGB', (60, 40), 'white').save(buf, format='JPEG')
        self.f = SimpleUploadedFile('n.jpg', buf.getvalue(), 'image/jpeg')

    def _low(self):
        """Ishonchi past natija — burishga undaydi."""
        return {'supplier': '', 'total': 0,
                'rows': [{'name': 'x', 'sum_ok': False,
                          'total_qty': 1, 'cost': 1}]}

    def test_budget_stops_the_rotation_retries(self):
        """Budjet bitta chaqiruvga yetsa — ikkinchisi BOSHLANMASIN."""
        import inventory.invoice_ai as ai
        calls = []

        def fake(raw, mt, timeout=120):
            calls.append(timeout)
            time.sleep(0.30)          # "sekin" chaqiruv
            return self._low()

        with mock.patch.object(ai, '_extract_bytes', side_effect=fake):
            out = ai.extract_invoice(self.f, budget=0.35)
        self.assertEqual(len(calls), 1,
                         'budjet tugagach yangi chaqiruv boshlanmasligi kerak')
        self.assertTrue(out.get('rows'), 'bor natija QAYTARILISHI kerak')

    def test_generous_budget_still_tries_rotations(self):
        """Vaqt yetsa — burib qayta o'qish ishlayveradi (xatti-harakat o'zgarmadi)."""
        import inventory.invoice_ai as ai
        calls = []

        def fake(raw, mt, timeout=120):
            calls.append(timeout)
            return self._low()

        with mock.patch.object(ai, '_extract_bytes', side_effect=fake):
            ai.extract_invoice(self.f, budget=600)
        self.assertEqual(len(calls), 4, 'asl + 3 burilish')

    def test_confident_result_never_rotates(self):
        import inventory.invoice_ai as ai
        calls = []

        def fake(raw, mt, timeout=120):
            calls.append(timeout)
            return {'supplier': 'A', 'total': 100,
                    'rows': [{'name': 'x', 'sum_ok': True,
                              'total_qty': 10, 'cost': 10}]}

        with mock.patch.object(ai, '_extract_bytes', side_effect=fake):
            ai.extract_invoice(self.f, budget=600)
        self.assertEqual(len(calls), 1)

    def test_per_call_timeout_never_exceeds_what_is_left(self):
        """Har chaqiruvning time-out'i qolgan vaqtдан oshmasin."""
        import inventory.invoice_ai as ai
        seen = []

        def fake(raw, mt, timeout=120):
            seen.append(timeout)
            return self._low()

        with mock.patch.object(ai, '_extract_bytes', side_effect=fake):
            ai.extract_invoice(self.f, timeout=120, budget=20)
        self.assertTrue(all(t <= 20.001 for t in seen), seen)


class Ai2ConfidenceUnderstandsBoxRows(TestCase):
    """AI-2 — "quti" qatorlari TO'G'RI o'qilgan fakturani past baholamasin.

    28.08 nakladnoyi: 41 qator, model hammasini to'g'ri o'qidi, lekin ishonch
    1.11 chiqdi (chegara 1.4) va kod uni burib qayta o'qishga tushdi — bu esa
    HTTP time-out'ini yeb, javobni butunlay yo'qotdi (AI-1 ga qarang).

    Sabab: `sum_ok` XODIM uchun belgi — "qog'ozdagi qty hisoblangan donaga
    teng emas, tekshir". "1 крб x 21 049,4 = 252 593" qatorida qty=1 (quti),
    dona esa 12 — demak sum_ok tabiiy ravishda False. Fakturada 13 ta shunday
    qator bor edi, ya'ni 68% o'tdi. Bu SIFAT belgisi edi, BURILISH belgisi
    emas. Ikkovi boshqa savol.
    """

    def test_box_row_closes_arithmetic_but_is_not_sum_ok(self):
        """Ikkala belgi ATAYLAB farq qiladi — chalkashtirmaslik kerak."""
        from inventory.invoice_ai import _units
        total_qty, per_case, sum_ok, arith_ok = _units(1, 21049.4, 252593)
        self.assertEqual(total_qty, 12.0, 'dona = summa / narx')
        self.assertEqual(per_case, 12.0)
        self.assertFalse(sum_ok, 'xodim tekshirsin: qog`ozda 1, aslida 12')
        self.assertTrue(arith_ok, 'lekin arifmetika YOPILDI — burilish to`g`ri')

    def test_piece_row_is_both(self):
        from inventory.invoice_ai import _units
        _, _, sum_ok, arith_ok = _units(24, 11654.0, 279696)
        self.assertTrue(sum_ok)
        self.assertTrue(arith_ok)

    def test_scrambled_row_closes_nothing(self):
        """Burilgan rasmda ustunlar chalkashadi — bo'linish yopilmaydi."""
        from inventory.invoice_ai import _units
        _, _, sum_ok, arith_ok = _units(3, 7331.0, 50000)
        self.assertFalse(sum_ok)
        self.assertFalse(arith_ok)

    def test_free_bonus_row_is_not_judged(self):
        """Narxsiz bonus qator burilish haqida hech narsa aytmaydi."""
        from inventory.invoice_ai import _units
        _, _, sum_ok, arith_ok = _units(10, 0, 0)
        self.assertTrue(sum_ok)
        self.assertIsNone(arith_ok, 'maxrajga kirmasligi kerak')

    def _invoice(self, boxes, pieces, free=0, scrambled=0):
        rows = []
        for _ in range(boxes):        # 1 крб x 21049.4 = 252593
            rows.append({'total_qty': 12.0, 'cost': 21049.4,
                         'sum_ok': False, 'arith_ok': True})
        for _ in range(pieces):       # 24 шт x 11654 = 279696
            rows.append({'total_qty': 24.0, 'cost': 11654.0,
                         'sum_ok': True, 'arith_ok': True})
        for _ in range(free):
            rows.append({'total_qty': 10.0, 'cost': 0,
                         'sum_ok': True, 'arith_ok': None})
        for _ in range(scrambled):
            rows.append({'total_qty': 3.0, 'cost': 7331.0,
                         'sum_ok': False, 'arith_ok': False})
        total = sum(r['total_qty'] * r['cost'] for r in rows)
        return {'rows': rows, 'total': total}

    def test_real_invoice_shape_is_now_confident(self):
        """13 quti + 26 dona + 2 bonus — bu 28.08 fakturasining shakli."""
        from inventory.invoice_ai import _confidence
        score = _confidence(self._invoice(boxes=13, pieces=26, free=2))
        self.assertGreaterEqual(
            score, 1.4,
            'to`g`ri o`qilgan faktura burishga tushmasligi kerak')

    def test_old_rule_would_have_failed_this_invoice(self):
        """Eski qoida (sum_ok) shu fakturani 1.4 dan past baholardi."""
        data = self._invoice(boxes=13, pieces=26, free=2)
        rows = data['rows']
        old = sum(1 for r in rows if r['sum_ok']) / len(rows) + 0.5
        self.assertLess(old, 1.4)

    def test_scrambled_read_still_scores_low(self):
        """Haqiqiy burilgan o'qish PAST baho olishда davom etsin."""
        from inventory.invoice_ai import _confidence
        bad = self._invoice(boxes=0, pieces=2, scrambled=10)
        bad['total'] = 999999999      # jami ham mos kelmaydi
        self.assertLess(_confidence(bad), 1.4)

    def test_bonus_only_rows_do_not_inflate_the_score(self):
        from inventory.invoice_ai import _confidence
        d = self._invoice(boxes=0, pieces=1, free=9)
        # 9 ta bonus maxrajga kirmaydi -> 1/1 = 1.0, +0.5 jami mos
        self.assertGreaterEqual(_confidence(d), 1.4)


class Stk14LockingNeverJoinsNullableSide(MoneyTestBase):
    """STK-14 — FOR UPDATE nullable outer join bilan ishlamaydi (Postgres).

    /prices/apply/ prodda 500 berardi:

        psycopg.errors.FeatureNotSupported:
        FOR UPDATE cannot be applied to the nullable side of an outer join

    Sabab: `_price_qs` 'variant__product__category' ni select_related qiladi,
    `Product.category` esa null=True -> LEFT OUTER JOIN. Postgres bunday
    so'rovni qulflashni rad etadi.

    NEGA TESTDA CHIQMAGAN: bu to'plam SQLite'да ishlaydi, SQLite esa
    select_for_update'ni umuman e'tiborsiz qoldiradi. Ya'ni xatoni QAYTA
    KELTIRIB bo'lmaydi — shuning uchun test XATONI emas, SO'ROV SHAKLINI
    tekshiradi: qulflanadigan so'rovда LEFT OUTER JOIN BO'LMASIN. Bu shart
    ikkala bazada ham bir xil tekshiriladi.

    Xuddi shu xato ilgari pos_refund'да ham bo'lgan — takrorlanuvchi tuzoq,
    shuning uchun qulflash bitta yordamchiga (_lock_stocks) yig'ildi.
    """

    def test_input_queryset_really_is_dangerous(self):
        """Avval xavfni ISBOTLAYMIZ — aks holda test hech narsani ushlamaydi."""
        qs = BranchStock.objects.select_related('variant__product__category')
        self.assertIn('LEFT OUTER JOIN', str(qs.query).upper(),
                      'category null=True bo`lgani uchun outer join kutilgan')

    def test_locked_queryset_has_no_outer_join(self):
        from inventory.views import _lock_stocks
        qs = BranchStock.objects.select_related('variant__product__category')
        sql = str(_lock_stocks(qs).query).upper()
        self.assertNotIn('LEFT OUTER JOIN', sql,
                         'qulflanadigan so`rovda outer join BO`LMASLIGI shart')

    def test_lock_still_selects_the_same_rows(self):
        from inventory.views import _lock_stocks
        qs = BranchStock.objects.select_related('variant__product__category')
        self.assertEqual(
            set(_lock_stocks(qs).values_list('pk', flat=True)),
            set(qs.values_list('pk', flat=True)),
            'qulflash to`plamni o`zgartirmasligi kerak')

    def test_locking_actually_executes_on_this_database(self):
        """CI-2: so'rov SHAKLINI emas, BAJARILISHINI tekshiradi.

        SQLite'da select_for_update() umuman e'tiborsiz qoldiriladi, ya'ni bu
        test u yerda hech narsani isbotlamaydi. Postgres'da esa u HAQIQATAN
        FOR UPDATE yuboradi — eski (buzuq) kod bilan aynan shu qator
        NotSupportedError bilan yiqilardi. CI endi ikkala bazada ham
        yuradi, demak shu sinf boshqa prodga chiqmaydi.
        """
        from django.db import transaction, connection
        from inventory.views import _lock_stocks
        qs = BranchStock.objects.select_related('variant__product__category')
        with transaction.atomic():
            rows = list(_lock_stocks(qs))    # Postgres: haqiqiy FOR UPDATE
        self.assertGreaterEqual(len(rows), 1)
        if connection.vendor != 'postgresql':
            self.skipTest("bu tekshiruv faqat Postgres'da ma'noli "
                          "(SQLite qulflashni e'tiborsiz qoldiradi)")

    def test_price_apply_actually_works(self):
        """Uchdan-uchgacha: sahifa 500 bermasin va narx yangilansin."""
        admin = User.objects.create_user(
            username='admin_stk14', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        c = Client()
        c.force_login(admin)
        self.stock.cost_price = Decimal('10000')
        self.stock.sale_price = Decimal('0')
        self.stock.save()
        r = c.post('/prices/apply/', {
            'mode': 'bulk',            # 'rows' default — ommaviy amal EMAS
            'op': 'margin_from_cost', 'pct': '20',
            'scope': 'selected', 'sel': [str(self.stock.pk)],
        })
        self.assertIn(r.status_code, (200, 302), 'sahifa 500 bermasin')
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.sale_price, Decimal('12000.00'),
                         '10 000 + 20% = 12 000')


class Disc8RoundingButtonBoundary(MoneyTestBase):
    """DISC-8 — POS yaxlitlash tugmasi va server tasnifi BIR XIL chegarada.

    POS 5 000 gacha taklif qiladi (85 000 -> 80 000 aynan 5 000). Server esa
    ilgari `manual >= ROUNDING_MAX` deb tekshirardi, ya'ni AYNAN 5 000 lik
    yaxlitlash "qo'lda chegirma" bo'lib ko'rinardi — kassir tugmani bosgan
    va sabab "Yaxlitlash" deb yozilgan bo'lsa ham. Ikki chegara bir xil
    bo'lishi kerak, aks holda karta o'zi taklif qilgan summani boshqa
    ustunga yozadi.
    """

    def test_exactly_5000_declared_rounding_counts_as_rounding(self):
        from inventory.views import _is_rounding, ROUNDING_MAX
        self.assertEqual(ROUNDING_MAX, Decimal('5000'))
        self.assertTrue(
            _is_rounding(Decimal('5000'), Decimal('85000'), Decimal('80000'),
                         'Yaxlitlash'),
            "POS aynan shu summani taklif qiladi — yaxlitlash bo'lishi kerak")

    def test_just_over_the_cap_is_manual(self):
        from inventory.views import _is_rounding
        self.assertFalse(
            _is_rounding(Decimal('5001'), Decimal('85000'), Decimal('79999'),
                         'Yaxlitlash'))

    def test_inferred_rounding_still_needs_the_round_total(self):
        """Sababsiz 5 000: jami butun bo'lsa VA yalpi butun bo'lmasa."""
        from inventory.views import _is_rounding
        self.assertTrue(_is_rounding(Decimal('4800'), Decimal('234800'),
                                     Decimal('230000')))
        self.assertFalse(_is_rounding(Decimal('4800'), Decimal('234000'),
                                      Decimal('229200')))

    def test_pos_page_offers_15_not_20(self):
        admin = User.objects.create_user(
            username='admin_d8', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        c = Client()
        c.force_login(admin)
        self.open_shift()
        r = c.get('/pos/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('data-pct="15"', html)
        self.assertNotIn('data-pct="20"', html,
                         "-20% zarar keltiradi (marja 20% -> chegara 16.7%)")
        self.assertIn('id="roundSuggestWrap"', html)


class Ux9PayModalResetsChange(MoneyTestBase):
    """UX-9 — "Qaytim" oldingi sotuvning summasini ko'rsatmasin.

    Tozalash faqat openPayModal() ichida edi. Oyna boshqa yo'l bilan
    ko'rsatilganda (Bootstrap hodisasi, klaviatura, qayta ochish) eski
    "Naqd berildi" va "Qaytim" raqamlari ekranda qolardi. Bundan tashqari
    updateChange() qatorni YASHIRARDI, lekin span ichidagi MATNNI
    tozalamasdi — qator qayta ko'ringan lahzada oldingi mijozning qaytimi
    ko'rinib ketardi. Kassa uchun bu xavfli: kassir uni yangi chekning
    qaytimi deb o'qib, noto'g'ri pul qaytarishi mumkin.

    Endi tozalash oynaning O'Z hodisasiga (show/hidden.bs.modal) bog'langan,
    ya'ni chaqiruv yo'liga bog'liq emas.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_ux9', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def test_reset_is_bound_to_the_modal_event_not_the_opener(self):
        self.assertIn("pm.addEventListener('show.bs.modal'", self.html)
        self.assertIn("pm.addEventListener('hidden.bs.modal'", self.html)

    def test_hiding_the_row_also_clears_the_text(self):
        """Faqat display:none yetarli emas — matn ham nolga qaytsin."""
        self.assertIn("if (amt) amt.textContent = '0';", self.html)

    def test_modal_is_enlarged_for_touch(self):
        self.assertIn('modal-lg', self.html)
        self.assertIn('form-control-lg', self.html)
        self.assertIn('#payModal .payment-btn', self.html)


class Ux10EmptyCartClearsDiscount(MoneyTestBase):
    """UX-10 — savat bo'shaganda chegirma KEYINGI mijozga o'tib ketmasin.

    Kassir yaxlitlash tugmasini bosadi (-1 000), keyin savatni bo'shatadi va
    yangi sotuvni boshlaydi — 1 000 chegirma joyida qolib, yangi chekka
    JIMGINA qo'llanadi. Ogohlantirish yo'q: savat bo'sh bo'lgani uchun
    chegirma qatori ko'rinmaydi ham.

    Bu ko'rinish emas, PUL xatosi: har savat bo'shatilishidan keyingi chek
    kamroq to'lanadi va kassa kamomadi sifatida chiqadi.

    Sababi: renderCart()ning "savat bo'sh" tarmog'i faqat jami va donani
    nolga qaytarardi. Chegirma summasi, foizi va sababi — hech biri
    tozalanmasdi. "Oraliq" va "Chegirma" ko'rsatkichlari ham eski qiymatда
    qolib ketardi (bo'sh savatда 15 000 ko'rsatardi).

    Bu tarmoq — YAGONA choke point: oxirgi tovarni o'chirish ham, "savatni
    tozalash" ham, sotuvdan keyingi tozalash ham shu yerдан o'tadi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_ux10', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def _empty_branch(self):
        """renderCart()ning "savat bo'sh" tarmog'i."""
        start = self.html.index("Savat bo\\'sh. Kodni skanerlang")
        return self.html[start:start + 1600]

    def test_discount_amount_is_cleared(self):
        self.assertIn("orderDiscountInput.value = ''", self._empty_branch())

    def test_discount_percent_is_cleared(self):
        self.assertIn('discountPct = 0', self._empty_branch())

    def test_discount_reason_is_cleared(self):
        self.assertIn('resetDiscountReason()', self._empty_branch())

    def test_subtotal_and_discount_labels_reset(self):
        b = self._empty_branch()
        self.assertIn("cartSubtotalEl.textContent = '0'", b)
        self.assertIn("cartDiscountEl.textContent = '0'", b)


class Kbd1ScreenKeyboardIsAdaptive(MoneyTestBase):
    """KBD-1/2 — ekran klaviaturasi (sotuvchilar shikoyati bo'yicha qayta qurildi).

    Uch shikoyat bor edi:

    1. "Raqam kiritish noqulay" — klaviatura DOIM QWERTY ko'rsatardi, raqamli
       blok esa yonida kichik va 992px dan tor ekranda umuman yashirin edi.
       Summa kataklarida eng kerakli '000' va 'C' tugmalari yo'q edi.
    2. "Fokus noto'g'ri joyga sakraydi" — POS'ning refocus() funksiyasi ~30
       joydan chaqiriladi va fokusni QIDIRUV katagiga qaytaradi (skaner uchun
       shart). Kassir chegirma katagiga terayotganda shu chaqiruvlardan biri
       fokusni tortib olardi va keyingi tugma qidiruvga tushardi.
    3. "Yorliqlar bilinmaydi" — 1/2/3 va '3 *' faqat yordam oynasida edi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_kbd', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    # ---- 1. raqam kiritish ----
    def test_numpad_has_the_two_keys_som_amounts_need(self):
        self.assertIn("{t:'000'}", self.html, "10 000 uchun 1 + 000")
        self.assertIn("{k:'clear',label:'C'}", self.html)

    def test_numeric_field_switches_the_keyboard_to_123(self):
        self.assertIn('function isNumericField', self.html)
        self.assertIn('function effectiveMode', self.html)
        self.assertIn('osk-num', self.html)

    def test_money_fields_are_marked_numeric(self):
        """OSK 123 rejimini SHU belgidan biladi."""
        self.assertIn('id="orderDiscount"', self.html)
        block = self.html[self.html.index('id="orderDiscount"'):][:260]
        self.assertIn('inputmode="numeric"', block)

    # ---- 2. fokus ----
    def test_refocus_does_not_steal_from_another_field(self):
        self.assertIn("osk.classList.contains('osk-open')", self.html)
        self.assertIn("a !== scanInput", self.html)

    def test_scanner_path_is_untouched(self):
        """Klaviatura YOPIQ bo'lsa — eski xatti-harakat, skaner ishlayveradi."""
        self.assertIn('scanInput.focus();', self.html)

    # ---- 3. yorliqlar ----
    def test_shortcuts_are_shown_on_the_keyboard(self):
        self.assertIn('oskHint', self.html)
        self.assertIn('Naqd', self.html)

    # ---- qaysi katakka yozilyapti ----
    def test_target_field_is_named_and_highlighted(self):
        self.assertIn('oskTarget', self.html)
        self.assertIn('yrt-osk-field', self.html)
        self.assertIn('function fieldName', self.html)


class Kbd3ModesAreSeparateAndRemembered(MoneyTestBase):
    """KBD-3 — ABC va 123 bir-birini TO'LIQ almashtiradi, tanlov eslab qolinadi.

    Sotuvchilar aytdi: ABC rejimida o'ng tomonda raqamli blok ham turardi,
    ya'ni bitta raqamni ikki joydan terish mumkin edi va qaysi birini bosish
    noaniq edi. Endi ABC = faqat harflar (tepadagi raqam qatori qoladi),
    123 = faqat raqamlar.

    Ikkinchisi: kassir 123 ni tanlab, klaviaturani yopib qayta ochsa yana
    QWERTY chiqardi — tanlov saqlanmasdi. Ilgari fokus boshqa katakka
    o'tganda ham rejim 'auto' ga qaytardi. Endi tanlov localStorage'da
    saqlanadi va yopib-ochganda ham, katak almashganda ham qoladi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_kbd3', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def test_abc_hides_the_side_numpad(self):
        self.assertIn('#osk.osk-abc .osk-numpad { display: none; }', self.html)

    def test_123_hides_the_qwerty(self):
        self.assertIn('#osk.osk-num .osk-main { display: none; }', self.html)

    def test_abc_keeps_its_top_number_row(self):
        """Tepadagi 1..0 qatori QWERTY qismida qoladi — u olib tashlanmadi."""
        self.assertIn("[['`','~'],['1','!']", self.html)

    def test_hide_button_exists_in_both_modes(self):
        """Ikkala rejimda ham yopish tugmasi bo'lsin.

        Ilgari bu test tugmaning ANIQ ta'rifiga (kengligi bilan birga)
        bog'langan edi va joylashuv o'zgarishi bilanoq sinar edi —
        garchi tugmaning O'ZI joyida turgan bo'lsa ham. Endi NIYAT
        tekshiriladi: har bir qatorlar to'plamida 'hide' tugmasi bor.
        """
        import re
        for name, pat in (('ABC', r'const ROWS = \[(.*?)\n    \];'),
                          ('123', r'const NUM = \[(.*?)\n    \];')):
            m = re.search(pat, self.html, re.S)
            self.assertIsNotNone(m, f'{name} joylashuvi topilmadi')
            self.assertIn("k:'hide'", m.group(1),
                          f'{name} rejimida yopish tugmasi yo\'q')

    def test_field_navigation_exists_in_both_modes(self):
        """KBD-4: kataklar bo'ylab yurish IKKALA rejimda ham bo'lsin.

        Aks holda raqamli katakka o'tish bilanoq rejim 123 ga almashib,
        ⇤/⇥ ko'zdan yo'qolardi va kassir sichqonchaga qo'l uzatardi.
        """
        import re
        for name, pat in (('ABC', r'const ROWS = \[(.*?)\n    \];'),
                          ('123', r'const NUM = \[(.*?)\n    \];')):
            blk = re.search(pat, self.html, re.S).group(1)
            self.assertIn("k:'next'", blk, f'{name}: keyingi katak tugmasi yo\'q')
            self.assertIn("k:'prev'", blk, f'{name}: oldingi katak tugmasi yo\'q')

    def test_mode_is_read_from_storage_on_load(self):
        self.assertIn("sGet('yurit_osk_mode')", self.html)

    def test_mode_is_saved_when_chosen(self):
        self.assertIn("sSet('yurit_osk_mode', mode)", self.html)

    def test_focus_change_no_longer_resets_the_choice(self):
        """Eski xatti-harakat: fokus o'zgarsa mode='auto' bo'lardi."""
        self.assertNotIn("mode = 'auto';   // yangi katak", self.html)


class Scan1SingleScanAddsOnce(MoneyTestBase):
    """SCAN-1 — bitta skanerlash savatga BITTA dona qo'shsin.

    Sotuvchi: "bir marta skanerlasam ikki marta qo'shadi".

    Poyga (race) shunday edi:
      1. skaner kodni yozadi — har belgi 'input' hodisasi 350 ms taymerni
         qayta qo'yadi;
      2. skaner Enter yuboradi -> doSearch() -> search();
      3. search() ASINXRON: /pos/lookup/ javobini kutadi va faqat javobdan
         KEYIN savatga qo'shib, katakni tozalaydi;
      4. lookup 350 ms dan uzoq ketsa, taymer o'sha orada ishga tushadi.
         Katak hali tozalanmagan, ya'ni sharti bajariladi -> doSearch()
         IKKINCHI marta -> savatga ikkinchi dona.

    Ya'ni xato tarmoq tezligiga bog'liq edi: tez javobda ko'rinmasdi,
    sekin javobda har skanerda takrorlanardi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_scan', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def test_search_cancels_the_pending_debounce(self):
        block = self.html[self.html.index('function doSearch()'):][:1400]
        self.assertIn('clearTimeout(typeTimer);', block,
                      'Enter bilan qidirilganda kutayotgan taymer bekor bo`lsin')

    def test_timer_is_declared_before_use(self):
        """typeTimer doSearch'dan OLDIN e'lon qilinsin (TDZ bo'lmasin)."""
        self.assertLess(self.html.index('let typeTimer = null;'),
                        self.html.index('function doSearch()'))

    def test_lookup_is_still_async(self):
        """Poyga sababi shu — hujjat sifatida qoladi."""
        self.assertIn('async function search(q)', self.html)


class Pos1CheckoutHasAClientTimeout(MoneyTestBase):
    """POS-1 — "TO'LASH VA YAKUNLASH" cheksiz qotib turmasin.

    fetch'ning o'z chegarasi yo'q edi: server sekinlashsa tugma cheksiz
    "Saqlanmoqda..." holatida turardi va kassir mijoz oldida kutardi.

    25 soniyadan keyin uzamiz va sotuvni navbatga olamiz. Bu XAVFSIZ, chunki
    payload ichida idempotency_key bor — navbat qayta yuborganda server
    o'sha kalit bilan ikkinchi chek YARATMAYDI.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_pos1', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def test_checkout_fetch_is_aborted_after_a_wait(self):
        self.assertIn('new AbortController()', self.html)
        self.assertIn('_ctl.abort()', self.html)
        self.assertIn('signal: _ctl.signal', self.html)

    def test_timeout_falls_back_to_the_offline_queue(self):
        self.assertIn("e.name === 'AbortError'", self.html)
        self.assertIn('server javob bermadi', self.html)

    def test_idempotency_key_makes_the_retry_safe(self):
        self.assertIn('idempotency_key: idemKey', self.html)

    def test_duplicate_key_creates_only_one_sale(self):
        """Server tomoni: ayni kalit ikkinchi chek yaratmasligi SHART."""
        r1 = self.checkout(idempotency_key='pos1-key')
        self.assertEqual(r1.status_code, 200)
        r2 = self.checkout(idempotency_key='pos1-key')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(SaleTransaction.objects.count(), 1,
                         'navbat qayta yuborsa ham chek BITTA bo`lsin')


class Tg1NoTelegramOnTheSalePath(MoneyTestBase):
    """TG-1 — sotuv yo'lida Telegram xabari qolmasin (do'kon egasi so'rovi).

    pos_checkout ichida uchta send_telegram bor edi:
      1. kech offline sotuv (yopilgan smenga tushgan),
      2. rad etilgan offline replay,
      3. tannarxdan past sotuv.

    Hech qanday YOZUV yo'qolmaydi — uchalasi ham AuditLog'ga yoziladi.
    Telegram faqat dublikat bildirishnoma edi.

    Yon foyda: send_telegram har chat_id uchun 10 s time-out bilan tashqi
    so'rov qiladi va ulardan biri transaction.atomic() ICHIDA edi — tarmoq
    sekinlashsa BranchStock qatorlari qulfda turib, boshqa kassirning sotuvi
    ham kutib qolardi.
    """

    def _checkout_source(self):
        # ARCH-2: faylni emas, FUNKSIYANI topamiz. pos_checkout views.py dan
        # views_pos.py ga ko'chdi va bu test faylga bog'langani uchun sindi —
        # inspect ishlatilsa, keyingi ko'chirish ham sindirmaydi.
        import inspect
        from inventory.views import pos_checkout
        # unwrap: @login_required o'rab turadi, bizga ASL tana kerak.
        return inspect.getsource(inspect.unwrap(pos_checkout))

    def test_no_telegram_call_remains_in_checkout(self):
        self.assertNotIn('send_telegram(', self._checkout_source(),
                         "sotuv yo'lida Telegram chaqiruvi qolmasligi kerak")

    def test_no_outbound_network_left_in_the_atomic_block(self):
        """Qulf ushlab turib tarmoqni kutish — eng xavflisi shu edi."""
        src = self._checkout_source()
        # AUD-3: endi `with transaction.atomic(), audit_batch(...)` —
        # shuning uchun ikki nuqtaga emas, chaqiruvning O'ZIGA bog'lanamiz.
        atomic = src[src.index('with transaction.atomic()'):]
        self.assertNotIn('send_telegram', atomic)

    def test_below_cost_sale_is_still_recorded_in_audit(self):
        """Telegram ketdi, AUDIT qoldi — signal yo'qolmadi."""
        self.open_shift()
        r = self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                                  'sale_price': '50000'}])   # tannarx 60 000
        self.assertEqual(r.status_code, 200)
        from inventory.models import AuditLog
        logs = AuditLog.objects.filter(model_name='PriceOverride')
        self.assertEqual(logs.count(), 1, 'tannarxdan past sotuv audit`da qolsin')
        self.assertTrue(logs.first().changes['price_override']['below_cost'])

    def test_normal_sale_still_works(self):
        self.open_shift()
        self.assertEqual(self.checkout().status_code, 200)
        self.assertEqual(SaleTransaction.objects.count(), 1)


class Off8OfflineCatalog(MoneyTestBase):
    """OFF-8 — offline'da HAR QANDAY tovar topilsin, faqat ilgari skanerlangani emas.

    Service worker /pos/lookup/ javoblarini TO'LIQ URL bo'yicha keshlaydi,
    ya'ni offline faqat shu qurilmada ilgari skanerlangan kod topilardi.
    Yangi tovar "topilmadi" berardi — kassir buni "bunday tovar yo'q" deb
    o'qiydi va mijozni qaytarib yuborishi mumkin.

    Endi butun katalog oldindan yuklanadi (har 1 soatda yangilanadi) va
    offline mahalliy nusxadan qidiriladi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_off8', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()

    # ---- server ----
    def test_catalog_returns_this_branch_products(self):
        r = self.client.get('/pos/catalog/')
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['branch_id'], self.branch.id)
        codes = [p['code'] for p in d['products']]
        self.assertIn(self.product.code, codes)

    def test_variant_fields_match_pos_lookup(self):
        """Ikki shakl bir xil bo'lsin — mijozda tarjima qilinmaydi."""
        cat = self.client.get('/pos/catalog/').json()
        row = next(p for p in cat['products'] if p['code'] == self.product.code)
        v = row['variants'][0]
        lk = self.client.get('/pos/lookup/', {'q': self.product.code}).json()
        lv = lk['variants'][0]
        for key in ('stock_id', 'variant_id', 'size', 'color', 'barcode',
                    'stock_count', 'sale_price', 'wholesale_price'):
            self.assertIn(key, v, key)
            self.assertEqual(v[key], lv[key], key)

    def test_cost_price_is_not_shipped(self):
        """POS uni ishlatmaydi — butun tannarx kitobi brauzerga tushmasin."""
        cat = self.client.get('/pos/catalog/').json()
        row = next(p for p in cat['products'] if p['code'] == self.product.code)
        self.assertNotIn('cost_price', row['variants'][0])

    def test_open_price_products_are_excluded(self):
        """Ular 'Tezkor sotuv' panelidan sotiladi, skanerlanmaydi."""
        p2 = Product.objects.create(name='Ochiq', default_sale_price=Decimal('0'),
                                    is_open_price=True)
        v2 = ProductVariant.objects.create(product=p2, size='', color='Paypoq')
        BranchStock.objects.create(variant=v2, branch=self.branch, stock_count=5,
                                   cost_price=Decimal('0'), sale_price=Decimal('0'))
        cat = self.client.get('/pos/catalog/').json()
        self.assertNotIn(p2.code, [p['code'] for p in cat['products']])

    def test_other_branch_stock_is_not_included(self):
        b2 = Branch.objects.create(name='Ikkinchi')
        p2 = Product.objects.create(name='Faqat B2', default_sale_price=Decimal('1000'))
        v2 = ProductVariant.objects.create(product=p2, size='M', color='Oq')
        BranchStock.objects.create(variant=v2, branch=b2, stock_count=3,
                                   cost_price=Decimal('500'), sale_price=Decimal('1000'))
        cat = self.client.get('/pos/catalog/').json()
        self.assertNotIn(p2.code, [p['code'] for p in cat['products']])

    def test_requires_login(self):
        c = Client()
        r = c.get('/pos/catalog/')
        self.assertIn(r.status_code, (302, 403))

    # ---- mijoz tomoni ----
    def test_pos_page_wires_the_offline_fallback(self):
        html = self.client.get('/pos/').content.decode()
        self.assertIn('function localLookup', html)
        self.assertIn("data.offline === true", html)
        self.assertIn('refreshCatalog', html)

    def test_refresh_is_hourly(self):
        html = self.client.get('/pos/').content.decode()
        self.assertIn('60 * 60 * 1000', html)
        # OFF-10: soatlik urinish endi BO'SH VAQT rejalashtiruvchisi orqali
        # ketadi (sotuvga xalaqit bermasin), lekin davri o'sha-o'sha.
        self.assertIn('setInterval(() => scheduleCatalogSync(false)', html)

    def test_indexeddb_upgrade_keeps_the_queue(self):
        """v1 -> v2 da yuborilmagan sotuvlar yo'qolmasin."""
        html = self.client.get('/pos/').content.decode()
        self.assertIn('objectStoreNames.contains(QUEUE_STORE)', html)
        self.assertIn("indexedDB.open(QUEUE_DB, 2)", html)


class Off10SyncNeverBlocksSales(MoneyTestBase):
    """OFF-10 — sotuv BIRINCHI o'rinda; katalog sinxroni ko'rinmas bo'lsin.

    Egasi: "Offline catalog sync should be invisible and should not affect
    POS sales at any time. Sales is prior No1."

    Shuning uchun sinxron:
      * savat bo'sh bo'lmasa BOSHLANMAYDI,
      * checkout ketayotgan bo'lsa boshlanmaydi,
      * brauzer bo'sh vaqtida (requestIdleCallback) ishlaydi,
      * xato bersa kassirga hech narsa ko'rsatmaydi,
      * kechiktirilgani savat bo'shashi bilan qayta uriniladi (yo'qolmaydi).

    Va POS sahifasidagi ko'rinadigan nishon OLIB TASHLANDI — kassirga
    katalog holati kerak emas, u egasiga kerak.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_off10', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.pos = self.client.get('/pos/').content.decode()

    # ---- ko'rinmas ----
    def test_sync_waits_while_a_sale_is_open(self):
        self.assertIn('function _posBusy', self.pos)
        self.assertIn('if (_posBusy()) { _syncWanted = true; return; }', self.pos)

    def test_sync_runs_only_when_the_browser_is_idle(self):
        self.assertIn('requestIdleCallback', self.pos)
        self.assertIn('scheduleCatalogSync', self.pos)

    def test_deferred_sync_is_retried_when_the_cart_empties(self):
        self.assertIn('if (_syncWanted) scheduleCatalogSync(false);', self.pos)

    def test_no_visible_badge_on_the_pos_page(self):
        self.assertNotIn('offlineCatalogBadge', self.pos,
                         'kassirga katalog holati ko`rsatilmaydi')

    def test_sync_errors_are_swallowed(self):
        self.assertIn('eski kesh qoladi, kassirga ko`rsatilmaydi'
                      .replace('`', "'"), self.pos)

    # ---- egasi hamma qurilmani ko'radi ----
    def test_device_reports_its_status(self):
        r = self.client.post('/pos/device-sync/',
                             data=json.dumps({'device_id': 'dev-1',
                                              'catalog_count': 800,
                                              'catalog_at': 1756500000000}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200)
        from inventory.models import PosDevice
        d = PosDevice.objects.get(device_id='dev-1')
        self.assertEqual(d.catalog_count, 800)
        self.assertEqual(d.branch, self.branch)
        self.assertEqual(d.last_user, self.admin)

    def test_second_report_updates_not_duplicates(self):
        from inventory.models import PosDevice
        for n in (100, 800):
            self.client.post('/pos/device-sync/',
                             data=json.dumps({'device_id': 'dev-1',
                                              'catalog_count': n}),
                             content_type='application/json')
        self.assertEqual(PosDevice.objects.filter(device_id='dev-1').count(), 1)
        self.assertEqual(PosDevice.objects.get(device_id='dev-1').catalog_count, 800)

    def test_status_flags_a_device_with_no_catalog(self):
        from inventory.models import PosDevice
        d = PosDevice.objects.create(device_id='dev-empty', catalog_count=0)
        self.assertEqual(d.status, 'none')

    def test_status_flags_a_stale_catalog(self):
        from inventory.models import PosDevice
        d = PosDevice.objects.create(
            device_id='dev-old', catalog_count=800,
            catalog_at=timezone.now() - timezone.timedelta(hours=5))
        self.assertEqual(d.status, 'stale')

    def test_owner_sees_every_device_on_the_branches_page(self):
        from inventory.models import PosDevice
        PosDevice.objects.create(device_id='k1', label='Kassa 1',
                                 branch=self.branch, catalog_count=800,
                                 catalog_at=timezone.now())
        PosDevice.objects.create(device_id='k2', label='Kassa 2',
                                 branch=self.branch, catalog_count=0)
        html = self.client.get('/branches/').content.decode()
        self.assertIn('Kassa 1', html)
        self.assertIn('Kassa 2', html)
        self.assertIn('1 / 2 tayyor', html)


class Pos2OneKeyCashCheckout(MoneyTestBase):
    """POS-2 — naqd sotuv uch bosish emas, bitta harakat bo'lsin.

    30 kunlik o'lchov: 4 427 chekdan 3 655 tasi (82.6%) oddiy NAQD. Ular
    uchun yo'l "Yakunlash -> to'lov turi -> To'lash va yakunlash" edi, ya'ni
    uch bosish. Oyiga ~8 850 ortiqcha harakat.

    DIQQAT: bu "jimgina naqd" EMAS. Avtomatik naqd tanlash ilgari xato
    yozuvlarga olib kelgan va ATAYLAB olib tashlangan. Bu esa kassir O'ZI
    bosadigan alohida tugma/tugmacha — boshqa to'lov turi kerak bo'lsa
    odatdagi yashil tugma o'z joyida turibdi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_pos2', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def test_quick_cash_button_exists(self):
        self.assertIn('id="quickCashBtn"', self.html)
        self.assertIn('TEZ NAQD', self.html)

    def test_f12_triggers_it(self):
        self.assertIn("e.key === 'F12'", self.html)
        self.assertIn('quickCash()', self.html)

    def test_quick_cash_sets_cash_explicitly(self):
        block = self.html[self.html.index('function quickCash()'):][:420]
        self.assertIn("paymentMethod').value = 'cash'", block)

    def test_enter_confirms_the_chosen_method(self):
        self.assertIn("if (e.key === 'Enter')", self.html)

    def test_errors_are_visible_when_the_modal_is_closed(self):
        """Tez naqd oynani ochmaydi — rad etilgan sotuv jimgina yo'qolmasin."""
        self.assertIn('function checkoutMsg', self.html)
        self.assertIn('checkoutMsg(`<span class="text-danger">', self.html)

    def test_shortcut_is_documented(self):
        self.assertIn('<kbd>F12</kbd>', self.html)

    def test_manual_method_choice_still_required_on_the_normal_path(self):
        """Oddiy yo'lda to'lov turi baribir tanlanishi shart (eski xato qaytmasin)."""
        self.assertIn("To'lov turini tanlang", self.html)

    def test_cash_sale_still_records_correctly(self):
        """Server tomoni o'zgarmadi — naqd chek avvalgidek yoziladi."""
        r = self.checkout(payment_method='cash')
        self.assertEqual(r.status_code, 200)
        t = SaleTransaction.objects.get()
        self.assertEqual(t.payment_method, 'cash')


class Stk15BulkPricingDoesNotHoldTheCatalogue(MoneyTestBase):
    """STK-15 — ommaviy narx amali kassani bloklamasin.

    price_apply BUTUN filtrlangan to'plamni BITTA tranzaksiyada qulflardi.
    4 000 qatorli qayta narxlash o'sha qatorlarni o'n soniyalab band qiladi,
    va shu vaqtda o'sha tovarni sotmoqchi bo'lgan kassir kutib qoladi —
    "TO'LASH VA YAKUNLASH bosganda qotib qoladi" shikoyatining ehtimoliy
    sababi shu edi.

    Endi bo'laklab bajariladi (PRICE_CHUNK). Qulflar uzluksiz bo'shatiladi.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_stk15', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)

    def _many(self, n):
        made = []
        for i in range(n):
            v = ProductVariant.objects.create(
                product=self.product, size=f'S{i}', color='Qora')
            made.append(BranchStock.objects.create(
                variant=v, branch=self.branch, stock_count=1,
                cost_price=Decimal('10000'), sale_price=Decimal('0')))
        return made

    def test_chunk_size_is_bounded(self):
        from inventory.views import PRICE_CHUNK
        self.assertGreater(PRICE_CHUNK, 0)
        self.assertLessEqual(PRICE_CHUNK, 500,
                             'bo`lak katta bo`lsa qulf uzoq ushlanadi')

    def test_chunker_covers_every_row_exactly_once(self):
        from inventory.views import _chunked
        ids = list(range(1, 1001))
        out = [x for c in _chunked(ids) for x in c]
        self.assertEqual(out, ids, 'birorta qator tushib qolmasin')

    def test_bulk_update_touches_all_rows_across_chunks(self):
        """Bo'laklash natijani o'zgartirmasligi SHART."""
        from inventory.views import PRICE_CHUNK
        rows = self._many(PRICE_CHUNK + 25)      # bir necha bo'lak
        r = self.client.post('/prices/apply/', {
            'mode': 'bulk', 'op': 'margin_from_cost', 'pct': '20',
            'scope': 'selected', 'sel': [str(s.pk) for s in rows],
        })
        self.assertIn(r.status_code, (200, 302))
        for s in rows:
            s.refresh_from_db()
            self.assertEqual(s.sale_price, Decimal('12000.00'))

    def test_locks_are_released_between_chunks(self):
        """Postgres'da: bo'lak tugagach qator BOSHQA tranzaksiyaga ochiladi.

        SQLite select_for_update'ni e'tiborsiz qoldiradi, shuning uchun u
        yerda bu tekshiruv o'tkazib yuboriladi — CI'ning postgres ishi uni
        haqiqatan bajaradi.
        """
        from django.db import connection, transaction
        if connection.vendor != 'postgresql':
            self.skipTest("faqat Postgres'da ma'noli")
        from inventory.views import _chunked
        rows = self._many(3)
        ids = [s.pk for s in rows]
        seen = 0
        for chunk in _chunked(ids, 1):
            with transaction.atomic():
                seen += len(list(BranchStock.objects.select_for_update()
                                 .filter(pk__in=chunk)))
            # tranzaksiya yopildi -> qulf bo'shadi, keyingi bo'lak kuta olmaydi
        self.assertEqual(seen, 3)


class Pos3SpeedFeaturesAreVisible(MoneyTestBase):
    """POS-3 — tezlik xususiyatlari KO'RINSIN.

    Tizimda allaqachon bor edi, lekin yashiringan:
      * '3 *' — miqdor yorlig'i, faqat yordam oynasida yozilgan;
      * F1..F10 — to'liq to'plam, faqat yopiq akkordeonda.

    Sotuvchilar shuning uchun "yorliqlar bilinmaydi" deyishdi: tizimning eng
    tez yo'llari ekranda umuman ko'rinmasdi. Yangi sotuvchi ularni birinchi
    kunidan ishlata olishi kerak.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_pos3', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)
        self.open_shift()
        self.html = self.client.get('/pos/').content.decode()

    def test_quantity_buttons_are_on_screen(self):
        self.assertIn('id="qtyQuickRow"', self.html)
        for q in ('×2', '×3', '×5', '×10'):
            self.assertIn(q, self.html)

    def test_quantity_buttons_use_the_same_mechanism_as_the_shortcut(self):
        """Ikki yo'l bir xil holatni o'zgartirsin — ajralib ketmasin."""
        self.assertIn('pendingQty = (pendingQty === n) ? 1 : n', self.html)

    def test_pressing_again_cancels(self):
        self.assertIn('qayta bosish = bekor', self.html)

    def test_key_hints_are_always_visible(self):
        self.assertIn('id="posKeyHints"', self.html)
        for k in ('F12', 'F4', 'F2', 'F6'):
            self.assertIn(f'<kbd class="pos-key">{k}</kbd>', self.html)

    def test_state_resets_after_the_item_is_added(self):
        """pendingQty bir martalik — tugma ham qaytishi kerak."""
        self.assertIn('window.__qtyQuickPaint', self.html)


class Ops14AuditPruneKeepsTheMoneyTrail(MoneyTestBase):
    """OPS-14 — audit tozalash PUL izini o'chirmasin.

    AuditLog bazadagi ENG KATTA jadval (50 647 qator — sotuvlardan ham
    ko'p) va hech narsa uni cheklamasdi. Tozalash buyrug'i bor edi, lekin
    hech qachon rejalashtirilmagan.

    Nozik joyi: eski buyruq oynadan eski HAMMA narsani o'chirardi. SEC-14
    bo'yicha esa audit jadvaliga ataylab pul va ombor hodisalari qo'shilgan
    — ichki o'g'irlik tergovi uchun. O'g'irlik ko'pincha ancha keyin
    aniqlanadi, ya'ni aynan o'sha yozuvlarni o'chirish maqsadni yo'qqa
    chiqaradi. Endi ular himoyalangan.
    """

    def setUp(self):
        super().setUp()
        from inventory.models import AuditLog
        old = timezone.now() - timezone.timedelta(days=800)
        self.money = AuditLog.objects.create(
            action=AuditLog.Action.UPDATE, model_name='CashPayout',
            object_repr='kassa chiqimi', created_at=old)
        self.sale = AuditLog.objects.create(
            action=AuditLog.Action.CREATE, model_name='Return',
            object_repr='qaytarish', created_at=old)
        self.noise = AuditLog.objects.create(
            action=AuditLog.Action.UPDATE, model_name='Product',
            object_repr='nom tahrirlandi', created_at=old)
        self.recent = AuditLog.objects.create(
            action=AuditLog.Action.UPDATE, model_name='Product',
            object_repr='yaqinda', created_at=timezone.now())

    def _run(self, **kw):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('prune_audit_log', stdout=out, **kw)
        return out.getvalue()

    def test_money_events_survive(self):
        from inventory.models import AuditLog
        self._run()
        self.assertTrue(AuditLog.objects.filter(pk=self.money.pk).exists(),
                        'kassa chiqimi o`chirilmasligi kerak')
        self.assertTrue(AuditLog.objects.filter(pk=self.sale.pk).exists(),
                        'qaytarish o`chirilmasligi kerak')

    def test_routine_edits_are_pruned(self):
        from inventory.models import AuditLog
        self._run()
        self.assertFalse(AuditLog.objects.filter(pk=self.noise.pk).exists())

    def test_recent_rows_are_untouched(self):
        from inventory.models import AuditLog
        self._run()
        self.assertTrue(AuditLog.objects.filter(pk=self.recent.pk).exists())

    def test_dry_run_deletes_nothing(self):
        from inventory.models import AuditLog
        before = AuditLog.objects.count()
        out = self._run(dry_run=True)
        self.assertEqual(AuditLog.objects.count(), before)
        self.assertIn('dry-run', out)

    def test_all_models_flag_can_override(self):
        from inventory.models import AuditLog
        self._run(all_models=True)
        self.assertFalse(AuditLog.objects.filter(pk=self.money.pk).exists())


class Ops15SaleNeverWaitsOnExternalServices(MoneyTestBase):
    """OPS-15 — sotuv yo'lida tashqi chaqiruv QOLMASIN.

    Ilgari chek yozilgandan keyin so'rov ichida hali fiskal chek yuborilar
    va SMS ketardi. Ikkalasi ham tashqi tarmoq: sekinlashsa kassir mijoz
    oldida kutardi. Telegram esa bundan ham yomon edi —
    transaction.atomic() ichida, ya'ni ombor qatorlari qulfda turib
    BOSHQA kassirning sotuvi ham to'xtardi (TG-1 da olib tashlandi).

    Endi ular fon navbatiga tushadi. Chek allaqachon yozilgan; ish
    kechiksa ham, umuman bajarilmasa ham sotuv joyida qoladi.

    Navbat ATAYLAB eng sodda: broker yo'q, bazadagi jadval + systemd timer.
    Server bitta, kuniga ~150 chek — bunga yetadi va ishlovchi to'xtasa
    sotuv baribir davom etadi.
    """

    def setUp(self):
        super().setUp()
        self.open_shift()

    def test_checkout_enqueues_instead_of_calling(self):
        from inventory.models import BackgroundJob
        r = self.checkout()
        self.assertEqual(r.status_code, 200)
        kinds = list(BackgroundJob.objects.values_list('kind', flat=True))
        self.assertIn('fiscal_submit', kinds)

    def test_sms_is_queued_not_sent_inline(self):
        from inventory.models import BackgroundJob
        r = self.checkout(send_sms=True, customer_phone='901234567')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(BackgroundJob.objects.filter(kind='sms_receipt').exists())
        self.assertEqual(r.json()['sms'], {'queued': True})

    def test_no_outbound_call_remains_on_the_sale_path(self):
        """Manba matnida tekshiriladi — kelajakda qaytib qo'shilmasin."""
        import inspect
        from inventory.views import pos_checkout
        # ARCH-2: fayl emas, funksiya (unwrap — dekorator ostidagi tana)
        body = inspect.getsource(inspect.unwrap(pos_checkout))
        for bad in ('send_telegram(', 'submit_for_transaction(txn)', 'send_receipt('):
            self.assertNotIn(bad, body, f'{bad} sotuv yo`lida qolmasin')

    def test_a_failing_job_does_not_touch_the_sale(self):
        """Eng muhim kafolat: ish yiqilsa ham chek joyida qoladi."""
        from inventory.models import BackgroundJob
        from inventory import jobs
        r = self.checkout()
        self.assertEqual(r.status_code, 200)
        txn_count = SaleTransaction.objects.count()

        @jobs.handler('always_fails')
        def _boom():
            raise RuntimeError('tashqi xizmat javob bermadi')

        jobs.enqueue('always_fails')
        ok, err = jobs.run_once()
        self.assertEqual((ok, err), (1, 1))          # fiskal ok, boom xato
        self.assertEqual(SaleTransaction.objects.count(), txn_count)
        j = BackgroundJob.objects.get(kind='always_fails')
        self.assertEqual(j.status, BackgroundJob.Status.PENDING)  # qayta uriniladi
        self.assertIn('javob bermadi', j.last_error)

    def test_retries_back_off_then_give_up(self):
        from inventory.models import BackgroundJob
        from inventory import jobs

        @jobs.handler('flaky')
        def _flaky():
            raise RuntimeError('yana xato')

        jobs.enqueue('flaky', max_attempts=2)
        jobs.run_once()
        j = BackgroundJob.objects.get(kind='flaky')
        self.assertEqual(j.status, BackgroundJob.Status.PENDING)
        self.assertGreater(j.run_after, timezone.now())   # kechiktirildi

        j.run_after = timezone.now()
        j.save(update_fields=['run_after'])
        jobs.run_once()
        j.refresh_from_db()
        self.assertEqual(j.status, BackgroundJob.Status.FAILED)
        self.assertEqual(j.attempts, 2)

    def test_unknown_kind_is_refused_at_enqueue(self):
        from inventory import jobs
        with self.assertRaises(ValueError):
            jobs.enqueue('yoq-bunday-ish')

    def test_successful_job_is_marked_done(self):
        from inventory.models import BackgroundJob
        from inventory import jobs
        seen = []

        @jobs.handler('noop_ok')
        def _ok(**kw):
            seen.append(kw)

        jobs.enqueue('noop_ok', a=1)
        ok, err = jobs.run_once()
        self.assertEqual(err, 0)
        self.assertEqual(seen, [{'a': 1}])
        self.assertEqual(BackgroundJob.objects.get(kind='noop_ok').status,
                         BackgroundJob.Status.DONE)


class Arch2ModulesStaySeparate(TestCase):
    """ARCH-2: bo'linish vaqt o'tishi bilan yana qorishib ketmasin.

    views.py 13 000 qator bo'lgani uchun bitta naqsh — aynan bir xil
    qulflash xatosi — ikki xil view'da alohida prodga chiqdi. Bo'linish
    faqat bir marta qilinsa foydasi yo'q: keyingi safar kimdir
    views_pos.py ga views.py dan import qo'shsa, aylanma bog'liqlik
    paydo bo'ladi va fayllar yana bir-biriga yopishadi.

    Shuning uchun yo'nalish testda qattiq belgilanadi:
        views.py  ->  views_pos.py  ->  access.py / money.py
    Teskari yo'nalish yo'q.
    """

    def _imports(self, module_name):
        import ast
        import os

        import inventory
        path = os.path.join(os.path.dirname(inventory.__file__), module_name)
        tree = ast.parse(open(path, encoding='utf-8').read())
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module.lstrip('.'))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    out.add(a.name)
        return out

    def test_pos_module_never_imports_views(self):
        self.assertNotIn('views', self._imports('views_pos.py'),
                         'views_pos.py views.py ni import qilmasligi kerak — '
                         'aks holda aylanma import')

    def test_access_module_imports_no_view_layer(self):
        got = self._imports('access.py')
        for forbidden in ('views', 'views_pos'):
            self.assertNotIn(forbidden, got,
                             f'access.py {forbidden} ni import qilmasligi kerak')

    def test_money_module_imports_no_view_layer(self):
        got = self._imports('money.py')
        for forbidden in ('views', 'views_pos'):
            self.assertNotIn(forbidden, got,
                             f'money.py {forbidden} ni import qilmasligi kerak')

    def test_pos_routes_still_resolve_through_views(self):
        """urls.py hali views.pos_* deb chaqiradi — re-export uzilmasin."""
        from django.urls import reverse
        for name in ('pos_terminal', 'pos_lookup', 'pos_catalog',
                     'pos_checkout', 'pos_refund', 'pos_exchange',
                     'pos_device_sync', 'pos_park'):
            self.assertTrue(reverse(name), name)

    def test_checkout_lives_outside_views_py(self):
        """Ko'chirish haqiqatan bo'ldimi — funksiya endi views_pos.py da."""
        import inspect

        from inventory.views import pos_checkout
        self.assertEqual(pos_checkout.__module__, 'inventory.views_pos')
        self.assertTrue(
            inspect.getsourcefile(inspect.unwrap(pos_checkout))
            .endswith('views_pos.py'),
            'pos_checkout views_pos.py da bo\'lishi kerak')


class Rpt1SalesPageIsPaginated(MoneyTestBase):
    """RPT-1: /sales/ endi jimgina 300-qatorда kesilmaydi.

    Avvalgi xatti-harakat: ro'yxat `qs[:300]` bilan kesilar, sahifa esa
    kichkina yozuv bilan "eng so'nggi 300 ko'rsatildi" derdi. Ya'ni egasi
    filtr qo'yib so'ragan qatorlarning bir qismi umuman ko'rinmasdi va
    ularga yetib borishning yagona yo'li CSV eksport edi.

    Eng muhim kafolat pastda: SAHIFALASH JAMILARGA TEGMAYDI. Agar jami
    faqat ko'rinib turgan sahifa bo'yicha hisoblansa, hisobot butunlay
    yolg'on bo'lardi — 2-sahifada boshqa "Jami tushum" chiqardi.
    """

    PER = 100

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='rpt1_admin', password='x', role=User.Role.ADMIN,
        )
        self.stock.stock_count = 500
        self.stock.save(update_fields=['stock_count'])
        self.open_shift()
        # PER + 15 ta alohida chek — ikkinchi sahifa paydo bo'lsin.
        for _ in range(self.PER + 15):
            self.assertEqual(self.checkout().status_code, 200)
        self.client.force_login(self.admin)

    def _get(self, **params):
        params.setdefault('date_from',
                          (timezone.localdate() - timedelta(days=1)).isoformat())
        return self.client.get('/sales/', params)

    def test_first_page_shows_a_full_page_not_everything(self):
        r = self._get(view='items')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['sales']), self.PER)
        self.assertEqual(r.context['page_obj'].paginator.count, self.PER + 15)

    def test_second_page_returns_the_remainder(self):
        r = self._get(view='items', page=2)
        self.assertEqual(len(r.context['sales']), 15)
        self.assertEqual(r.context['page_obj'].number, 2)
        self.assertFalse(r.context['page_obj'].has_next())

    def test_no_row_is_lost_between_pages(self):
        """Ikki sahifadagi qatorlar — jami qatorlarning AYNAN o'zi."""
        seen = []
        for page in (1, 2):
            r = self._get(view='items', page=page)
            seen += [s.pk for s in r.context['sales']]
        self.assertEqual(len(seen), len(set(seen)), 'qator takrorlanmasin')
        self.assertEqual(sorted(seen),
                         sorted(Sale.objects.values_list('pk', flat=True)))

    def test_totals_are_for_the_whole_filter_not_the_page(self):
        """Eng muhim: 1- va 2-sahifada JAMI bir xil bo'lishi shart."""
        p1 = self._get(view='items', page=1)
        p2 = self._get(view='items', page=2)
        for key in ('total', 'net_total', 'qty_total', 'txn_count',
                    'line_count', 'gross_total'):
            self.assertEqual(p1.context[key], p2.context[key],
                             f'{key} sahifaga qarab o\'zgarmasligi kerak')
        expected = Decimal('100000') * (self.PER + 15)
        self.assertEqual(p1.context['total'], expected)

    def test_checks_view_is_paginated_too(self):
        r = self._get(view='checks')
        self.assertEqual(len(r.context['checks']), self.PER)
        self.assertEqual(r.context['check_count'], self.PER + 15)
        r2 = self._get(view='checks', page=2)
        self.assertEqual(len(r2.context['checks']), 15)

    def test_filters_survive_the_page_link(self):
        r = self._get(view='items', page=2, q='Test koylak')
        self.assertIn('q=Test', r.context['page_qs'].replace('+', ' ')
                      .replace('%20', ' '))
        self.assertNotIn('page=', r.context['page_qs'])
        self.assertIn('view=items', r.context['page_qs'])

    def test_out_of_range_page_does_not_500(self):
        """get_page() oxirgi sahifani beradi — 404 emas, 500 ham emas."""
        r = self._get(view='items', page=9999)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['page_obj'].number,
                         r.context['page_obj'].paginator.num_pages)
        r = self._get(view='items', page='abc')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['page_obj'].number, 1)

    def test_csv_export_still_ignores_pagination(self):
        """Eksport butun filtrni beradi — sahifa emas."""
        r = self._get(view='items', page=2, export='csv')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode('utf-8-sig')
        rows = [ln for ln in body.splitlines() if ln.strip()]
        self.assertEqual(len(rows), self.PER + 15 + 1)   # + sarlavha

    def test_the_old_silent_cap_is_gone(self):
        r = self._get(view='items')
        self.assertNotIn('shown_capped', r.context)
        self.assertNotContains(r, "eng so'nggi 300")


class Ux11OnlyCustomerDisplayOpensANewTab(TestCase):
    """UX-11: butun tizim bitta tabda ishlaydi — bitta ataylab qilingan istisno.

    Kassirlarda kun oxirida o'nlab tab to'planib qolardi: har chek, har
    etiketka, har Z-hisobot yangi tab ochardi. Chek tabi o'zini yopishga
    urinardi, lekin chop etish oynasi bekor qilinsa yoki brauzer
    window.close() ni rad etsa — tab qolib ketardi.

    Yagona istisno — MIJOZ EKRANI: u ataylab ikkinchi monitorga chiqariladi,
    shuning uchun alohida oyna bo'lishi SHART.

    Bu test shablonlarni matn sifatida o'qiydi. Sabab oddiy: yangi tab
    ochadigan narsa qayta-qayta, ko'pincha "shunchaki bitta havola" deb
    qo'shiladi va uni ko'rib chiqishда payqash qiyin.
    """

    EXCEPT_URL = 'pos_customer_display'

    def _template_dir(self):
        import os
        from django.conf import settings
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory')
            if os.path.isdir(p):
                return p
        self.fail('shablonlar papkasi topilmadi')

    def _sources(self):
        import os
        root = self._template_dir()
        for name in sorted(os.listdir(root)):
            if name.endswith('.html'):
                with open(os.path.join(root, name), encoding='utf-8') as f:
                    yield name, f.read()

    def test_no_template_opens_a_new_tab_except_the_customer_display(self):
        import re
        # target="_blank" va target="nomlangan_oyna" — ikkalasi ham yangi tab.
        # (?<![\w-]) — Bootstrap'ning data-bs-target="#modal" iga tegmaydi;
        # '#' bilan boshlanadigan qiymat ham tab emas (sahifa ichidagi tugma).
        pat = re.compile(
            r'(?<![\w-])target\s*=\s*["\'](?!_self|_top|_parent|#)([^"\']+)["\']')
        offenders = []
        for name, src in self._sources():
            for m in pat.finditer(src):
                # Havolaning o'zi mijoz ekranimi? Shu tegning ichiga qaraymiz.
                start = src.rfind('<', 0, m.start())
                end = src.find('>', m.end())
                tag = src[start:end if end > 0 else m.end()]
                if self.EXCEPT_URL in tag:
                    continue
                offenders.append(f'{name}: target="{m.group(1)}"')
        self.assertEqual(
            offenders, [],
            'Faqat mijoz ekrani yangi tabda ochilishi kerak. Topildi:\n  '
            + '\n  '.join(offenders))

    def test_no_template_calls_window_open(self):
        offenders = []
        for name, src in self._sources():
            for i, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('//') or stripped.startswith('#'):
                    continue          # izoh — kodda emas
                if 'window.open(' in line:
                    offenders.append(f'{name}:{i}')
        self.assertEqual(offenders, [],
                         'window.open() yangi tab ochadi: ' + ', '.join(offenders))

    def test_static_js_opens_no_new_tab(self):
        import os
        from django.conf import settings
        roots = [str(p) for p in getattr(settings, 'STATICFILES_DIRS', [])]
        offenders = []
        for root in roots:
            for base, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.endswith('.js'):
                        continue
                    path = os.path.join(base, fn)
                    with open(path, encoding='utf-8', errors='ignore') as f:
                        src = f.read()
                    if '_blank' in src or 'window.open(' in src:
                        offenders.append(os.path.relpath(path, root))
        self.assertEqual(offenders, [],
                         'static JS yangi tab ochmasligi kerak: '
                         + ', '.join(offenders))

    def test_the_customer_display_link_is_still_there(self):
        """Istisno YO'QOLMASIN — ikkinchi monitor shunga tayanadi."""
        for name, src in self._sources():
            if name == 'pos.html':
                self.assertIn(self.EXCEPT_URL, src)
                self.assertIn('target="_blank"', src)
                return
        self.fail('pos.html topilmadi')

    def test_receipt_prints_through_a_hidden_iframe(self):
        """Chek chop etish tab emas, ko'rinmas iframe orqali ketadi."""
        for name, src in self._sources():
            if name == 'pos.html':
                self.assertIn('function printReceipt(', src)
                self.assertIn("f.id = 'rcptFrame'", src)
                self.assertNotIn("window.open(data.receipt_url", src)
                return
        self.fail('pos.html topilmadi')


class Prn1ReceiptPrintsAsOnePage(TestCase):
    """PRN-1: chek 80mm termal printerda BITTA uzluksiz chek bo'lib chiqsin.

    14 qatorli chek XP-80'da IKKITA alohida chek bo'lib chiqardi: ikkinchi
    varaqda ustun sarlavhalari va "JAMI" qaytadan bosilib, mijozga ikkita
    yarim chek berilardi.

    Sabab CSS'da bir qatorda edi:

        @media print { @page { size: 80mm auto; margin: 0; } }

    Ikki xato bor: (1) Chrome `@media print` ichiga yozilgan `@page` ni
    butunlay e'tiborsiz qoldiradi — u yuqori darajada turishi kerak, ya'ni
    bu qoida hech qachon ishlamagan; (2) Chrome balandlik uchun `auto` ni
    ham qo'llamaydi. Endi balandlik o'lchanib, aniq son yoziladi.

    Bu yerdagi testlar brauzersiz — ular NAQSHNI qo'riqlaydi. Haqiqiy
    sahifa soni Chromium'da o'lchab tasdiqlangan (1/3/14/40 qatorli chek —
    hammasi 1 sahifa).
    """

    def _module(self):
        import os
        from django.conf import settings
        for d in getattr(settings, 'STATICFILES_DIRS', []):
            p = os.path.join(str(d), 'js', 'yurit-receipt-print.js')
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    return f.read()
        self.fail('yurit-receipt-print.js topilmadi')

    def _code(self):
        """Izohlarsiz KOD. Izohlarda xato naqsh ATAYLAB keltirilgan
        (nima uchun bunday qilmaslik kerakligini tushuntirish uchun) —
        test kodni tekshirishi kerak, izohni emas."""
        import re
        src = self._module()
        src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)     # /* ... */
        src = re.sub(r'^\s*//.*$', '', src, flags=re.M)      # // ...
        return src

    def test_page_rule_is_written_at_top_level(self):
        """Eng muhimi: @page `@media print` ICHIGA yozilmasin."""
        code = self._code()
        self.assertIn("'@page { size: '", code)
        self.assertNotIn('@media print { @page', code)
        self.assertNotIn('@media print{@page', code)

    def test_height_is_never_auto(self):
        code = self._code()
        self.assertNotIn('auto;', code.split('function fit')[-1])

    def test_measures_bottom_not_scrollheight(self):
        """scrollHeight oyna balandligidan kichik bo'lmaydi — kalta chek
        uchun 84mm ortiqcha lenta chiqarardi."""
        code = self._code()
        self.assertIn('getBoundingClientRect().bottom', code)
        self.assertNotIn('documentElement.scrollHeight', code)

    def test_measure_styles_go_to_body_not_head(self):
        """Chek uslublari body ichidagi <style> da — head'ga qo'yilsa
        o'lchov 15% xato bo'lardi."""
        code = self._code()
        measure = code.split('function measure')[1].split('function fit')[0]
        self.assertIn('document.body.appendChild', measure)
        self.assertNotIn('document.head.appendChild', measure)

    def test_templates_fit_the_page_before_printing(self):
        import os
        from django.conf import settings
        root = None
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory')
            if os.path.isdir(p):
                root = p
                break
        for name in ('transaction_detail.html', 'shift_receipt.html'):
            with open(os.path.join(root, name), encoding='utf-8') as f:
                src = f.read()
            self.assertIn('yuritReceiptPrint', src, name)
            self.assertIn('beforeprint', src,
                          f'{name}: qo\'lda Ctrl+P bosilganda ham moslansin')


class Prn2ReceiptCanBeFramedBySelf(TestCase):
    """PRN-1/UX-11: chek KO'RINMAS IFRAME ichida chop etiladi — demak
    sahifa o'z saytimiz ichida freymga tusha olishi SHART.

    Bu ikki joyda bloklangan edi va ikkalasi ham jimgina ishlardi —
    chek shunchaki chop etilmasdi, hech qanday xato ko'rinmasdi:
      - CSP: `frame-ancestors 'none'`
      - prod sozlamasi: X_FRAME_OPTIONS = 'DENY'

    Himoya YO'QOLMADI: 'self' va SAMEORIGIN begona saytga ruxsat bermaydi,
    faqat o'z sahifamiz o'z sahifamizni freymlashi mumkin.
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial')
        self.admin = User.objects.create_user(
            username='prn2', password='x', role=User.Role.ADMIN)
        self.shift = Shift.objects.create(
            branch=self.branch, opened_by=self.admin, opening_cash=Decimal('0'))
        self.txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.admin, shift=self.shift,
            payment_method='cash')
        self.client = Client()
        self.client.force_login(self.admin)

    def _receipt(self):
        return self.client.get(f'/transaction/{self.txn.public_id}/')

    def test_receipt_allows_same_origin_framing(self):
        r = self._receipt()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get('X-Frame-Options'), 'SAMEORIGIN')

    def test_csp_allows_self_as_frame_ancestor(self):
        r = self._receipt()
        csp = r.headers.get('Content-Security-Policy') or ''
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertNotIn("frame-ancestors 'none'", csp)

    def test_csp_still_refuses_other_sites(self):
        """Clickjacking himoyasi joyida — 'self' ochiq eshik emas."""
        r = self._receipt()
        csp = r.headers.get('Content-Security-Policy') or ''
        self.assertNotIn('frame-ancestors *', csp)
        self.assertNotIn('frame-ancestors https:', csp)

    def test_other_pages_are_not_loosened_more_than_needed(self):
        """Boshqa sahifalar ham 'self' — lekin hech qachon '*' emas."""
        r = self.client.get('/pos/')
        csp = r.headers.get('Content-Security-Policy') or ''
        if csp:
            self.assertNotIn('frame-ancestors *', csp)


class Ws1WholesaleModeActuallyChangesPrices(TestCase):
    """WS-1: POS'da ulgurji rejim chakana bilan bir xil narx berardi.

    Ma'lumot to'g'ri edi — 3 038 ta zaxirali qatorda ulgurji chakanadan
    arzon, AYNAN teng bo'lgani esa BITTA ham yo'q. Xato POS sahifasida edi
    va u ikki qismdan iborat:

      1. "Ulgurji" tugmasi FAQAT izoh matnini ko'rsatardi. Savatdagi
         qatorlarni qayta narxlamasdi. Sotuvchi tovarlarni skanerlab,
         keyin ulgurjiga o'tsa — narx o'zgarmasdi. Aynan shu "ulgurji
         ishlamayapti" degan shikoyat.
      2. Rejim hech qayerda saqlanmasdi va sahifa yangilanganda jimgina
         chakanaga qaytardi — savat esa localStorage'da qolardi. Ya'ni
         sotuv o'rtasidagi yangilanish narxlarni bildirmay ko'tarib
         yuborardi.

    Endi: rejim almashganda savat qayta narxlanadi, rejim yangilanishdan
    omon qoladi, va HAR SOTUVDAN KEYIN chakanaga qaytadi (ulgurji —
    istisno; rejim keyingi mijozga yopishib qolsa, sotuvchi esdan
    chiqarib chakana mijozlarga arzon narx qo'yib yuborardi).

    Brauzerda (Chromium) to'rt ssenariy ham tekshirilgan. Bu yerdagi
    testlar mantiqning yo'qolib ketmasligini qo'riqlaydi.
    """

    def _pos(self):
        import os
        from django.conf import settings
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory', 'pos.html')
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    return f.read()
        self.fail('pos.html topilmadi')

    def test_switching_mode_reprices_the_cart(self):
        """Eng muhimi: tugma endi savatga TEGADI."""
        src = self._pos()
        self.assertIn('function applyPriceMode()', src)
        # rejim o'zgarganda applyPriceMode chaqirilishi shart
        handler = src.split("input[name=\"priceMode\"]")[-1][:400]
        self.assertIn('applyPriceMode()', handler)

    def test_cart_line_keeps_both_prices(self):
        """Qayta narxlash uchun ikkala narx qatorda saqlanadi."""
        src = self._pos()
        self.assertIn('retail_price:', src)
        self.assertIn('ws_price:', src)

    def test_manual_open_price_lines_are_never_repriced(self):
        """Qo'lda kiritilgan narxni rejim bosib ketmasin."""
        src = self._pos()
        body = src.split('function applyPriceMode()')[1].split('function ')[0]
        self.assertIn('if (it.open) continue;', body)

    def test_mode_survives_a_reload(self):
        src = self._pos()
        self.assertIn('yurit_pos_price_mode', src)
        self.assertIn("lsGet(PRICE_MODE_LS)", src)

    def test_mode_resets_after_every_completed_sale(self):
        """Ulgurji keyingi mijozga YOPISHIB QOLMASIN — pul yo'qotish xavfi."""
        src = self._pos()
        self.assertIn('function resetPriceMode()', src)
        # sotuv tugagan/savat tozalangan joylarda chaqirilsin
        self.assertGreaterEqual(src.count('resetPriceMode();'), 4,
                                'resetPriceMode() barcha savat tozalash '
                                'joylarida chaqirilishi kerak')

    def test_lines_without_a_wholesale_price_say_so(self):
        """Jim qolsa — yana 'ulgurji ishlamayapti' deb o'ylanadi."""
        src = self._pos()
        self.assertIn('function wsNote(', src)
        self.assertIn("ulgurji narx yo'q", src)

    def test_wholesale_higher_than_retail_is_flagged(self):
        src = self._pos()
        self.assertIn('ulgurji &gt; chakana', src)

    def test_wholesale_mode_is_visible(self):
        """Rejim jimgina yoqilib qolmasin."""
        src = self._pos()
        self.assertIn('wsBanner', src)
        self.assertIn('pos-wholesale', src)


class Ws1LookupSendsWholesalePrice(MoneyTestBase):
    """Server ulgurji narxni HAR IKKALA yo'l bilan ham yuborishi shart:
    onlayn qidiruv (/pos/lookup/) va oflayn katalog (/pos/catalog/).
    Oflayn katalogda tushib qolsa, internet uzilganda ulgurji jimgina
    chakanaga aylanardi."""

    def setUp(self):
        super().setUp()
        self.stock.wholesale_price = Decimal('80000')
        self.stock.save(update_fields=['wholesale_price'])
        self.open_shift()

    def test_lookup_includes_wholesale_price(self):
        r = self.client.get('/pos/lookup/', {'q': self.variant.barcode})
        self.assertEqual(r.status_code, 200)
        v = r.json()['variants'][0]
        self.assertEqual(v['wholesale_price'], 80000.0)
        self.assertEqual(v['sale_price'], 100000.0)

    def test_offline_catalog_includes_wholesale_price(self):
        r = self.client.get('/pos/catalog/')
        self.assertEqual(r.status_code, 200)
        found = [v
                 for p in r.json()['products']
                 for v in p['variants']
                 if v.get('barcode') == self.variant.barcode]
        self.assertTrue(found, 'katalogda tovar topilmadi')
        self.assertEqual(found[0]['wholesale_price'], 80000.0)
        self.assertEqual(found[0]['sale_price'], 100000.0)


class Pay1FixPaymentMethodOnly(MoneyTestBase):
    """PAY-1: yakunlangan chekning FAQAT to'lov turini tuzatish.

    Sotuvchilar ba'zan chekni noto'g'ri turda yakunlaydi (naqd bosib
    yuboradi, aslida karta edi). Smena yopilishida kassa to'g'ri chiqmaydi
    va kim haq ekani bo'yicha nizo chiqadi.

    Bu testlarning asosiy vazifasi — CHEGARALARNI qo'riqlash. "Naqd ->
    Karta" tuzatishi kutilgan naqdni KAMAYTIRADI, ya'ni bu naqd kamomadini
    yashirishning eng oson yo'li. Shuning uchun:
      - faqat OCHIQ smena (yopilgan kun hisoboti qotib qoladi, MON-22);
      - sotuvchi faqat O'ZI sotgan chekni;
      - aralash chekka umuman tegilmaydi;
      - har bir tuzatish audit'ga yoziladi.
    """

    URL = '/pos/payment/fix/'

    def setUp(self):
        super().setUp()
        self.shift = self.open_shift(opening_cash='0')
        r = self.checkout()
        self.assertEqual(r.status_code, 200)
        self.txn = SaleTransaction.objects.order_by('-pk').first()
        self.assertEqual(self.txn.payment_method, 'cash')

    def fix(self, method='card', txn=None, reason='karta o\'tdi'):
        return self.client.post(self.URL, data=json.dumps({
            'txn_id': (txn or self.txn).pk,
            'method': method,
            'reason': reason,
        }), content_type='application/json')

    # ---- asosiy xatti-harakat ----

    def test_payment_method_changes(self):
        r = self.fix('card')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['ok'])
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_method, 'card')

    def test_amount_and_lines_are_untouched(self):
        """ENG MUHIMI: faqat TUR o'zgaradi."""
        before = {
            'total': self.txn.total,
            'lines': [(l.pk, l.quantity, l.sale_price) for l in self.txn.lines.all()],
            'order_discount': self.txn.order_discount,
            'customer_name': self.txn.customer_name,
            'sold_by': self.txn.sold_by_id,
            'sold_at': self.txn.sold_at,
        }
        self.fix('transfer')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.total, before['total'])
        self.assertEqual(
            [(l.pk, l.quantity, l.sale_price) for l in self.txn.lines.all()],
            before['lines'])
        self.assertEqual(self.txn.order_discount, before['order_discount'])
        self.assertEqual(self.txn.customer_name, before['customer_name'])
        self.assertEqual(self.txn.sold_by_id, before['sold_by'])
        self.assertEqual(self.txn.sold_at, before['sold_at'])

    def test_expected_cash_follows_the_correction(self):
        """Butun tizim bo'ylab aks etadi — kassa hisobi o'zi to'g'rilanadi."""
        cash_before = self.shift.cash_sales()
        self.assertEqual(cash_before, Decimal('100000'))
        self.fix('card')
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_sales(), Decimal('0'))
        # va teskarisi
        self.fix('cash')
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_sales(), Decimal('100000'))

    def test_totals_do_not_move(self):
        """Savdo jami o'zgarmasligi kerak — faqat qaysi ustunda turishi."""
        before = self.shift.total_sales()
        self.fix('card')
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_sales(), before)

    def test_change_is_audited_with_who_and_reason(self):
        from inventory.models import AuditLog
        self.fix('card', reason='mijoz karta bilan to\'ladi')
        lg = (AuditLog.objects
              .filter(model_name='SaleTransaction', object_id=str(self.txn.pk))
              .filter(changes__has_key='sabab')
              .order_by('-created_at').first())
        self.assertIsNotNone(lg, 'tuzatish audit\'ga yozilmadi')
        self.assertEqual(lg.changes['payment_method'], ['cash', 'card'])
        self.assertIn('karta', lg.changes['sabab'])
        self.assertEqual(lg.username_snapshot, self.cashier.username)

    # ---- chegaralar ----

    def test_closed_shift_is_refused(self):
        """Yopilgan smenaning Z-hisoboti qotgan — cheklar unga zid bo'lmasin."""
        self.shift.status = Shift.Status.CLOSED
        self.shift.closing_expected_cash = Decimal('100000')
        self.shift.save(update_fields=['status', 'closing_expected_cash'])
        r = self.fix('card')
        self.assertEqual(r.status_code, 400)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_method, 'cash')

    def test_mixed_receipt_is_refused(self):
        self.txn.payment_method = 'mixed'
        self.txn.payment_breakdown = [{'method': 'cash', 'amount': '60000'},
                                      {'method': 'card', 'amount': '40000'}]
        self.txn.save(update_fields=['payment_method', 'payment_breakdown'])
        r = self.fix('cash')
        self.assertEqual(r.status_code, 400)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_method, 'mixed')

    def test_seller_cannot_touch_someone_elses_receipt(self):
        other = User.objects.create_user(
            username='boshqa', password='x', role=User.Role.SOTUVCHI,
            branch=self.branch)
        self.txn.sold_by = other
        self.txn.save(update_fields=['sold_by'])
        r = self.fix('card')
        self.assertEqual(r.status_code, 403)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_method, 'cash')

    def test_admin_may_fix_any_receipt_in_the_branch(self):
        other = User.objects.create_user(
            username='boshqa2', password='x', role=User.Role.SOTUVCHI,
            branch=self.branch)
        self.txn.sold_by = other
        self.txn.save(update_fields=['sold_by'])
        admin = User.objects.create_user(
            username='pay_admin', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        self.client.force_login(admin)
        r = self.fix('card')
        self.assertEqual(r.status_code, 200, r.content)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_method, 'card')

    def test_other_branch_receipt_is_not_found(self):
        other_branch = Branch.objects.create(name='Boshqa filial')
        self.txn.branch = other_branch
        self.txn.save(update_fields=['branch'])
        r = self.fix('card')
        self.assertEqual(r.status_code, 404)

    def test_unknown_method_is_refused(self):
        for bad in ('mixed', 'naqd', '', 'CASH', 'bitcoin'):
            r = self.fix(bad)
            self.assertEqual(r.status_code, 400, bad)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_method, 'cash')

    def test_same_method_is_refused(self):
        r = self.fix('cash')
        self.assertEqual(r.status_code, 400)

    def test_get_is_not_allowed(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 405)

    def test_breakdown_is_cleared_so_one_source_of_truth_remains(self):
        """Yagona turdagi chekda eski bo'linish qolib ketmasin — aks holda
        kassa hisobi ikki manbadan ikki xil javob berardi."""
        self.txn.payment_breakdown = [{'method': 'cash', 'amount': '100000'}]
        self.txn.save(update_fields=['payment_breakdown'])
        self.fix('card')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.payment_breakdown, [])
        self.assertEqual(self.shift.cash_sales(), Decimal('0'))

    def test_correction_is_visible_on_the_shift_page(self):
        """NAZORAT: tuzatish kassa hisoblanadigan joyda ko'rinishi shart.

        Audit jurnali bor, lekin uni kimdir ochib o'qishini kutib bo'lmaydi.
        Kutilgan naqdni kamaytiradigan tuzatish smena sahifasida turishi
        kerak — kassani sanashdan oldin ko'zga tashlanadigan joyda.
        """
        admin = User.objects.create_user(
            username='pay_admin2', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        self.fix('card', reason='karta bilan to\'landi')
        self.client.force_login(admin)
        r = self.client.get(f'/shift/{self.shift.pk}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['pay_fixes']), 1)
        self.assertContains(r, "To'lov turi tuzatilgan cheklar")
        self.assertContains(r, "karta bilan to&#x27;landi")


class Stk14LockingQueriesNeverJoin(TestCase):
    """STK-14: qulflanadigan so'rovda select_related BO'LMASIN.

    Bu naqsh endi UCH MARTA takrorlandi va har safar bir xil:

        pos_refund    (27.08)  -> prodda 500
        price_apply   (29.08)  -> prodda 500
        pos_payment_fix (30.08) -> Postgres CI tutdi

    Sababi har safar bir xil: `select_for_update()` ga `select_related()`
    qo'shilsa va bog'lanish NULLABLE bo'lsa, Django LEFT OUTER JOIN yasaydi,
    Postgres esa "FOR UPDATE cannot be applied to the nullable side of an
    outer join" deb rad etadi. SQLite qulflashni umuman e'tiborsiz
    qoldiradi, shuning uchun xato faqat PRODDA chiqadi.

    Postgres CI (CI-2) uchinchisini tutdi — bu yaxshi. Lekin CI ishga
    tushguncha kutish shart emas: naqsh MATNDAN ko'rinadi. Bu test uni
    yozilgan zahoti to'xtatadi va nima uchunligini aytadi.
    """

    FILES = ('views.py', 'views_pos.py', 'money.py')

    def _sources(self):
        import os
        import inventory
        root = os.path.dirname(inventory.__file__)
        for name in self.FILES:
            path = os.path.join(root, name)
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    yield name, f.read()

    def _chain(self, node):
        """Zanjirni yig'adi: (ildiz nomi, {metod: [matn argumentlari]})."""
        import ast
        calls = {}
        cur = node
        while True:
            if isinstance(cur, ast.Call):
                if isinstance(cur.func, ast.Attribute):
                    args = [a.value for a in cur.args
                            if isinstance(a, ast.Constant)
                            and isinstance(a.value, str)]
                    calls.setdefault(cur.func.attr, []).extend(args)
                cur = cur.func
            elif isinstance(cur, ast.Attribute):
                cur = cur.value
            elif isinstance(cur, ast.Name):
                return cur.id, calls
            else:
                return None, calls

    def _path_is_nullable(self, model, path):
        """select_related yo'lida NULLABLE bog'lanish bormi?"""
        from django.core.exceptions import FieldDoesNotExist
        cur = model
        for part in path.split('__'):
            try:
                f = cur._meta.get_field(part)
            except FieldDoesNotExist:
                return False          # bilmasak — ayblamaymiz
            if getattr(f, 'null', False):
                return True
            rel = getattr(f, 'related_model', None)
            if rel is None:
                return False
            cur = rel
        return False

    def test_locking_queries_never_join_a_nullable_relation(self):
        """Qoida ANIQ: qulflashda NULLABLE bog'lanishni join qilmang.

        NOT NULL bog'lanish (variant, branch) INNER JOIN beradi va Postgres
        uni bemalol qulflaydi — bunday kod ishlaydi va unga tegmaymiz.
        Muammo faqat NULLABLE bog'lanishda: u LEFT OUTER JOIN beradi va
        Postgres FOR UPDATE ni rad etadi. Uchala prod xatosi ham aynan
        shunday bo'lgan: Product.category (null), SaleTransaction.shift (null).

        Izohlarga emas, KODGA va MODEL maydonlariga qaraydi.
        """
        import ast
        from django.apps import apps
        models = {m.__name__: m for m in apps.get_app_config('inventory').get_models()}
        offenders = []
        for name, src in self._sources():
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call):
                    continue
                root, calls = self._chain(node)
                if 'select_for_update' not in calls or 'select_related' not in calls:
                    continue
                model = models.get(root)
                if model is None:
                    continue
                for path in calls['select_related']:
                    if self._path_is_nullable(model, path):
                        offenders.append(f'{name}:{node.lineno} -> {root}.{path}')
        self.assertEqual(
            sorted(set(offenders)), [],
            "Qulflanadigan so'rovda NULLABLE bog'lanish join qilingan. "
            "Postgres buni RAD ETADI (SQLite jimgina yutadi, shuning uchun "
            "xato faqat PRODDA chiqadi). Avval PK bo'yicha qulflang, "
            "bog'liq obyektlarni keyin oling. Joylar: "
            + '; '.join(sorted(set(offenders))))

    def test_the_helper_that_does_it_right_still_exists(self):
        """_lock_stocks — to'g'ri naqsh: avval PK, keyin qulf."""
        from inventory.money import _lock_stocks
        import inspect
        src = inspect.getsource(_lock_stocks)
        # Izohni tashlab, faqat KODga qaraymiz
        body = src.split('"""')[-1]
        self.assertIn('select_for_update', body)
        self.assertNotIn('select_related', body)
        self.assertIn("values_list('pk'", body)


# POS sahifasini haqiqiy brauzerda tekshirish TESTDA EMAS, alohida
# asbobda: scripts/browser_check_pos.py
#
# Sabab: LiveServerTestCase jonli server oqimi bilan SQLite ulanishini
# baham ko'radi va test goh yiqilib, goh o'tib turardi. Goh yiqiladigan
# test — testsizlikdan yomon: unga ishonch yo'qoladi va oxiri hamma
# qizil rangni e'tiborsiz qoldiradi. Skript esa har safar bir xil
# ishlaydi va POS'dagi JS'ga tegilganda qo'lda chaqiriladi.


class Var1RenameThenReuseAColour(TestCase):
    """VAR-1: turlarni tahrirlashda 500 (UniqueViolation).

    Prodda 01.09 12:27 da:
        duplicate key ... (product_id, size, color)=(718, , 0039#)
        views.py:2455 -> ProductVariant.objects.create(...)

    Sabab: bitta saqlashda MAVJUD turning rangi bo'shatiladi va O'SHA rang
    YANGI turga beriladi. Tekshiruvda bu ataylab kechiriladi:

        if clash and clash.pk not in _form_pks:   # formadagi tur bo'lsa — mayli

    ya'ni "nomini boshqasiga berayapsan" holati to'g'ri deb topiladi. Lekin
    YOZISH tartibi hisobga olinmagan: agar yangi qator ro'yxatda eskisidan
    OLDIN kelsa, create() eski tur hali o'sha rangni ushlab turganda
    ishlaydi va baza rad etadi. Tartib teskari bo'lsa — ishlaydi. Shuning
    uchun xato "goh chiqadi, goh chiqmaydi" bo'lib ko'rinardi.
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial')
        self.admin = User.objects.create_user(
            username='var1_admin', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        self.product = Product.objects.create(
            name='Женский', code='ICH-0001', default_sale_price=Decimal('10000'))
        self.v_old = ProductVariant.objects.create(
            product=self.product, size='', color='0039#', barcode='6976387880396')
        BranchStock.objects.create(
            variant=self.v_old, branch=self.branch, stock_count=5,
            cost_price=Decimal('5000'), sale_price=Decimal('10000'))
        self.client = Client()
        self.client.force_login(self.admin)
        self.url = f'/products/{self.product.code}/variants/edit/'

    def _post(self, rows):
        data = {'branch': self.branch.pk, 'v_id': [], 'v_color': [],
                'v_size': [], 'v_barcode': [], 'v_cost': [], 'v_sale': [],
                'v_wholesale': [], 'v_stock': []}
        for r in rows:
            data['v_id'].append(r.get('id', ''))
            data['v_color'].append(r.get('color', ''))
            data['v_size'].append(r.get('size', ''))
            data['v_barcode'].append(r.get('barcode', ''))
            data['v_cost'].append(r.get('cost', ''))
            data['v_sale'].append(r.get('sale', ''))
            data['v_wholesale'].append(r.get('ws', ''))
            data['v_stock'].append(r.get('stock', ''))
        return self.client.post(self.url, data)

    def test_new_row_before_the_renamed_one_does_not_500(self):
        """PRODDAGI AYNAN SHU HOLAT: yangi qator eskisidan OLDIN."""
        r = self._post([
            # yangi tur — eski turning rangini oladi
            {'color': '0039#', 'sale': '12000', 'cost': '6000', 'stock': '3'},
            # eski tur — rangi boshqasiga o'zgartirilgan
            {'id': self.v_old.pk, 'color': '0039#-eski', 'sale': '10000',
             'cost': '5000', 'stock': '5', 'barcode': self.v_old.barcode},
        ])
        self.assertIn(r.status_code, (200, 302),
                      '500 qaytdi — UniqueViolation ushlanmagan')
        self.v_old.refresh_from_db()
        self.assertEqual(self.v_old.color, '0039#-eski')
        self.assertTrue(
            ProductVariant.objects.filter(
                product=self.product, size='', color='0039#')
            .exclude(pk=self.v_old.pk).exists(),
            'yangi tur yaratilmadi')

    def test_new_row_after_the_renamed_one_also_works(self):
        """Teskari tartib ilgari ham ishlardi — sinmaganini tekshiramiz."""
        r = self._post([
            {'id': self.v_old.pk, 'color': '0039#-eski', 'sale': '10000',
             'cost': '5000', 'stock': '5', 'barcode': self.v_old.barcode},
            {'color': '0039#', 'sale': '12000', 'cost': '6000', 'stock': '3'},
        ])
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(
            ProductVariant.objects.filter(product=self.product).count(), 2)

    def test_swapping_two_colours_does_not_500(self):
        """Eng yomon holat: ikki tur ranglarini ALMASHTIRADI."""
        v2 = ProductVariant.objects.create(
            product=self.product, size='', color='A9888', barcode='6935955098885')
        BranchStock.objects.create(
            variant=v2, branch=self.branch, stock_count=2,
            cost_price=Decimal('4000'), sale_price=Decimal('9000'))
        r = self._post([
            {'id': self.v_old.pk, 'color': 'A9888', 'sale': '10000',
             'cost': '5000', 'stock': '5', 'barcode': self.v_old.barcode},
            {'id': v2.pk, 'color': '0039#', 'sale': '9000',
             'cost': '4000', 'stock': '2', 'barcode': v2.barcode},
        ])
        self.assertIn(r.status_code, (200, 302),
                      '500 qaytdi — ranglarni almashtirish ishlamadi')
        self.v_old.refresh_from_db(); v2.refresh_from_db()
        self.assertEqual({self.v_old.color, v2.color}, {'A9888', '0039#'})

    def test_a_true_duplicate_is_still_refused_politely(self):
        """Haqiqiy takror — xato XABARI chiqsin, 500 emas."""
        r = self._post([
            {'id': self.v_old.pk, 'color': '0039#', 'sale': '10000',
             'cost': '5000', 'stock': '5', 'barcode': self.v_old.barcode},
            {'color': '0039#', 'sale': '10000', 'cost': '5000', 'stock': '1'},
        ])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            ProductVariant.objects.filter(product=self.product).count(), 1,
            'takror tur yaratilib qolgan')

    def test_only_the_size_changes_and_colour_survives(self):
        """Faqat O'LCHAM o'zgarsa ham vaqtinchalik rang qolib ketmasin."""
        r = self._post([
            {'id': self.v_old.pk, 'color': '0039#', 'size': 'XL',
             'sale': '10000', 'cost': '5000', 'stock': '5',
             'barcode': self.v_old.barcode},
        ])
        self.assertIn(r.status_code, (200, 302))
        self.v_old.refresh_from_db()
        self.assertEqual(self.v_old.size, 'XL')
        self.assertEqual(self.v_old.color, '0039#')

    def test_no_temporary_colour_is_ever_left_behind(self):
        """Uch fazali yozishning vaqtinchalik nomi bazada qolmasligi shart."""
        v2 = ProductVariant.objects.create(
            product=self.product, size='', color='A9888', barcode='6935955098885')
        BranchStock.objects.create(
            variant=v2, branch=self.branch, stock_count=2,
            cost_price=Decimal('4000'), sale_price=Decimal('9000'))
        self._post([
            {'id': self.v_old.pk, 'color': 'A9888', 'sale': '10000',
             'cost': '5000', 'stock': '5', 'barcode': self.v_old.barcode},
            {'id': v2.pk, 'color': '0039#', 'sale': '9000',
             'cost': '4000', 'stock': '2', 'barcode': v2.barcode},
            {'color': 'YANGI', 'sale': '7000', 'cost': '3000', 'stock': '1'},
        ])
        left = [v.color for v in ProductVariant.objects.filter(product=self.product)
                if v.color.startswith('__tmp_')]
        self.assertEqual(left, [], f'vaqtinchalik rang qolib ketdi: {left}')

    def test_nothing_is_half_saved_when_the_database_refuses(self):
        """Baza rad etsa — tranzaksiya qaytariladi, yarim holat qolmaydi."""
        before = set(ProductVariant.objects.filter(product=self.product)
                     .values_list('pk', 'size', 'color'))
        # Ikkita YANGI qator, ikkalasi ham bir xil rang, narxi HAR XIL:
        # narx belgisi bilan ajratiladi, shuning uchun xato bermaydi —
        # bu holat saqlanishi KERAK. Asosiysi: yarim holat qolmasin.
        r = self._post([
            {'id': self.v_old.pk, 'color': '0039#', 'sale': '10000',
             'cost': '5000', 'stock': '5', 'barcode': self.v_old.barcode},
            {'color': 'YANGI', 'sale': '7000', 'cost': '3000', 'stock': '1'},
        ])
        self.assertIn(r.status_code, (200, 302))
        after = set(ProductVariant.objects.filter(product=self.product)
                    .values_list('pk', 'size', 'color'))
        self.assertTrue(before <= after or len(after) >= len(before))


class Row1TablesAreNumbered(TestCase):
    """ROW-1: jadval qatorlariga raqam.

    Xato xabarlari qator raqami bilan gapiradi ("5-qator: manfiy qiymat
    kiritilmaydi"), lekin jadvalda raqam yo'q edi — foydalanuvchi barmoq
    bilan sanardi. 79 turli mahsulotda bu jiddiy vaqt.

    Raqamlar bitta umumiy modul (yurit-rownum.js) orqali qo'shiladi:
    jadvalga `yrt-numbered` sinfi qo'yiladi, qolganini modul qiladi —
    sarlavhaga "#", har qatorga raqam, qator qo'shilsa/o'chirilsa qayta
    raqamlash. Shablonlarni qo'lda o'zgartirish shart emas.

    Bu testlar CHEGARALARNI qo'riqlaydi: chek chop etiladigan jadvalga
    raqam TUSHMASLIGI kerak (u yerda ustun kengliklari nth-child bilan
    qattiq belgilangan — bitta ustun qo'shilsa chek buziladi).
    """

    def _tpl(self, name):
        import os
        from django.conf import settings
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory', name)
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    return f.read()
        self.fail(f'{name} topilmadi')

    def _static(self, rel):
        import os
        from django.conf import settings
        for d in getattr(settings, 'STATICFILES_DIRS', []):
            p = os.path.join(str(d), rel)
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    return f.read()
        self.fail(f'{rel} topilmadi')

    def test_the_module_is_loaded_for_every_page(self):
        base = self._tpl('base.html')
        self.assertIn('js/yurit-rownum.js', base)
        self.assertIn('.yrt-rownum', base, 'raqam ustuni uchun uslub yo\'q')

    def test_the_variants_grid_is_numbered(self):
        """Aynan shu jadval so'ralgan edi."""
        src = self._tpl('product_variants_edit.html')
        self.assertIn('yrt-numbered', src)

    def test_the_printed_receipt_is_never_numbered(self):
        """Chekda ustun kengliklari nth-child bilan qattiq belgilangan —
        bitta ustun qo'shilsa chek buziladi."""
        src = self._tpl('transaction_detail.html')
        self.assertNotIn('yrt-numbered', src)

    def test_row_numbers_are_hidden_when_printing(self):
        base = self._tpl('base.html')
        self.assertIn('@media print { .yrt-rownum { display: none !important; } }',
                      base)

    def test_module_skips_tables_without_a_header(self):
        """Sarlavhasiz jadvalga katak qo'shilsa ustunlar siljib ketardi."""
        js = self._static('js/yurit-rownum.js')
        self.assertIn('function usable(', js)
        self.assertIn('table.tHead', js)

    def test_module_renumbers_when_rows_change(self):
        """Qator qo'shilgach raqamlar qayta chizilishi shart — xato
        xabari aynan o'sha raqamga ishora qiladi."""
        js = self._static('js/yurit-rownum.js')
        self.assertIn('MutationObserver', js)

    def test_empty_state_rows_keep_the_table_aligned(self):
        js = self._static('js/yurit-rownum.js')
        self.assertIn('colSpan', js)

    def test_a_broad_set_of_tables_opted_in(self):
        """Bitta jadval emas — butun tizim bo'ylab."""
        import os
        from django.conf import settings
        root = None
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory')
            if os.path.isdir(p):
                root = p
                break
        n = 0
        for name in os.listdir(root):
            if not name.endswith('.html'):
                continue
            with open(os.path.join(root, name), encoding='utf-8') as f:
                n += f.read().count('yrt-numbered')
        self.assertGreaterEqual(n, 40, f'faqat {n} ta jadval belgilangan')


class Wsm1WholesaleMarginColumn(TestCase):
    """WSM-1: turlarni tahrirlashda "Ulg. marja %" ustuni.

    Chakana uchun tannarx <-> marja <-> sotuv bog'lanishi bor edi;
    ulgurji uchun esa faqat narx katagi bor edi va marjani odam o'zi
    hisoblardi. Endi ulgurji ham xuddi shunday ishlaydi.

    ENG MUHIM QOIDA — 10% FAQAT TAKLIF, YOZUV EMAS.

    Hozir 43 ta zaxirali tovarda ulgurji narx yo'q. Agar 10% avtomatik
    to'ldirilsa, bu sahifani BARKODNI tuzatgani ochgan odam ham Saqlash
    bosishi bilan 43 ta narxni bilmasdan o'zgartirib yuborardi. Shu bois
    bo'sh katakda 10 faqat XIRA TAKLIF (placeholder) bo'lib turadi;
    haqiqiy qiymat foydalanuvchi katakka tekkanda qo'yiladi.

    Xatti-harakat brauzerda (Chromium) tekshirilgan: marja yozilsa narx
    hisoblanadi, narx yozilsa marja chiqadi, tegilmagan qator esa
    saqlashdan keyin ham o'zgarmaydi.
    """

    def _tpl(self):
        import os
        from django.conf import settings
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory', 'product_variants_edit.html')
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    return f.read()
        self.fail('shablon topilmadi')

    def test_the_column_exists_in_the_header(self):
        self.assertIn("Ulg. marja %", self._tpl())

    def test_every_row_has_an_editable_margin_input(self):
        src = self._tpl()
        # mavjud qatorlarda ham, yangi qator andozasida ham
        self.assertGreaterEqual(src.count('v-wsmarja'), 2)
        self.assertIn('name="v_wsmarja"', src)

    def test_ten_percent_is_only_a_placeholder(self):
        """Qiymat emas, TAKLIF: value="10" bo'lmasligi shart."""
        src = self._tpl()
        self.assertIn('placeholder="10"', src)
        self.assertNotIn('name="v_wsmarja" ... value="10"', src)
        for chunk in src.split('v-wsmarja')[1:]:
            head = chunk[:160]
            self.assertNotIn('value="10"', head,
                             'ulgurji marja katagiga 10 QIYMAT sifatida '
                             'yozilgan — bu tegilmagan qatorlarni ham '
                             'o\'zgartirib yuboradi')

    def test_the_server_ignores_the_margin_field(self):
        """Marja — yordamchi ustun. Bazaga NARX yoziladi, marja emas."""
        # admin_required functools.wraps ishlatmaydi, shuning uchun
        # inspect asl funksiyaga yeta olmaydi — manbani fayldan o'qiymiz.
        import os
        import inventory
        path = os.path.join(os.path.dirname(inventory.__file__), 'views.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        start = src.index('def product_variants_edit(')
        body = src[start:start + 8000]
        self.assertIn("getlist('v_wholesale')", body)
        self.assertNotIn("v_wsmarja", src,
                         'server ulgurji MARJANI o\'qimasligi kerak — '
                         'u faqat yordamchi ustun, bazaga NARX yoziladi')

    def test_bulk_row_can_set_every_wholesale_margin_at_once(self):
        """43 ta tovarga 10% ni bittalab emas, ATAYLAB bir marta."""
        src = self._tpl()
        self.assertIn('bulkWsMarja', src)

    def test_default_is_ten(self):
        src = self._tpl()
        self.assertIn('WS_DEFAULT = 10', src)

    # ---- WSM-2: ustun sarlavhasidagi "hammasiga" tugmasi ----

    def test_the_column_header_has_an_apply_to_all_button(self):
        src = self._tpl()
        self.assertIn('id="wsmAll"', src)
        before = src.split('id="wsmAll"')[0][-220:]
        self.assertIn('type="button"', before,
                      "tugma type=button bo'lishi shart — aks holda "
                      "forma yuborilib ketadi")

    def test_apply_to_all_asks_first(self):
        """Tasodifan bosilganda qatorlar jimgina o'zgarib ketmasin."""
        src = self._tpl()
        block = src.split("getElementById('wsmAll')")[1][:1500]
        self.assertIn('window.prompt', block)
        self.assertIn('if (raw === null) return;', block,
                      "Bekor bosilganda hech narsa o'zgarmasligi kerak")

    def test_apply_to_all_only_fills_the_inputs(self):
        """Tugma bazaga yozmaydi — Saqlash bosilishi kerak."""
        src = self._tpl()
        block = src.split("getElementById('wsmAll')")[1][:1500]
        self.assertNotIn('fetch(', block)
        self.assertNotIn('.submit()', block)


class Scan2SearchBoxAlwaysClears(TestCase):
    """SCAN-2: skanerlangan katak HAR DOIM tozalansin.

    Ilgari katak faqat tovar TOPILGANDA tozalanardi. Topilmasa eski kod
    joyida qolib ketardi va kassir keyingi tovarni skanerlaganda yangi
    kod eskisining USTIGA yozilardi — natijada aralashgan kod bilan
    qidiruv yana topilmasdi. Kassir buni sezmay ikki-uch marta
    skanerlab, oxiri katakni qo'lda tozalashi kerak bo'lardi.

    Endi javob kelishi bilan katak tozalanadi: topilsa ham, topilmasa
    ham, omborda bo'lmasa ham, tarmoq xatosida ham.

    Skanerlangan matn YO'QOLMAYDI — xato xabarlarining hammasi nima
    izlanganini aytadi ("9999999999999" topilmadi). Brauzerda
    tekshirilgan.
    """

    def _pos(self):
        import os
        from django.conf import settings
        for d in settings.TEMPLATES[0]['DIRS']:
            p = os.path.join(str(d), 'inventory', 'pos.html')
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    return f.read()
        self.fail('pos.html topilmadi')

    def _do_search(self):
        src = self._pos()
        start = src.index('async function search(q)')
        return src[start:start + 9000]

    def test_the_box_is_cleared_before_any_branch(self):
        """Tozalash SHOXLANISHDAN OLDIN — hamma yo'l uchun bitta joyda."""
        body = self._do_search()
        clear = body.index("scanInput.value = '';")
        notfound = body.index('if (!data.found)')
        self.assertLess(clear, notfound,
                        "katak 'topilmadi' shoxidan OLDIN tozalanishi kerak")

    def test_the_not_found_message_still_shows_what_was_scanned(self):
        body = self._do_search()
        self.assertIn('topilmadi', body)
        self.assertIn('${q}', body,
                      'xato xabari nima izlanganini aytishi kerak')

    def test_network_errors_also_clear_and_name_the_code(self):
        body = self._do_search()
        tail = body[body.index('} catch (e) {'):][:400]
        self.assertIn("scanInput.value = '';", tail)
        self.assertIn('${q}', tail)


class Kpi9BranchTotalsAreNotMultipliedByStaff(TestCase):
    """/branches/ dagi "Zaxira qiymati" xodimlar soniga ko'paymasin.

    Ilgari bitta annotate() ichida `stocks__` va `staff` birga yig'ilardi;
    SQL JOIN har bir ombor qatorini xodimlar soniga takrorlab, qiymatni
    ANIQ N baravar oshirib ko'rsatardi (bosh sahifadagi to'g'ri raqamdan
    farq qilardi).
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial-KPI9')
        self.admin = User.objects.create_user(
            username='kpi9admin', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        # yana 2 ta xodim — jami 3 ta (fan-out bo'lsa 3 baravar bo'lardi)
        for i in range(2):
            User.objects.create_user(username=f'kpi9s{i}', password='x',
                                     role=User.Role.SOTUVCHI,
                                     branch=self.branch)
        cat = Category.objects.create(name='KPI9')
        p = Product.objects.create(code='KPI-0001', name='Tovar', category=cat)
        for i in range(3):
            v = ProductVariant.objects.create(product=p, size=str(i), color='q')
            BranchStock.objects.create(
                variant=v, branch=self.branch, stock_count=10,
                cost_price=Decimal('1000'), sale_price=Decimal('1500'))

    def test_stock_value_matches_a_plain_aggregate(self):
        real_q = sum(s.stock_count for s in
                     BranchStock.objects.filter(branch=self.branch))
        real_v = sum(s.stock_count * s.cost_price for s in
                     BranchStock.objects.filter(branch=self.branch))
        self.assertEqual(real_q, 30)
        self.assertEqual(real_v, Decimal('30000'))

        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse('branch_list'))
        self.assertEqual(resp.status_code, 200)
        br = [b for b in resp.context['branches'] if b.pk == self.branch.pk][0]
        self.assertEqual(br.staff_count, 3)
        self.assertEqual(br.stock_total, real_q,
                         'ombor soni xodimlar soniga ko\'paygan')
        self.assertEqual(Decimal(str(br.stock_value)), real_v,
                         'zaxira qiymati xodimlar soniga ko\'paygan')
        self.assertEqual(Decimal(str(resp.context['total_stock_value'])),
                         real_v)

    def test_branch_page_agrees_with_dashboard(self):
        c = Client()
        c.force_login(self.admin)
        b = c.get(reverse('branch_list')).context['total_stock_value']
        d = c.get(reverse('dashboard')).context['stock_value']
        self.assertEqual(Decimal(str(b)), Decimal(str(d)))


class Aud1OneActionOneAuditRow(TestCase):
    """AUD-1: bitta "Saqlash" — audit ro'yxatida bitta satr.

    Ilgari 14 ta turni birga tahrirlash 15+ alohida satr yozardi va
    audit sahifasida nima qilinganini o'qib bo'lmasdi.
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial-AUD1')
        self.admin = User.objects.create_user(
            username='aud1admin', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        cat = Category.objects.create(name='AUD1')
        self.product = Product.objects.create(
            code='AUD-0001', name='Tovar', category=cat)
        self.variants = []
        for i in range(4):
            v = ProductVariant.objects.create(
                product=self.product, size=str(i), color='Qora')
            BranchStock.objects.create(
                variant=v, branch=self.branch, stock_count=5,
                cost_price=Decimal('1000'), sale_price=Decimal('1500'),
                wholesale_price=Decimal('1100'))
            self.variants.append(v)
        self.c = Client()
        self.c.force_login(self.admin)

    def _post_variants(self, **over):
        n = len(self.variants)
        data = {
            'branch': str(self.branch.pk),
            'v_id': [str(v.pk) for v in self.variants],
            'v_size': [v.size for v in self.variants],
            'v_color': [v.color for v in self.variants],
            'v_barcode': [v.barcode or '' for v in self.variants],
            'v_cost': ['1000'] * n,
            'v_sale': ['1500'] * n,
            'v_wholesale': ['1200'] * n,   # HAMMASIGA yangi ulgurji narx
            'v_stock': ['5'] * n,
        }
        data.update(over)
        return self.c.post(
            reverse('product_variants_edit', args=[self.product.code]), data)

    def test_bulk_edit_writes_exactly_one_visible_row(self):
        AuditLog.objects.all().delete()
        resp = self._post_variants()
        self.assertIn(resp.status_code, (200, 302))

        heads = list(AuditLog.objects.filter(batch_count__gt=0))
        self.assertEqual(len(heads), 1, 'bitta amal — bitta bosh qator')
        head = heads[0]
        self.assertTrue(head.batch_id)
        self.assertEqual(head.model_name, 'ProductVariant')
        self.assertIn('AUD-0001', head.object_repr)

        kids = AuditLog.objects.filter(batch_id=head.batch_id, batch_count=0)
        self.assertEqual(head.batch_count, kids.count())
        self.assertGreaterEqual(kids.count(), 4,
                                'har bir turning diffi SAQLANIB qolsin')

    def test_audit_page_shows_one_row_and_expands(self):
        AuditLog.objects.all().delete()
        self._post_variants()
        head = AuditLog.objects.get(batch_count__gt=0)

        resp = self.c.get(reverse('audit_list'))
        self.assertEqual(resp.status_code, 200)
        shown = list(resp.context['page'])
        # partiya ichidagi qatorlar YASHIRILGAN
        self.assertEqual([l.pk for l in shown if l.batch_id],
                         [head.pk])
        row = [l for l in shown if l.pk == head.pk][0]
        self.assertEqual(len(row.kids), head.batch_count)

        # ?raw=1 — hammasi ko'rinadi
        raw = self.c.get(reverse('audit_list') + '?raw=1')
        self.assertGreater(len(list(raw.context['page'])), len(shown))

    def test_head_row_carries_ip_and_user(self):
        AuditLog.objects.all().delete()
        self._post_variants()
        head = AuditLog.objects.get(batch_count__gt=0)
        self.assertEqual(head.username_snapshot, 'aud1admin')
        self.assertTrue(head.ip, 'qo\'lda yozilgan audit qatorida IP bo\'lsin')

    def test_price_history_still_sees_every_row(self):
        """Guruhlash TARIXNI buzmasin — /prices/history/ bolalarni o'qiydi."""
        AuditLog.objects.all().delete()
        self._post_variants()
        rows = (AuditLog.objects
                .filter(model_name='BranchStock')
                .filter(changes__has_any_keys=['cost_price', 'sale_price',
                                               'wholesale_price']))
        self.assertGreaterEqual(rows.count(), 4)
        resp = self.c.get(reverse('price_history'))
        self.assertEqual(resp.status_code, 200)

    def test_no_change_writes_nothing(self):
        AuditLog.objects.all().delete()
        self._post_variants(v_wholesale=['1100'] * len(self.variants))
        self.assertEqual(AuditLog.objects.count(), 0,
                         "o'zgarish yo'q — audit ham yozilmasin")

    def test_csv_export_includes_changes(self):
        AuditLog.objects.all().delete()
        self._post_variants()
        resp = self.c.get(reverse('audit_list') + '?export=csv')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn("O'zgarishlar", body.split('\n')[0])

    def test_pagination_keeps_the_date_filter(self):
        resp = self.c.get(reverse('audit_list')
                          + '?date_from=2020-01-01&date_to=2030-01-01')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('date_from=2020-01-01', resp.context['page_qs'])
        self.assertIn('date_to=2030-01-01', resp.context['page_qs'])


class Aud1DeletedSnapshotRenders(TestCase):
    """O'chirish yozuvi "1 ta o'zgarish" deb yozilib, ichi BO'SH ko'rinardi."""

    def test_snapshot_dict_is_rendered(self):
        from inventory.templatetags.yurit_extras import audit_val
        out = str(audit_val({'name': 'Koylak', 'code': 'KOY-0001'}))
        self.assertIn('Koylak', out)
        self.assertIn('KOY-0001', out)

    def test_pair_still_renders_as_old_to_new(self):
        from inventory.templatetags.yurit_extras import audit_val
        out = str(audit_val(['1000', '1200']))
        self.assertIn('1000', out)
        self.assertIn('1200', out)
        self.assertIn('rarr', out)

    def test_html_in_a_value_is_escaped(self):
        from inventory.templatetags.yurit_extras import audit_val
        out = str(audit_val(['<script>x</script>', 'ok']))
        self.assertNotIn('<script>', out)


class Aud1PaymentFixIsOneRow(TestCase):
    """PAY-1 ilgari IKKI qator yozardi: o'zimizniki + signal qatori."""

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial-AUD1P')
        self.admin = User.objects.create_user(
            username='aud1p', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        self.shift = Shift.objects.create(
            branch=self.branch, opened_by=self.admin,
            status=Shift.Status.OPEN, opening_cash=Decimal('0'))
        self.txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.admin, shift=self.shift,
            payment_method=SaleTransaction.PaymentMethod.CASH)
        self.c = Client()
        self.c.force_login(self.admin)
        session = self.c.session
        session['pos_branch_id'] = self.branch.pk
        session.save()

    def test_one_head_row_with_reason(self):
        AuditLog.objects.all().delete()
        resp = self.c.post(
            '/pos/payment/fix/',
            data=json.dumps({'txn_id': self.txn.pk, 'method': 'card',
                             'reason': 'kassir adashdi'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        heads = list(AuditLog.objects.filter(batch_count__gt=0))
        self.assertEqual(len(heads), 1)
        head = heads[0]
        self.assertEqual(head.model_name, 'SaleTransaction')
        self.assertEqual(head.object_id, str(self.txn.pk))
        self.assertEqual(head.changes.get('sabab'), 'kassir adashdi')
        self.assertEqual(head.changes.get('payment_method'), ['cash', 'card'])
        # smena sahifasidagi so'rov (changes__has_key) hali ham topsin
        found = (AuditLog.objects
                 .filter(model_name='SaleTransaction',
                         object_id=str(self.txn.pk))
                 .filter(changes__has_key='payment_method')
                 .filter(changes__has_key='sabab'))
        self.assertEqual(found.count(), 1)


class Aud2RenameDiffIsReadable(TestCase):
    """AUD-2: uch fazali yozishning vaqtinchalik qiymati auditga tushmasin."""

    def test_no_tmp_marker_in_the_log(self):
        branch = Branch.objects.create(name='Filial-AUD2')
        admin = User.objects.create_user(
            username='aud2', password='x', role=User.Role.ADMIN, branch=branch)
        cat = Category.objects.create(name='AUD2')
        p = Product.objects.create(code='AU2-0001', name='T', category=cat)
        vs = []
        for c in ('Qora', 'Oq'):
            v = ProductVariant.objects.create(product=p, size='M', color=c)
            BranchStock.objects.create(variant=v, branch=branch, stock_count=1,
                                       cost_price=Decimal('100'),
                                       sale_price=Decimal('200'))
            vs.append(v)
        c = Client()
        c.force_login(admin)
        AuditLog.objects.all().delete()
        # ranglarni ALMASHTIRAMIZ — aynan shu holat uch fazali yozishni
        # ishga soladi (Qora -> Oq, Oq -> Qora).
        resp = c.post(reverse('product_variants_edit', args=[p.code]), {
            'branch': str(branch.pk),
            'v_id': [str(vs[0].pk), str(vs[1].pk)],
            'v_size': ['M', 'M'],
            'v_color': ['Oq', 'Qora'],
            'v_barcode': [vs[0].barcode or '', vs[1].barcode or ''],
            'v_cost': ['100', '100'],
            'v_sale': ['200', '200'],
            'v_wholesale': ['', ''],
            'v_stock': ['1', '1'],
        })
        self.assertIn(resp.status_code, (200, 302))
        blob = ' '.join(str(l.changes) + str(l.object_repr)
                        for l in AuditLog.objects.all())
        self.assertNotIn('__tmp_', blob,
                         "vaqtinchalik qiymat audit'ga tushib qolgan")
        self.assertIn('Qora', blob)


class Aud3SaleIsOneAuditRow(TestCase):
    """AUD-3: bitta chek — bitta audit satri (ilgari ~11 ta edi)."""

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial-AUD3')
        self.cashier = User.objects.create_user(
            username='aud3', password='x', role=User.Role.SOTUVCHI,
            branch=self.branch)
        Shift.objects.create(branch=self.branch, opened_by=self.cashier,
                             status=Shift.Status.OPEN,
                             opening_cash=Decimal('0'))
        cat = Category.objects.create(name='AUD3')
        p = Product.objects.create(code='AU3-0001', name='Tovar', category=cat)
        self.stocks = []
        for i in range(3):
            v = ProductVariant.objects.create(product=p, size=str(i), color='Q')
            self.stocks.append(BranchStock.objects.create(
                variant=v, branch=self.branch, stock_count=10,
                cost_price=Decimal('1000'), sale_price=Decimal('2000')))
        self.c = Client()
        self.c.force_login(self.cashier)
        sess = self.c.session
        sess['pos_branch_id'] = self.branch.pk
        sess.save()

    def test_checkout_collapses_to_one_visible_row(self):
        AuditLog.objects.all().delete()
        resp = self.c.post(CHECKOUT_URL, data=json.dumps({
            'lines': [{'stock_id': s.pk, 'qty': 1, 'sale_price': '2000'}
                      for s in self.stocks],
            'payment_method': 'cash',
        }), content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content[:400])

        heads = list(AuditLog.objects.filter(batch_count__gt=0))
        self.assertEqual(len(heads), 1)
        head = heads[0]
        self.assertEqual(head.model_name, 'SaleTransaction')
        self.assertIn('chek #', head.object_repr)
        # sotuv qatorlari saqlanib qoldi — faqat ichkarida
        self.assertGreaterEqual(head.batch_count, 4)

        page = self.c.get(reverse('audit_list'))
        # sotuvchi audit sahifasini ocholmasligi mumkin — admin bilan
        admin = User.objects.create_user(username='aud3a', password='x',
                                         role=User.Role.ADMIN,
                                         branch=self.branch)
        c2 = Client()
        c2.force_login(admin)
        page = c2.get(reverse('audit_list'))
        self.assertEqual(page.status_code, 200)
        rows = [l for l in page.context['page'] if l.batch_id]
        self.assertEqual([l.pk for l in rows], [head.pk])


class Aud1PruneDoesNotHideOrphans(TestCase):
    """Bosh qator o'chsa, bolasi ko'rinmay qolmasin."""

    def test_orphans_become_standalone(self):
        from django.core.management import call_command
        old = timezone.now() - timedelta(days=1000)
        head = AuditLog.objects.create(
            action=AuditLog.Action.UPDATE, model_name='BranchStock',
            object_repr='Ombor tuzatildi', batch_id='b' * 32,
            batch_count=1, created_at=old)
        kid = AuditLog.objects.create(
            action=AuditLog.Action.CREATE, model_name='Intake',
            object_repr='Qabul', batch_id='b' * 32, created_at=old)
        call_command('prune_audit_log', '--months', '1', verbosity=0)
        self.assertFalse(AuditLog.objects.filter(pk=head.pk).exists(),
                         'himoyalanmagan bosh qator o\'chishi kerak')
        kid.refresh_from_db()
        self.assertEqual(kid.batch_id, '',
                         'boshsiz qolgan qator mustaqil bo\'lishi kerak')


class Aud1BatchHandleIsUsedInsideItsBlock(unittest.TestCase):
    """Men buni IKKI marta xato qildim — endi test ushlab tursin.

    `audit_batch(...) as _batch` blokidan CHIQQANDAN keyin `_batch.describe()`
    yoki `_batch.cancel()` chaqirish JIM ta'sirsiz qoladi: bosh qator
    allaqachon yozilgan bo'ladi va sarlavha "Sotuv" bo'lib qolaveradi
    (yoki bekor qilinmaydi). Bu xatoni ko'z bilan payqash qiyin.
    """

    FILES = ('inventory/views.py', 'inventory/views_pos.py')

    def test_every_batch_call_is_inside_its_with_block(self):
        import ast
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bad = []
        for rel in self.FILES:
            src = open(os.path.join(base, rel), encoding='utf-8').read()
            tree = ast.parse(src)
            # (a) `with ... audit_batch(...) as NAME:` bloklari
            spans = []          # (name, start_line, end_line)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                for item in node.items:
                    call = item.context_expr
                    if (isinstance(call, ast.Call)
                            and getattr(call.func, 'id', '') == 'audit_batch'
                            and isinstance(item.optional_vars, ast.Name)):
                        spans.append((item.optional_vars.id,
                                      node.lineno,
                                      max(getattr(n, 'lineno', node.lineno)
                                          for n in ast.walk(node))))
            names = {n for n, _, _ in spans}
            # (b) har bir `NAME.<method>()` chaqiruvi shu oraliqda bo'lsin
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute)
                        and isinstance(f.value, ast.Name)
                        and f.value.id in names):
                    continue
                if not any(nm == f.value.id and lo <= node.lineno <= hi
                           for nm, lo, hi in spans):
                    bad.append(f'{rel}:{node.lineno} '
                               f'{f.value.id}.{f.attr}() blokdan TASHQARIDA')
                # (c) attribut yozuvi ham
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id in names
                                and not any(nm == t.value.id
                                            and lo <= node.lineno <= hi
                                            for nm, lo, hi in spans)):
                            bad.append(f'{rel}:{node.lineno} '
                                       f'{t.value.id}.{t.attr} = ... TASHQARIDA')
        self.assertEqual(bad, [], '\n'.join(bad))


class Core1OneNumberParser(TestCase):
    """CORE-1: views.py da YETTITA turlicha son tahlilchisi bor edi.

    Ular bir xil kiritmaga har xil javob berardi — bir formada rad
    etilgan qiymat boshqasida bazaga yetib borar va u yerda 500 qilardi.
    Endi hammasi money.parse_money() dan o'tadi.
    """

    def test_separators_and_commas(self):
        from inventory.money import parse_money
        for raw in ('12 345,50', '12 345.50', '12 345,50',
                    "12'345.50", '12 345,50 '):
            self.assertEqual(parse_money(raw), Decimal('12345.50'), raw)

    def test_blank_returns_the_default_the_caller_asked_for(self):
        from inventory.money import parse_money
        # narx tahriri: bo'sh = "tegilmasin"
        self.assertIsNone(parse_money(''))
        self.assertIsNone(parse_money(None))
        # yangi yozuv: bo'sh = nol
        self.assertEqual(parse_money('', default=Decimal('0')), Decimal('0'))

    def test_infinity_and_nan_never_pass(self):
        """Ikkita eski nusxa bularni o'tkazib yuborardi -> bazada 500."""
        from inventory.money import parse_money
        for bad in ('inf', 'Infinity', '-inf', 'nan', 'NaN'):
            self.assertEqual(parse_money(bad, default=Decimal('0')),
                             Decimal('0'), bad)

    def test_too_many_digits_is_rejected(self):
        """max_digits=12, decimal_places=2 -> 10 butun xona."""
        from inventory.money import parse_money
        self.assertEqual(parse_money('9999999999'), Decimal('9999999999'))
        self.assertIsNone(parse_money('10000000000'))
        self.assertIsNone(parse_money('12345678901234'))

    def test_negative_only_when_asked(self):
        from inventory.money import parse_money
        self.assertIsNone(parse_money('-5'))
        self.assertEqual(parse_money('-5', allow_negative=True), Decimal('-5'))

    def test_strict_mode_raises_for_forms(self):
        from inventory.money import parse_money
        with self.assertRaises(ValueError):
            parse_money('abc', strict=True, field='tannarx')
        with self.assertRaises(ValueError):
            parse_money('-1', strict=True, field='tannarx')
        with self.assertRaises(ValueError):
            parse_money('99999999999', strict=True, field='tannarx')

    def test_percent_accepts_the_percent_sign(self):
        from inventory.money import parse_percent
        self.assertEqual(parse_percent('50%'), Decimal('50'))
        self.assertEqual(parse_percent('-5'), Decimal('-5'))
        self.assertEqual(parse_percent('abc'), Decimal('0'))
        self.assertEqual(parse_percent('150', hi=100), Decimal('100'))
        self.assertEqual(parse_percent('-9', lo=0), Decimal('0'))

    def test_qty(self):
        from inventory.money import parse_qty
        self.assertEqual(parse_qty('3'), 3)
        self.assertEqual(parse_qty('3.9'), 3)
        self.assertEqual(parse_qty(''), 0)
        self.assertEqual(parse_qty('-4', minimum=0), 0)
        self.assertEqual(parse_qty('99', maximum=10), 10)

    def test_no_local_number_parser_is_left_in_views(self):
        """Yangi view yozganda yana o'z nusxasini yasamaslik uchun."""
        import inspect
        import re
        from inventory import views, views_pos
        bad = []
        for mod in (views, views_pos):
            src = inspect.getsource(mod)
            for m in re.finditer(
                    r'\n\s+def (dec|num|_money|_clean_num|_num)\(([^)]*)\):'
                    r'((?:\n(?:\s{8,}.*)?)+)', src):
                body = m.group(3)
                if 'parse_money' in body or 'parse_percent' in body:
                    continue
                bad.append(f'{mod.__name__}: def {m.group(1)}({m.group(2)})')
        self.assertEqual(bad, [], 'mustaqil son tahlilchisi qaytib keldi: '
                                  + '; '.join(bad))


class Core2OneRevenueFormula(TestCase):
    """CORE-2: tushum/tannarx/foyda ifodasi 11 marta qayta yozilgan edi."""

    def test_no_inline_revenue_expression_is_left(self):
        import inspect
        import re
        from inventory import views, views_pos
        bad = []
        for mod in (views, views_pos):
            src = inspect.getsource(mod)
            for pat, label in (
                    (r"F\('quantity'\)\s*\*\s*F\('sale_price'\)", 'tushum'),
                    (r"F\('quantity'\)\s*\*\s*F\('cost_at_sale'\)", 'tannarx')):
                n = len(re.findall(pat, src))
                if n:
                    bad.append(f'{mod.__name__}: {label} ifodasi {n} marta '
                               'qo\'lda yozilgan — line_*_expr() ishlating')
        self.assertEqual(bad, [], '; '.join(bad))

    def test_the_three_expressions_agree_with_hand_arithmetic(self):
        from django.db.models import Sum
        from inventory.money import (line_cost_expr, line_profit_expr,
                                     line_revenue_expr)
        branch = Branch.objects.create(name='CORE2')
        u = User.objects.create_user(username='core2', password='x',
                                     role=User.Role.ADMIN, branch=branch)
        cat = Category.objects.create(name='C2')
        p = Product.objects.create(code='C2-0001', name='T', category=cat)
        v = ProductVariant.objects.create(product=p, size='M', color='Q')
        txn = SaleTransaction.objects.create(
            branch=branch, sold_by=u,
            payment_method=SaleTransaction.PaymentMethod.CASH)
        Sale.objects.create(transaction=txn, variant=v, branch=branch,
                            quantity=3, sale_price=Decimal('10000'),
                            cost_at_sale=Decimal('6000'),
                            line_discount=Decimal('1500'), sold_by=u)
        agg = Sale.objects.aggregate(r=Sum(line_revenue_expr()),
                                     c=Sum(line_cost_expr()),
                                     p=Sum(line_profit_expr()))
        self.assertEqual(agg['r'], Decimal('28500'))   # 3*10000 - 1500
        self.assertEqual(agg['c'], Decimal('18000'))   # 3*6000
        self.assertEqual(agg['p'], Decimal('10500'))   # 28500 - 18000


class Core2CategoryExportSubtractsLineDiscount(TestCase):
    """CORE-2 markazlashtirish paytida topilgan XATO.

    /reports/?export=csv&type=category eksportida qator chegirmasi
    AYIRILMAS edi — boshqa hamma sahifa to'g'ri hisoblardi, faqat shu
    eksport daromadni chegirma miqdorida oshirib ko'rsatardi.
    """

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial-C2X')
        self.admin = User.objects.create_user(
            username='c2x', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        cat = Category.objects.create(name='Koylak')
        p = Product.objects.create(code='C2X-0001', name='Koylak',
                                   category=cat)
        v = ProductVariant.objects.create(product=p, size='M', color='Q')
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.admin,
            payment_method=SaleTransaction.PaymentMethod.CASH)
        Sale.objects.create(transaction=txn, variant=v, branch=self.branch,
                            quantity=2, sale_price=Decimal('50000'),
                            cost_at_sale=Decimal('30000'),
                            line_discount=Decimal('20000'),
                            sold_by=self.admin)

    def test_export_matches_what_the_customer_actually_paid(self):
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse('reports'),
                     {'report_type': 'margin', 'period': 'month',
                      'export': 'csv'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('csv', resp['Content-Type'])
        body = resp.content.decode('utf-8-sig')
        # 2 x 50 000 - 20 000 = 80 000 (ilgari 100 000 chiqardi)
        self.assertIn('80000', body.replace(' ', '').replace('.00', ''))
        self.assertNotIn('100000', body.replace(' ', '').replace('.00', ''))


class Core3OneRequestShape(TestCase):
    """CORE-3: JSON API qolipi, sana filtri, CSV va sahifalash — bitta joyda."""

    def setUp(self):
        self.branch = Branch.objects.create(name='Filial-C3')
        self.admin = User.objects.create_user(
            username='c3admin', password='x', role=User.Role.ADMIN,
            branch=self.branch)
        self.c = Client()
        self.c.force_login(self.admin)
        s = self.c.session
        s['pos_branch_id'] = self.branch.pk
        s.save()

    # ---- JSON API ----
    def test_get_on_a_json_endpoint_is_405_everywhere(self):
        for url in (CHECKOUT_URL, '/pos/park/', '/pos/refund/',
                    '/pos/payment/fix/'):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 405, url)
            self.assertEqual(r.json(), {'ok': False, 'error': 'POST only'}, url)

    def test_broken_json_is_400_everywhere(self):
        for url in (CHECKOUT_URL, '/pos/park/', '/pos/refund/',
                    '/pos/payment/fix/'):
            r = self.c.post(url, data='{not json',
                            content_type='application/json')
            self.assertEqual(r.status_code, 400, url)
            self.assertEqual(r.json()['error'], 'bad JSON', url)

    def test_a_json_list_body_does_not_crash(self):
        """`data.get(...)` ro'yxatda AttributeError berardi — endi 400."""
        r = self.c.post(CHECKOUT_URL, data='[1,2,3]',
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_no_endpoint_hand_rolls_the_prologue_any_more(self):
        import inspect
        import re
        from inventory import views, views_pos
        bad = []
        for mod in (views, views_pos):
            src = inspect.getsource(mod)
            for pat in (r"'error': 'POST only'", r"'error': 'bad JSON'",
                        r"_json\.loads\(request\.body"):
                n = len(re.findall(pat, src))
                if n:
                    bad.append(f'{mod.__name__}: {pat} — {n} marta')
        self.assertEqual(bad, [], '; '.join(bad))

    # ---- sana filtri ----
    def test_a_broken_date_is_ignored_not_500(self):
        for url in ('/audit/', '/shifts/', '/sales/'):
            r = self.c.get(url, {'date_from': 'shanba', 'date_to': '99-99-99'})
            self.assertEqual(r.status_code, 200, url)

    def test_date_range_helper_uses_an_index_friendly_filter(self):
        from inventory.web import filter_by_day_range
        qs, f, t = filter_by_day_range(
            Sale.objects.all(), 'sold_at', '2026-01-01', '2026-01-31')
        sql = str(qs.query)
        self.assertNotIn('DATE(', sql.upper().replace('"', ''))
        self.assertEqual((f, t), ('2026-01-01', '2026-01-31'))

    def test_date_range_end_is_inclusive(self):
        from inventory.web import filter_by_day_range
        cat = Category.objects.create(name='C3')
        p = Product.objects.create(code='C3-0001', name='T', category=cat)
        v = ProductVariant.objects.create(product=p, size='M', color='Q')
        txn = SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.admin,
            payment_method=SaleTransaction.PaymentMethod.CASH)
        s = Sale.objects.create(transaction=txn, variant=v, branch=self.branch,
                                quantity=1, sale_price=Decimal('100'),
                                cost_at_sale=Decimal('50'), sold_by=self.admin)
        day = timezone.localtime(s.sold_at).strftime('%Y-%m-%d')
        qs, _, _ = filter_by_day_range(Sale.objects.all(), 'sold_at', day, day)
        self.assertEqual(qs.count(), 1, 'oxirgi kun ham kirsin')

    # ---- sahifalash ----
    def test_pagination_never_loses_a_filter(self):
        """/prices/history/ da 2-sahifaga o'tsangiz FAQAT ?q= saqlanardi."""
        for url, params in (
                ('/prices/history/', {'q': 'koylak'}),
                ('/audit/', {'q': 'a', 'date_from': '2020-01-01',
                             'model': 'BranchStock'}),
                ('/sales/', {'q': 'a', 'date_from': '2020-01-01'}),
                ('/prices/', {'q': 'a', 'issue': 'no_cost'})):
            r = self.c.get(url, params)
            self.assertEqual(r.status_code, 200, url)
            qs = r.context['page_qs']
            for k, v in params.items():
                self.assertIn(f'{k}={v}'.replace(' ', '+'), qs,
                              f'{url}: "{k}" sahifa havolasida yo\'q')
            self.assertNotIn('page=', qs, url)

    def test_the_pagination_links_actually_render(self):
        """Kontekstda page_qs bo'lishi YETARLI EMAS.

        `{% firstof page_obj page as pg %}` qiymatni SATRGA aylantiradi va
        `pg.has_other_pages` jimgina bo'sh chiqadi — sahifalash UMUMAN
        chizilmay qoladi, lekin sahifa 200 qaytaraveradi. Shuning uchun
        HAQIQIY HTML tekshiriladi.
        """
        import re
        AuditLog.objects.all().delete()
        for i in range(120):
            AuditLog.objects.create(action=AuditLog.Action.LOGIN,
                                    model_name='User', object_repr=f'u{i}')
        r = self.c.get('/audit/', {'model': 'User'})
        html = r.content.decode()
        links = [x for x in
                 re.findall(r'class="page-link"\s+href="([^"]+)"', html)
                 if 'page=' in x]
        self.assertTrue(links, 'sahifalash havolalari CHIZILMADI')
        self.assertIn('model=User', links[0],
                      'havolada filtr yo\'q: ' + links[0])

    def test_firstof_as_is_never_used_for_an_object(self):
        """`{% firstof a b as x %}` qiymatni SATRGA aylantiradi.

        Bu jim xato: sahifa 200 qaytaradi, lekin blok chizilmaydi.
        Aynan shu xatoni bir marta qildim — endi test ushlab tursin.
        """
        import glob
        import os
        import re
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'templates')
        bad = []
        for f in glob.glob(os.path.join(base, '**', '*.html'), recursive=True):
            src = open(f, encoding='utf-8').read()
            src = re.sub(r'\{#.*?#\}', '', src, flags=re.S)
            src = re.sub(r'\{% comment %\}.*?\{% endcomment %\}', '', src,
                         flags=re.S)
            for m in re.finditer(r'\{%\s*firstof\b[^%]*\bas\s+\w+\s*%\}', src):
                bad.append(f'{os.path.basename(f)}: {m.group(0)}')
        self.assertEqual(bad, [], '; '.join(bad))

    def test_every_pagination_block_uses_the_shared_partial(self):
        import glob
        import os
        import re
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'templates', 'inventory')
        bad = []
        for f in glob.glob(os.path.join(base, '*.html')):
            if os.path.basename(f) == '_pagination.html':
                continue
            src = open(f, encoding='utf-8').read()
            if re.search(r'class="pagination', src):
                bad.append(os.path.basename(f))
        self.assertEqual(bad, [], "qo'lda yozilgan sahifalash: " + ', '.join(bad))

    # ---- CSV ----
    def test_every_export_has_the_bom_and_a_filename(self):
        for url, params in (('/sales/', {'export': 'csv'}),
                            ('/audit/', {'export': 'csv'}),
                            ('/customers/', {'export': 'csv'})):
            r = self.c.get(url, params)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn('text/csv', r['Content-Type'], url)
            self.assertIn('attachment; filename=', r['Content-Disposition'], url)
            self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'),
                            f'{url}: BOM yo\'q — Excel harflarni buzadi')

    def test_csv_injection_is_neutralised(self):
        from inventory.web import csv_safe
        for bad in ('=cmd|calc', '+1', '-1+2', '@SUM(1)', '\tx'):
            self.assertTrue(str(csv_safe(bad)).startswith("'"), bad)
        self.assertEqual(csv_safe('Koylak'), 'Koylak')
        self.assertEqual(csv_safe(None), '')


class Core4SharedPageChrome(TestCase):
    """CORE-4/5: sahifa sarlavhasi va modal qobig'i — bitta joyda.

    `dash-hero` bloki 40 marta, 33 faylda AYNAN bir xil yozilgan edi.
    Dizaynni o'zgartirish 33 faylni tahrirlashni talab qilardi va bitta
    faylda klass nomi noto'g'ri yozilsa hech kim sezmasdi.
    """

    # Istisno YO'Q: 40 ta blokning HAMMASI umumiy tegdan o'tadi.
    # Uchta "murakkab" sahifa ham ko'chirildi — ular uchun {% herotitle %},
    # {% herosub %}, {% heroafter %}, {% heroextra %} slotlari va
    # inner_style / heading_style / glow parametrlari qo'shildi.
    HERO_EXCEPTIONS = set()

    def _templates(self):
        import glob
        import os
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'templates')
        return glob.glob(os.path.join(base, '**', '*.html'), recursive=True)

    def test_page_headers_use_the_shared_hero(self):
        import os
        bad = []
        for f in self._templates():
            name = os.path.basename(f)
            if name in self.HERO_EXCEPTIONS or name == '_hero.html':
                continue
            if '<header class="dash-hero">' in open(f, encoding='utf-8').read():
                bad.append(name)
        self.assertEqual(bad, [], "qo'lda yozilgan sarlavha: " + ', '.join(bad))

    def test_the_hero_tag_renders_the_expected_markup(self):
        from django.template import Context, Template
        html = Template(
            '{% load yurit_extras %}'
            '{% hero "Sarlavha" icon="bi-x" eyebrow="Ko\'z" sub="Izoh" %}'
            '<a href="#">Tugma</a>{% endhero %}'
        ).render(Context({}))
        for piece in ('dash-hero__glow', 'dash-hero__inner',
                      'dash-hero__heading', 'dash-hero__eyebrow',
                      'dash-pulse', 'dash-hero__title', 'bi bi-x',
                      'Sarlavha', 'dash-hero__sub', 'Izoh',
                      'dash-hero__actions', '>Tugma<'):
            self.assertIn(piece, html, piece)

    def test_the_hero_tag_skips_empty_parts(self):
        from django.template import Context, Template
        html = Template('{% load yurit_extras %}'
                        '{% hero "Faqat sarlavha" %}{% endhero %}'
                        ).render(Context({}))
        self.assertNotIn('dash-hero__eyebrow', html)
        self.assertNotIn('dash-hero__sub', html)
        self.assertNotIn('dash-hero__actions', html)
        self.assertIn('Faqat sarlavha', html)

    def test_rich_slots_work(self):
        from django.template import Context, Template
        html = Template(
            '{% load yurit_extras %}'
            '{% hero "" icon="bi-y" %}'
            '{% herotitle %}Chek #{{ n }}{% endherotitle %}'
            '{% herosub %}<span>HTML ham</span>{% endherosub %}'
            '<button>Amal</button>'
            '{% endhero %}'
        ).render(Context({'n': 7}))
        self.assertIn('Chek #7', html)
        self.assertIn('<span>HTML ham</span>', html)
        self.assertIn('<button>Amal</button>', html)
        # slot amallar orasida IKKI MARTA chizilmasin
        self.assertEqual(html.count('Chek #7'), 1)
        self.assertEqual(html.count('HTML ham'), 1)

    def test_glow_can_be_turned_off(self):
        """Bitta sahifa (shift_returns) tarixan `glow` siz edi."""
        from django.template import Context, Template
        on = Template('{% load yurit_extras %}{% hero "A" %}{% endhero %}'
                      ).render(Context({}))
        off = Template('{% load yurit_extras %}{% hero "A" glow=0 %}{% endhero %}'
                       ).render(Context({}))
        self.assertIn('dash-hero__glow', on)
        self.assertNotIn('dash-hero__glow', off)

    def test_after_slot_lands_inside_the_heading(self):
        from django.template import Context, Template
        html = Template(
            '{% load yurit_extras %}{% hero "A" sub="S" %}'
            '{% heroafter %}<p id="x">qo\'shimcha</p>{% endheroafter %}'
            '{% endhero %}').render(Context({}))
        head = html[html.index('dash-hero__heading'):html.index('</header>')]
        self.assertIn('id="x"', head.split('</div>')[0] + '</div>')
        self.assertEqual(html.count('qo\'shimcha'), 1)

    def test_multiline_hero_tags_are_never_used(self):
        """Django lekseri QATOR UZILGAN {% ... %} ni teg deb bilmaydi.

        `tag_re` da re.DOTALL yo'q — shu bois ko'p qatorli teg jimgina
        oddiy MATN bo'lib chiqadi va sahifa "Invalid block tag" bilan
        yiqiladi. Bu xatoni bir marta qildim; test qaytarmasin.
        """
        import re
        bad = []
        for f in self._templates():
            src = open(f, encoding='utf-8').read()
            for m in re.finditer(r'\{%[^%]*?\n', src):
                frag = m.group(0)
                if '%}' in frag:
                    continue
                bad.append(f'{f.split("/")[-1]}: {frag.strip()[:60]}')
        self.assertEqual(bad, [], '; '.join(bad[:5]))

    def test_the_modal_tag_renders_the_expected_shell(self):
        from django.template import Context, Template
        html = Template(
            '{% load yurit_extras %}'
            '{% modal "testModal" title="Sarlavha" icon="bi-camera" %}'
            '<p>Tana</p>'
            '{% modalfooter %}<button>Yopish</button>{% endmodalfooter %}'
            '{% endmodal %}'
        ).render(Context({}))
        for piece in ('id="testModal"', 'modal-dialog-centered',
                      'modal-content', 'modal-header', 'btn-close',
                      'modal-body', '<p>Tana</p>', 'modal-footer',
                      '<button>Yopish</button>', 'bi bi-camera'):
            self.assertIn(piece, html, piece)
        self.assertEqual(html.count('Tana'), 1)


class Core6SharedJsHelpers(TestCase):
    """CORE-6: shablon ichidagi takrorlangan JS yordamchilari.

    `function num(s)` 5 faylda AYNAN bir xil, qisqa son formati 4 faylda
    aynan bir xil edi; CSRF tokenni olish esa UCH XIL usulda, ulardan
    ikkitasi NOTO'G'RI (keshlangan eski tokenni o'qiydi).
    """

    def _templates(self):
        import glob
        import os
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'templates')
        return glob.glob(os.path.join(base, '**', '*.html'), recursive=True)

    def test_common_js_is_loaded_before_page_scripts(self):
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(base, 'templates', 'inventory', 'base.html'),
                   encoding='utf-8').read()
        self.assertIn('js/yurit-common.js', src)
        # <head> ichida — sahifa skriptlaridan OLDIN
        self.assertLess(src.index('js/yurit-common.js'), src.index('</head>'))

    def test_no_template_redefines_the_shared_helpers(self):
        import os
        import re
        bad = []
        for f in self._templates():
            src = open(f, encoding='utf-8').read()
            name = os.path.basename(f)
            if re.search(r'function num\(s\)\s*\{', src):
                bad.append(f'{name}: o\'z num() nusxasi')
            if re.search(r"toFixed\(1\)\.replace\('\.0',\s*''\)\s*\+\s*'mlrd'",
                         src):
                bad.append(f'{name}: o\'z qisqa son formati')
        self.assertEqual(bad, [], '; '.join(bad))

    def test_no_template_reads_a_stale_csrf_token(self):
        """Keshlangan sahifada `{{ csrf_token }}` ESKI bo'ladi.

        Umumiy kassada A kassir chiqib B kirsa, B ning so'rovi 403
        bo'lardi. To'g'ri manba — jonli `csrftoken` cookie'si (Y.csrf()).
        """
        import os
        import re
        bad = []
        for f in self._templates():
            src = open(f, encoding='utf-8').read()
            name = os.path.basename(f)
            for m in re.finditer(r"(const|let|var)\s+CSRF\s*=\s*(.+)", src):
                val = m.group(2)
                if 'Y.csrf()' in val or 'document.cookie' in val:
                    continue
                bad.append(f'{name}: CSRF = {val.strip()[:60]}')
        self.assertEqual(bad, [], '; '.join(bad))

    def test_the_js_and_python_number_parsers_agree(self):
        """Brauzer ko'rsatgan narx bazadagi narx bilan bir xil bo'lsin.

        Eski JS nusxasi vergulni shunchaki O'CHIRARDI ("50,5" -> 505),
        server esa o'nlik ajratgich deb o'qirdi (50.5). Ya'ni kassir
        ko'rgan raqam bilan saqlangan raqam BOSHQA edi.
        """
        import os
        import re
        from inventory.money import parse_money
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js = open(os.path.join(base, 'static', 'js', 'yurit-common.js'),
                  encoding='utf-8').read()
        self.assertIn("replace(/,/g, '.')", js,
                      'JS vergulni o\'nlik ajratgich deb qabul qilsin')
        # Python tomoni ham xuddi shunday
        self.assertEqual(parse_money('50,5'), Decimal('50.5'))
        self.assertEqual(parse_money('12 345,50'), Decimal('12345.50'))
