"""SMS receipt provider — sends a short receipt summary to the customer.

How this works
--------------
After a successful sale, the POS may ask the provider to send a short SMS
to the customer's phone with the receipt summary (txn ID, total, branch).

Selection happens via settings.SMS_PROVIDER:
    ''      -> NoopProvider (default; just logs)
    'eskiz' -> EskizProvider (Eskiz.uz; stub here)

Real wiring is intentionally left out — when a real Eskiz account is
ready, fill in EskizProvider.send() with the token-auth + POST flow and
add ESKIZ_EMAIL / ESKIZ_PASSWORD to Render env.
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from .models import SaleTransaction

logger = logging.getLogger(__name__)


class SMSError(Exception):
    """Raised by a provider when SMS sending fails."""


class SMSProvider:
    name = 'base'

    def send(self, phone: str, message: str) -> dict:
        raise NotImplementedError


class NoopProvider(SMSProvider):
    name = 'noop'

    def send(self, phone: str, message: str) -> dict:
        logger.info('[SMS noop] %s -> %s', phone, message)
        return {'sent': False, 'provider': 'noop'}


class EskizProvider(SMSProvider):
    """Eskiz.uz stub. Real send() implementation pending API credentials.

    Required env when wired:
      ESKIZ_EMAIL, ESKIZ_PASSWORD
    """
    name = 'eskiz'

    def send(self, phone: str, message: str) -> dict:
        # TODO: real Eskiz flow
        #   1) POST /api/auth/login with {email, password} -> token
        #   2) cache token in Django cache for ~25 days
        #   3) POST /api/message/sms/send with {mobile_phone, message, from}
        logger.warning(
            '[SMS eskiz stub] %s -> %s (real API not wired)', phone, message,
        )
        return {'sent': False, 'provider': 'eskiz', 'stub': True}


def get_provider() -> SMSProvider:
    name = (getattr(settings, 'SMS_PROVIDER', '') or '').strip().lower()
    if name == 'eskiz':
        return EskizProvider()
    return NoopProvider()


def format_receipt_sms(txn: 'SaleTransaction') -> str:
    """Short SMS body — UZ telcos charge per 70-char chunk (Cyrillic/Latin
    extended set). Keep it under ~140 chars for two-part max."""
    total = int(txn.total)
    return (
        f"Srote: chek #{txn.pk}. "
        f"Summa: {total:,} so'm. "
        f"Filial: {txn.branch.name}. "
        f"Rahmat!"
    ).replace(',', ' ')


def send_receipt(txn: 'SaleTransaction', phone: str) -> dict:
    """Best-effort: never raises out of this helper — callers don't want
    a failed SMS to abort the sale."""
    if not phone:
        return {'sent': False, 'reason': 'no phone'}
    cleaned = ''.join(c for c in phone if c.isdigit() or c == '+')
    if not cleaned:
        return {'sent': False, 'reason': 'invalid phone'}
    try:
        body = format_receipt_sms(txn)
        return get_provider().send(cleaned, body)
    except Exception as e:
        logger.exception('SMS send failed for txn #%s: %s', txn.pk, e)
        return {'sent': False, 'error': str(e)}
