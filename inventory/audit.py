"""AUD-1: bitta amal — bitta audit yozuvi.

Muammo: bitta "Saqlash" bosilganda signal'lar har bir OBYEKT uchun
alohida AuditLog qatori yozardi (14 ta turni birga tahrirlasangiz —
14+ qator). Audit sahifasida bitta amal 14 ta satr bo'lib ko'rinardi
va nima qilinganini o'qib bo'lmasdi.

Yechim: `audit_batch(...)` kontekst menejeri. Uning ichida yozilgan
HAMMA yozuv bitta `batch_id` bilan belgilanadi, chiqishda esa bitta
BOSH (head) qator yoziladi — `batch_count` = nechta qator. Audit
ro'yxati faqat bosh qatorni ko'rsatadi, ichkarisi ochilib ko'rinadi.

Ma'lumot YO'QOLMAYDI: har bir obyekt uchun to'liq diff avvalgidek
saqlanadi (/prices/history/ va smena sahifasi shu qatorlarni o'qiydi),
faqat KO'RINISHI guruhlanadi. `?raw=1` bilan hammasi ko'rinadi.

`log_action(...)` — qo'lda yoziladigan audit yozuvlari uchun yagona
yo'l. Ilgari har bir view `AuditLog.objects.create(...)` ni o'zi
chaqirar va IP ni HECH QAYSI to'ldirmasdi — audit jadvalida "IP —"
ustuni shundan bo'sh edi.
"""
import threading
import uuid
from contextlib import contextmanager

from .middleware import get_current_ip, get_current_user
from .models import AuditLog

_batch = threading.local()

# Bir bosh qator ostida ko'rsatiladigan maksimal bola qator (sahifa
# 5000 qatordan iborat bo'lib ketmasin — qolgani "+N ta" deb yoziladi).
BATCH_PREVIEW = 200


def current_batch_id():
    """Hozir ochiq partiya identifikatori (yo'q bo'lsa — bo'sh satr)."""
    return getattr(_batch, 'bid', '') or ''


def log_action(action, *, model_name='', object_id='', object_repr='',
               changes=None, user=None, batch_count=0, batch_id=None):
    """Qo'lda audit yozuvi. IP va ochiq partiya AVTOMAT qo'yiladi."""
    u = user if user is not None else get_current_user()
    ok = bool(u is not None and getattr(u, 'is_authenticated', False))
    return AuditLog.objects.create(
        user=u if ok else None,
        username_snapshot=(u.username if ok else ''),
        action=action,
        model_name=model_name or '',
        object_id=str(object_id or '')[:80],
        object_repr=(object_repr or '')[:300],
        changes=changes if changes is not None else {},
        ip=get_current_ip(),
        batch_id=(current_batch_id() if batch_id is None else batch_id),
        batch_count=batch_count,
    )


class _Batch:
    """audit_batch(...) qaytaradigan tutqich."""

    def __init__(self, bid, label, action, model_name, object_id, changes):
        self.id = bid
        self.label = label
        self.action = action
        self.model_name = model_name
        self.object_id = object_id
        self.changes = dict(changes or {})
        self.cancelled = False

    def note(self, key, value):
        """Bosh qatorga qo'shimcha izoh (masalan sabab, filial)."""
        self.changes[str(key)] = value

    def describe(self, label):
        self.label = label

    def cancel(self):
        """Bosh qator YOZILMASIN (masalan hech nima o'zgarmadi)."""
        self.cancelled = True


class _NoopBatch(_Batch):
    """Ichma-ich chaqirilganda — tashqi partiya hukmron."""

    def cancel(self):
        pass


@contextmanager
def audit_batch(label, *, action=None, model_name='', object_id='',
                changes=None):
    """Ichkarida yozilgan hamma audit yozuvini BITTA amal deb belgilaydi.

    Tranzaksiya ICHIDA ishlatilsin — xato bo'lsa bosh qator ham,
    bolalari ham orqaga qaytadi.
    """
    action = action or AuditLog.Action.UPDATE
    prev = getattr(_batch, 'bid', '') or ''
    if prev:
        # Ichma-ich: yangi partiya ochilmaydi, yozuvlar tashqarisiga tushadi.
        yield _NoopBatch(prev, label, action, model_name, object_id, changes)
        return

    bid = uuid.uuid4().hex
    b = _Batch(bid, label, action, model_name, object_id, changes)
    _batch.bid = bid
    try:
        yield b
    except Exception:
        _batch.bid = prev
        raise
    _batch.bid = prev

    if b.cancelled:
        return
    n = AuditLog.objects.filter(batch_id=bid).count()
    if not n:
        # Hech nima o'zgarmadi — bo'sh bosh qator yozib, audit'ni
        # shovqinga to'ldirmaymiz.
        return
    log_action(b.action, model_name=b.model_name, object_id=b.object_id,
               object_repr=b.label, changes=b.changes,
               batch_count=n, batch_id=bid)
