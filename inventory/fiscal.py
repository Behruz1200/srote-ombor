"""Fiscal provider interface for soliq.uz / OFD integration.

How this is supposed to work
----------------------------
In Uzbekistan, every retail sale must be reported to a Fiscal Data
Operator (OFD), which forwards to soliq.uz. The OFD returns:
  - fiscal receipt number
  - QR URL (printed on the customer's receipt; mijoz scan qilib
    cheki haqiqiyligini tekshiradi)

Each OFD (Didox, OneSystem, MyTaxi, ...) has its own HTTP API. To
avoid coupling the rest of the codebase to any one provider, all
contact happens through one class with one method:

    provider.submit_sale(txn: SaleTransaction) -> dict
        returns {'receipt_number': str, 'qr_url': str, 'raw': ...}
        raises FiscalError on failure

The provider used is chosen by settings.FISCAL_PROVIDER:
    ''        -> NoopProvider (default; does nothing, useful in dev)
    'didox'   -> DidoxProvider (to be implemented)
    'onesystem' -> OneSystemProvider (to be implemented)

When a real OFD account is wired up, you only edit *this file*:
1. Implement the corresponding provider class.
2. Set FISCAL_PROVIDER env var on Render to its name.
3. Add the provider's API key / cert env vars and reference them
   in the provider class.

Nothing else in the codebase needs to know about the OFD's API.
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from .models import SaleTransaction

logger = logging.getLogger(__name__)


class FiscalError(Exception):
    """Raised by a provider when OFD submission fails."""


# ---------- Base interface ----------

class FiscalProvider:
    """Subclass and implement submit_sale."""

    name = 'base'

    def submit_sale(self, txn: 'SaleTransaction') -> dict:
        raise NotImplementedError

    def cancel_sale(self, txn: 'SaleTransaction') -> dict:
        """For returns/refunds. Optional, providers may not support it."""
        raise NotImplementedError


# ---------- No-op default (used in dev) ----------

class NoopProvider(FiscalProvider):
    """Default. Logs and returns empty values. Lets the rest of the
    app behave the same with or without a real OFD connected."""

    name = 'noop'

    def submit_sale(self, txn):
        logger.info(
            'NoopProvider: would submit txn=%s, branch=%s, total=%s',
            txn.pk, txn.branch_id, txn.total,
        )
        return {'receipt_number': '', 'qr_url': '', 'raw': {'noop': True}}

    def cancel_sale(self, txn):
        return {'noop': True}


# ---------- Didox skeleton (to be implemented) ----------

class DidoxProvider(FiscalProvider):
    """Stub. To activate Didox integration:

    1. Sign up at didox.uz, get an API key + your TIN.
    2. Set env vars:
         FISCAL_PROVIDER=didox
         DIDOX_API_KEY=...
         DIDOX_BASE_URL=https://api.didox.uz   (or whatever Didox docs say)
    3. Implement submit_sale() below using `requests` or urllib.

    The Sale and Branch records already carry every field a typical
    Didox call needs (INN, MXIK, VAT, units, customer INN, etc).
    """

    name = 'didox'

    def submit_sale(self, txn):
        # TODO: implement real Didox call here. Outline:
        #
        # api_key = os.environ['DIDOX_API_KEY']
        # body = {
        #     'sellerTin': txn.branch.inn,
        #     'fiscalModule': txn.branch.fiscal_module_id,
        #     'lines': [
        #         {
        #             'mxikCode': line.variant.product.mxik_code,
        #             'name': line.variant.product.name,
        #             'qty': line.quantity,
        #             'price': str(line.sale_price),
        #             'vatPercent': str(line.variant.product.vat_percent),
        #             'unitCode': line.variant.product.unit_code,
        #         } for line in txn.lines.all()
        #     ],
        #     'totalAmount': str(txn.total),
        #     'paymentMethod': txn.payment_method,
        #     'customerTin': txn.customer_inn or None,
        # }
        # resp = requests.post(f'{base_url}/receipt', json=body, headers=...)
        # data = resp.json()
        # return {'receipt_number': data['number'], 'qr_url': data['qrUrl'], 'raw': data}
        raise NotImplementedError("Didox provider hali yozilmagan.")


# ---------- Picker ----------

_REGISTRY = {
    '': NoopProvider,
    'noop': NoopProvider,
    'didox': DidoxProvider,
}


def get_provider() -> FiscalProvider:
    name = (getattr(settings, 'FISCAL_PROVIDER', '') or '').strip().lower()
    cls = _REGISTRY.get(name, NoopProvider)
    return cls()


# ---------- High-level helper used by views ----------

def submit_for_transaction(txn) -> None:
    """Best-effort: submit a SaleTransaction to the configured OFD,
    store result on the txn. Never re-raises -- a fiscal failure
    should NOT block the sale (audit log captures it). Manual retry
    is possible later from the admin."""
    provider = get_provider()
    if isinstance(provider, NoopProvider) and not getattr(
            settings, 'FISCAL_FORCE_NOOP_STATUS', False):
        # Dev mode: leave fiscal_status blank so it's clearly not real
        return
    try:
        result = provider.submit_sale(txn)
        txn.fiscal_receipt_number = result.get('receipt_number') or ''
        txn.fiscal_qr_url = result.get('qr_url') or ''
        txn.fiscal_status = 'sent'
        txn.fiscal_error = ''
    except FiscalError as e:
        txn.fiscal_status = 'failed'
        txn.fiscal_error = str(e)
        logger.warning('Fiscal submit failed for txn=%s: %s', txn.pk, e)
    except NotImplementedError:
        txn.fiscal_status = 'skipped'
        logger.info('Fiscal provider not implemented; skipped txn=%s', txn.pk)
    except Exception as e:
        txn.fiscal_status = 'failed'
        txn.fiscal_error = str(e)
        logger.exception('Unexpected fiscal error for txn=%s', txn.pk)
    txn.save(update_fields=[
        'fiscal_receipt_number', 'fiscal_qr_url', 'fiscal_status', 'fiscal_error'
    ])
