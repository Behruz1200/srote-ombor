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
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db import connection
from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from inventory.models import (
    Branch, Product, ProductVariant, BranchStock, Shift,
    SaleTransaction, Sale, CashPayout, PaymentIntent, Return,
    split_breakdown, _dec,
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
        self.assertEqual(_csv_safe(1000), 1000)


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


class AdminPageSmoke(MoneyTestBase):
    """Admin sahifalari KAMIDA 200 qaytarishi kerak — render paytидagi xato
    (KeyError, AttributeError) foydalanuvchiga 500 berib chiqmasin. Ikki marta
    aynan shu tur nuqson deploy'ga o'tib ketdi (sales_list _returned; variants
    edit 'variant' vs 'v_id'), chunki hech bir test bu sahifalarni OCHMASdi."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username='admin_smoke', password='x', role=User.Role.ADMIN,
            is_staff=True, branch=self.branch)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_variants_edit_renders(self):
        """Turlarni tahrirlash sahifasi (GET) — 'v_id' vs 'variant' KeyError'i
        qaytmasin (bu aynan yiqilgan sahifa edi)."""
        r = self.client.get(reverse('product_variants_edit',
                                     args=[self.product.code]))
        self.assertEqual(r.status_code, 200)

    def test_sales_list_both_views_render(self):
        r1 = self.client.get('/sales/')                      # checks (standart)
        r2 = self.client.get('/sales/', {'view': 'items'})   # mahsulotlar
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_page_without_discounts_pays_almost_nothing_extra(self):
        self._bulk(20)
        with CaptureQueriesContext(connection) as c:
            self._load()
        self.assertLess(len(c.captured_queries), 30,
                        'chegirmasiz sahifa uchun so\'rovlar juda ko\'p')
