"""Payment provider interface — QR to'lov va muddatli to'lov.

How this works
--------------
O'zbekistondagi to'lov tizimlari (Click, Payme, Uzum) "Customer-Presented
QR" sxemasi bilan ishlaydi: do'kon serveri provider'ga to'lov niyatini
yuboradi, javobida QR kod / deeplink oladi va kassa ekranida ko'rsatadi.
Mijoz telefonidan scan qilib to'laydi. Provider callback (webhook) yoki
periodic polling orqali holatni xabar qiladi.

Muddatli to'lov (Anor, Alif, Iman, Zoodpay) ham xuddi shu sxema — farqi
to'lov nogd amalga oshmaydi, balki mijoz kreditni rasmiylashtiradi.

Common interface
----------------
    provider.create_intent(amount, txn_ref) -> {
        'intent_id': str,   # provider-side ID
        'qr_url': str,      # QR rasm URL yoki to'g'ridan-to'g'ri payload
        'deeplink': str,    # ixtiyoriy: telefon ilovasiga to'g'ridan-to'g'ri
        'expires_at': iso,  # qancha vaqt ichida to'lash kerak
    }
    provider.check_status(intent_id) -> {
        'status': 'pending' | 'paid' | 'cancelled' | 'expired',
        'paid_at': iso | None,
        'raw': ...,
    }

Selection happens via settings.PAYMENT_PROVIDERS — dict {alias: provider_name}.
Default is all NoopProvider (returns demo data so UI can be tested without
real credentials).

When real credentials available, fill in the corresponding provider class
and set the env var. Nothing else in the codebase needs to change.

Required env (when wired):
    CLICK_MERCHANT_ID, CLICK_SECRET_KEY, CLICK_SERVICE_ID
    PAYME_MERCHANT_ID, PAYME_SECRET_KEY
    UZUM_MERCHANT_ID, UZUM_API_KEY
    ANOR_API_KEY, ALIF_API_KEY, IMAN_API_KEY, ZOODPAY_API_KEY
"""
from __future__ import annotations
import logging
import secrets
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """Raised by a provider when intent creation or status check fails."""


# ---------- Base interface ----------

class PaymentProvider:
    name = 'base'
    display_name = 'Base'
    icon = 'bi-credit-card'
    is_installment = False  # True for Anor/Alif/Iman/Zoodpay

    def create_intent(self, amount: float, txn_ref: str) -> dict:
        raise NotImplementedError

    def check_status(self, intent_id: str) -> dict:
        raise NotImplementedError


# ---------- Noop (demo / fallback) ----------

class NoopProvider(PaymentProvider):
    """Stub: barcha intent'lar darhol 'paid' qaytaradi, log'ga yoziladi.
    UI'ni real provider'siz ham sinab ko'rishga yordam beradi."""
    name = 'noop'
    display_name = 'Demo to\'lov'
    icon = 'bi-qr-code'

    def create_intent(self, amount: float, txn_ref: str) -> dict:
        intent_id = 'demo-' + uuid.uuid4().hex[:12]
        logger.info('[payments noop] create %s for %s so\'m (txn=%s)',
                    intent_id, amount, txn_ref)
        return {
            'intent_id': intent_id,
            'qr_url': f'https://api.qrserver.com/v1/create-qr-code/?size=240x240'
                      f'&data=demo-payment-{intent_id}',
            'deeplink': '',
            'expires_at': (timezone.now() + timedelta(minutes=10)).isoformat(),
        }

    def check_status(self, intent_id: str) -> dict:
        # Demo: 5 soniyadan keyin 'paid' deb hisoblansin.
        # Stub uchun darhol paid qaytaramiz — UI flow'ni sinab ko'rish uchun.
        return {'status': 'paid', 'paid_at': timezone.now().isoformat(),
                'raw': {'provider': 'noop', 'intent_id': intent_id}}


# ---------- QR providers ----------

class ClickProvider(PaymentProvider):
    """Click — eng keng tarqalgan O'zbek QR to'lov. Stub.

    Real wiring:
      1. POST /merchant/invoice/create
         {service_id, amount, transaction_param, phone_number}
         -> {invoice_id, qr_code_url}
      2. Webhook receive: POST from Click with signed payload (SHA-1 of
         click_trans_id+service_id+secret+...) -> mark paid
      3. Poll fallback: GET /merchant/invoice/status/<id>
    """
    name = 'click'
    display_name = 'Click'
    icon = 'bi-qr-code'

    def create_intent(self, amount: float, txn_ref: str) -> dict:
        logger.warning('[payments click stub] create %s amount=%s (real API not wired)',
                       txn_ref, amount)
        intent_id = 'click-' + secrets.token_hex(8)
        return {
            'intent_id': intent_id,
            'qr_url': f'https://api.qrserver.com/v1/create-qr-code/?size=240x240'
                      f'&data=click-stub-{intent_id}',
            'deeplink': '',
            'expires_at': (timezone.now() + timedelta(minutes=10)).isoformat(),
            'stub': True,
        }

    def check_status(self, intent_id: str) -> dict:
        # Stub: always pending so UI shows polling state. Real impl checks
        # against Click API.
        return {'status': 'pending', 'paid_at': None,
                'raw': {'provider': 'click', 'stub': True}}


