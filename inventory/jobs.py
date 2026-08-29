"""OPS-15 — fon ishlari: ro'yxatga olish, navbatga qo'yish, bajarish.

Sotuv yo'lidan sekin/ishonchsiz chaqiruvlarni olib chiqish uchun. Qoida
sodda: SOTUV hech qachon tashqi xizmatni kutmasin. Fiskal chek yuborilmasa
yoki SMS ketmasa — chek baribir yozilgan bo'ladi, ish esa navbatda qayta
urinadi.
"""
import logging
import traceback
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

HANDLERS = {}


def handler(kind):
    """Ishlovchini ro'yxatga oladi: @handler('sms_receipt')"""
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(kind, delay_seconds=0, max_attempts=5, **payload):
    """Ishni navbatga qo'yadi. Chaqiruvchi natijani KUTMAYDI.

    Tranzaksiya ichida chaqirilsa on_commit bilan kechiktiriladi: sotuv
    orqaga qaytsa, unga tegishli ish ham navbatga tushmasligi kerak.
    """
    from .models import BackgroundJob
    if kind not in HANDLERS:
        raise ValueError(f"noma'lum ish turi: {kind}")
    BackgroundJob.objects.create(
        kind=kind, payload=payload, max_attempts=max_attempts,
        run_after=timezone.now() + timedelta(seconds=delay_seconds))


def enqueue_on_commit(kind, **kw):
    """Tranzaksiya MUVAFFAQIYATLI yopilgandan keyin navbatga qo'yadi."""
    transaction.on_commit(lambda: enqueue(kind, **kw))


def _claim(limit):
    """Bajarishga tayyor ishlarni oladi va RUNNING deb belgilaydi.

    skip_locked — ikki ishlovchi bir ishni olmasin. SQLite uni
    qo'llab-quvvatlamaydi (u yerda umuman parallel ishlovchi ham yo'q),
    shuning uchun imkoniyat tekshiriladi.
    """
    from django.db import connection
    from .models import BackgroundJob
    now = timezone.now()
    qs = (BackgroundJob.objects
          .filter(status=BackgroundJob.Status.PENDING, run_after__lte=now)
          .order_by('run_after', 'id'))
    claimed = []
    with transaction.atomic():
        if connection.features.has_select_for_update_skip_locked:
            rows = list(qs.select_for_update(skip_locked=True)[:limit])
        else:
            rows = list(qs[:limit])
        for j in rows:
            j.status = BackgroundJob.Status.RUNNING
            j.attempts += 1
            j.save(update_fields=['status', 'attempts'])
            claimed.append(j)
    return claimed


def run_once(limit=20):
    """Bir marta aylanadi. Qaytaradi: (bajarildi, xato)."""
    from .models import BackgroundJob
    ok = err = 0
    for job in _claim(limit):
        fn = HANDLERS.get(job.kind)
        if fn is None:
            job.status = BackgroundJob.Status.FAILED
            job.last_error = f"ishlovchi yo'q: {job.kind}"
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'last_error', 'finished_at'])
            err += 1
            continue
        try:
            fn(**(job.payload or {}))
        except Exception as e:
            err += 1
            job.last_error = (str(e) + '\n' + traceback.format_exc())[:4000]
            if job.attempts >= job.max_attempts:
                job.status = BackgroundJob.Status.FAILED
                job.finished_at = timezone.now()
            else:
                # eksponensial kechikish: 1, 2, 4, 8 daqiqa
                job.status = BackgroundJob.Status.PENDING
                job.run_after = timezone.now() + timedelta(
                    minutes=2 ** (job.attempts - 1))
            job.save(update_fields=['status', 'last_error', 'run_after',
                                    'finished_at'])
            logger.warning('fon ishi xato %s #%s: %s', job.kind, job.pk, e)
        else:
            ok += 1
            job.status = BackgroundJob.Status.DONE
            job.last_error = ''
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'last_error', 'finished_at'])
    return ok, err


# ---------------------------------------------------------------- ishlovchilar

@handler('fiscal_submit')
def _fiscal_submit(txn_id):
    """Soliq chekini yuborish — ilgari sotuv so'rovi ichida edi."""
    from .models import SaleTransaction
    from .fiscal import submit_for_transaction
    txn = SaleTransaction.objects.filter(pk=txn_id).first()
    if txn is None:
        return
    submit_for_transaction(txn)


@handler('sms_receipt')
def _sms_receipt(txn_id, phone):
    """Chek SMS — ilgari sotuv so'rovi ichida edi."""
    from .models import SaleTransaction
    from .sms import send_receipt
    txn = SaleTransaction.objects.filter(pk=txn_id).first()
    if txn is None:
        return
    send_receipt(txn, phone)
