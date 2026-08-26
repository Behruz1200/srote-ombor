"""Audit log signals.

Hooks into pre_save/post_save/post_delete on tracked models, captures
field-level diffs, and writes an AuditLog row.

Request user / IP are stashed onto the thread-local context by
AuditMiddleware (see middleware.py) at the start of each request.
"""
from django.db.models.signals import pre_save, post_save, post_delete
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from decimal import Decimal
from datetime import datetime, date
from django.db.models.fields.files import FieldFile

from .models import (
    AuditLog, Product, ProductVariant, BranchStock, Intake, Sale,
    SaleTransaction, Transfer, Branch, Category, User,
)
from .middleware import get_current_user, get_current_ip

# Models we audit. Order matters for the dispatcher.
TRACKED_MODELS = [Product, ProductVariant, BranchStock, Intake, Sale,
                  Branch, Category, User]

# Fields to skip in diff (noise, sensitive, or auto-managed)
SKIP_FIELDS = {
    'last_login', 'date_joined', 'password',
    'received_at', 'sold_at', 'created_at',  # set automatically
}


def _serialize(value):
    """Render a field value safely for JSON storage."""
    if value is None:
        return None
    if isinstance(value, (Decimal, datetime, date)):
        return str(value)
    if isinstance(value, FieldFile):
        return value.name or ''
    if hasattr(value, 'pk'):
        return f'{value.__class__.__name__}:{value.pk}'
    return value


def _snapshot(instance):
    """Capture field name → value dict for an instance."""
    snap = {}
    for field in instance._meta.fields:
        name = field.name
        if name in SKIP_FIELDS:
            continue
        try:
            value = getattr(instance, name)
        except Exception:
            continue
        snap[name] = _serialize(value)
    return snap


def _diff(before, after):
    """Compute {field: [old, new]} for changed fields."""
    changes = {}
    for key in after:
        if before.get(key) != after.get(key):
            changes[key] = [before.get(key), after.get(key)]
    return changes


def _write_log(action, instance=None, changes=None, model_name=None,
               object_id='', object_repr=''):
    user = get_current_user()
    AuditLog.objects.create(
        user=user if (user and user.is_authenticated) else None,
        username_snapshot=user.username if (user and user.is_authenticated) else '',
        action=action,
        model_name=model_name or (instance.__class__.__name__ if instance else ''),
        object_id=str(instance.pk) if instance else object_id,
        object_repr=str(instance)[:300] if instance else object_repr,
        changes=changes or {},
        ip=get_current_ip(),
    )


# ---- pre_save: capture "before" snapshot on the instance ----

def pre_save_handler(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = sender.objects.get(pk=instance.pk)
            instance._audit_before = _snapshot(old)
        except sender.DoesNotExist:
            instance._audit_before = {}
    else:
        instance._audit_before = None


def post_save_handler(sender, instance, created, **kwargs):
    after = _snapshot(instance)
    if created:
        _write_log(AuditLog.Action.CREATE, instance=instance, changes={})
    else:
        before = getattr(instance, '_audit_before', {}) or {}
        changes = _diff(before, after)
        # Noise cut: BranchStock.stock_count changes on every sale/intake, and
        # those movements are already captured in Sale/Intake/Transfer with full
        # context. Skip auditing stock_count-only BranchStock updates so the
        # audit table doesn't balloon (price/other-field changes still logged).
        if sender is BranchStock and set(changes) == {'stock_count'}:
            changes = {}
        if changes:
            _write_log(AuditLog.Action.UPDATE, instance=instance, changes=changes)

    # NOTE: har sotuvда "tovar tugab bormoqda" Telegram xabari YUBORILMAYDI —
    # do'kon egasi so'roviga ko'ra (2026), kam qolgan tovarlar RO'YXATI endi
    # faqat kunlik 22:00 xulosadan keyin ALOHIDA bitta xabar bo'lib keladi
    # (inventory/notifications.py: low_stock_report_text). Har sotuvда spam yo'q.

    # Invalidate HQ dashboard cache on any sale-shaped activity so the next
    # admin hit recomputes immediately instead of waiting up to 60s.
    if sender in (SaleTransaction, Sale, BranchStock, Transfer):
        try:
            from django.core.cache import cache
            from .views import DASHBOARD_CACHE_KEY
            cache.delete(DASHBOARD_CACHE_KEY)
        except Exception:
            pass


def post_delete_handler(sender, instance, **kwargs):
    _write_log(AuditLog.Action.DELETE, instance=instance,
               changes={'_deleted': _snapshot(instance)})


# ---- auth signals ----

@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    _write_log(AuditLog.Action.LOGIN, model_name='User',
               object_id=str(user.pk), object_repr=user.username)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is None:
        return
    _write_log(AuditLog.Action.LOGOUT, model_name='User',
               object_id=str(user.pk), object_repr=user.username)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, **kwargs):
    username = (credentials or {}).get('username', '')
    AuditLog.objects.create(
        user=None,
        username_snapshot=username,
        action=AuditLog.Action.LOGIN_FAILED,
        model_name='User',
        object_repr=f'username={username}',
        ip=get_current_ip(),
    )


def connect():
    """Wire all signals. Called from apps.AppConfig.ready()."""
    for model in TRACKED_MODELS:
        pre_save.connect(pre_save_handler, sender=model,
                         dispatch_uid=f'audit_pre_{model.__name__}')
        post_save.connect(post_save_handler, sender=model,
                          dispatch_uid=f'audit_post_{model.__name__}')
        post_delete.connect(post_delete_handler, sender=model,
                            dispatch_uid=f'audit_del_{model.__name__}')
