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
import unittest
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from inventory.models import (
    Branch, Product, ProductVariant, BranchStock, Shift,
    SaleTransaction, Sale, CashPayout, PaymentIntent,
)

User = get_user_model()
CHECKOUT_URL = '/pos/checkout/'


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


class BugMon8ClientSuppliedPrice(MoneyTestBase):
    """MON-8 — narx brauzerdan keladi va DB bilan solishtirilmaydi.

    ATAYLAB KUTILGAN XATO (expectedFailure): do'kon narxni QO'LDA kiritadi —
    "Boshqa summa" (kiyim/poyabzal), ulgurji, kelishilgan narx. Serverда narxni
    qattiq DB narxiga majburlash real ish jarayonini buzadi. Buni tuzatish
    siyosat qarori talab qiladi (flag-and-audit yoki open_price bayrog'i) —
    egasidan tasdiq kutmoqda. Shuning uchun bu test ataylab qizil, lekin
    suite'ni yiqitmaydi.
    """

    @unittest.expectedFailure
    def test_server_does_not_accept_arbitrary_price(self):
        self.open_shift()
        self.checkout(lines=[{'stock_id': self.stock.pk, 'qty': 1,
                              'sale_price': '1'}])  # DB narxi 100 000
        sale = Sale.objects.first()
        if sale is not None:
            self.assertEqual(
                sale.sale_price, Decimal('100000'),
                'narx serverда qayta hisoblanishi kerak, mijozdan olinmaydi',
            )


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
