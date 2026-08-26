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
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from inventory.models import (
    Branch, Product, ProductVariant, BranchStock, Shift,
    SaleTransaction, Sale, CashPayout, PaymentIntent, Return,
    split_breakdown,
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
    """REF-1 (atomiklik) va REF-2 (order_discount qaytarishда)."""

    def _txn(self, order_discount='0'):
        return SaleTransaction.objects.create(
            branch=self.branch, sold_by=self.cashier, shift=self.open_shift(),
            payment_method='cash', order_discount=Decimal(order_discount))

    def _sale(self, txn, qty=1, price='100000'):
        return Sale.objects.create(
            transaction=txn, variant=self.variant, branch=self.branch,
            quantity=qty, sale_price=Decimal(price),
            cost_at_sale=Decimal('60000'), sold_by=self.cashier)

    def test_ref2_full_return_respects_order_discount(self):
        # 3×100000, chek chegirmasi 100000 → mijoz 200000 to'lagan
        txn = self._txn(order_discount='100000')
        sale = self._sale(txn, qty=3)
        r = self.client.post('/pos/refund/', data=json.dumps(
            {'lines': [{'sale_id': sale.pk, 'qty': 3, 'reason': 't'}]}),
            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['refunded_total'], 200000.0,
                         'qaytarish mijoz TO\'LAGANI (200000) bo\'lishi kerak, 300000 emas')

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