class PaymeProvider(PaymentProvider):
    """Payme — ikkinchi yirik to'lov tizimi. Stub.

    Real wiring: Payme Subscribe API (Merchant API). Two-phase flow:
      1. POST /api  method=cards.create  -> token
      2. POST /api  method=cards.get_verify_code
      3. Customer types SMS code
      4. POST /api  method=cards.verify -> card verified, charge happens
    For POS QR: use Payme P2P/Merchant QR endpoint (newer).
    """
    name = 'payme'
    display_name = 'Payme'
    icon = 'bi-qr-code'

    def create_intent(self, amount: float, txn_ref: str) -> dict:
        logger.warning('[payments payme stub] create %s amount=%s', txn_ref, amount)
        intent_id = 'payme-' + secrets.token_hex(8)
        return {
            'intent_id': intent_id,
            'qr_url': f'https://api.qrserver.com/v1/create-qr-code/?size=240x240'
                      f'&data=payme-stub-{intent_id}',
            'deeplink': '',
            'expires_at': (timezone.now() + timedelta(minutes=10)).isoformat(),
            'stub': True,
        }

    def check_status(self, intent_id: str) -> dict:
        return {'status': 'pending', 'paid_at': None,
                'raw': {'provider': 'payme', 'stub': True}}


class UzumProvider(PaymentProvider):
    """Uzum Bank QR — yangi, lekin tez o'sib bormoqda. Stub."""
    name = 'uzum'
    display_name = 'Uzum Bank'
    icon = 'bi-qr-code'

    def create_intent(self, amount: float, txn_ref: str) -> dict:
        logger.warning('[payments uzum stub] create %s amount=%s', txn_ref, amount)
        intent_id = 'uzum-' + secrets.token_hex(8)
        return {
            'intent_id': intent_id,
            'qr_url': f'https://api.qrserver.com/v1/create-qr-code/?size=240x240'
                      f'&data=uzum-stub-{intent_id}',
            'deeplink': '',
            'expires_at': (timezone.now() + timedelta(minutes=10)).isoformat(),
            'stub': True,
        }

    def check_status(self, intent_id: str) -> dict:
        return {'status': 'pending', 'paid_at': None,
                'raw': {'provider': 'uzum', 'stub': True}}


# ---------- Installment providers (muddatli to'lov) ----------

class _InstallmentStub(PaymentProvider):
    is_installment = True
    icon = 'bi-calendar-check'

    def create_intent(self, amount: float, txn_ref: str) -> dict:
        logger.warning('[payments %s stub] create %s amount=%s',
                       self.name, txn_ref, amount)
        intent_id = f'{self.name}-' + secrets.token_hex(8)
        return {
            'intent_id': intent_id,
            'qr_url': f'https://api.qrserver.com/v1/create-qr-code/?size=240x240'
                      f'&data={self.name}-stub-{intent_id}',
            'deeplink': '',
            'expires_at': (timezone.now() + timedelta(minutes=30)).isoformat(),
            'stub': True,
        }

    def check_status(self, intent_id: str) -> dict:
        return {'status': 'pending', 'paid_at': None,
                'raw': {'provider': self.name, 'stub': True}}


class AnorProvider(_InstallmentStub):
    name = 'anor'
    display_name = 'Anor (muddatli)'


class AlifProvider(_InstallmentStub):
    name = 'alif'
    display_name = 'Alif (muddatli)'


class ImanProvider(_InstallmentStub):
    name = 'iman'
    display_name = 'Iman (muddatli)'


class ZoodpayProvider(_InstallmentStub):
    name = 'zoodpay'
    display_name = 'Zoodpay (muddatli)'


# ---------- Registry ----------

_REGISTRY = {
    'noop': NoopProvider,
    'click': ClickProvider,
    'payme': PaymeProvider,
    'uzum': UzumProvider,
    'anor': AnorProvider,
    'alif': AlifProvider,
    'iman': ImanProvider,
    'zoodpay': ZoodpayProvider,
}


def available_providers() -> list[PaymentProvider]:
    """Faqat sozlamada ANIQ yoqilgan provider'lar.

    SEC-18: ilgari env yo'q bo'lsa 7 ta providerni HAMMASINI yoqar edi —
    real ulanmagan to'lov usullari kassirga ko'rinardi. Endi yoqilmagan
    bo'lsa — BO'SH (hech biri).
    """
    enabled = getattr(settings, 'PAYMENT_PROVIDERS_ENABLED', None)
    if not enabled:
        enabled = []
    out = []
    for name in enabled:
        cls = _REGISTRY.get(name)
        if cls:
            out.append(cls())
    return out


def get_provider(name: str) -> PaymentProvider | None:
    cls = _REGISTRY.get(name)
    return cls() if cls else None
