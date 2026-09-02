"""ARCH-2 — har bir so'rovda takrorlanadigan uchta savol.

Deyarli har bir view bir xil narsadan boshlanadi: bu kim, qaysi filialdan,
qaysi smenada? Bu javoblar views.py ichida uch xil joyda yotardi (148, 5163,
5950-qatorlar) va shuning uchun yangi view yozganda ularni topish qiyin edi.

Bu modul faqat SO'ROV CHEGARASI bilan shug'ullanadi: ruxsat, filial, smena
va foydalanuvchi kiritgan kodni standart shaklga keltirish. Bu yerda biror
sahifa mantiqi yo'q, shuning uchun views.py ni ham, views_pos.py ni ham
import qilmaydi — ikkalasi buni import qiladi.
"""
import re

from django.shortcuts import redirect
from django.http import HttpResponseForbidden

from .models import Branch, Shift

POS_BRANCH_SESSION_KEY = 'pos_branch_id'


def admin_required(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_admin():
            return HttpResponseForbidden(
                "<h3>Ruxsat yo'q. Bu sahifa faqat administrator uchun.</h3>"
            )
        return view_func(request, *args, **kwargs)
    return wrapper

def get_user_branch(user):
    """Sotuvchi uchun majburiy filial. Admin uchun None bo'lishi mumkin."""
    return user.branch

def normalize_code(typed):
    """Kiritilgan kodni standart shaklga keltirish: OYO1 → OYO-0001"""
    if not typed:
        return ''
    t = typed.strip().upper().replace(' ', '')
    m = re.match(r'^([A-Z]+)-?(\d+)$', t)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):04d}'
    return t

def _open_shift_for(branch):
    """Returns the single open shift for a branch, or None."""
    return Shift.objects.filter(branch=branch, status=Shift.Status.OPEN).first()

def _user_branch_or_403(request):
    """Sellers are locked to their assigned branch. Admins can pick
    which branch the POS operates on via a dropdown — the choice is
    stored in the session under POS_BRANCH_SESSION_KEY.

    Fallback order for admins:
      1) session-selected branch
      2) ?branch_id= query (used once by pos_terminal to set the session)
      3) admin's own assigned branch (if set)
      4) branch of admin's own open shift
      5) any open shift's branch
      6) first active branch
    """
    if not request.user.is_admin():
        return request.user.branch

    sb_id = request.session.get(POS_BRANCH_SESSION_KEY)
    if sb_id:
        b = Branch.objects.filter(pk=sb_id, is_active=True).first()
        if b:
            return b

    q_id = request.GET.get('branch_id')
    if q_id:
        try:
            b = Branch.objects.filter(pk=int(q_id), is_active=True).first()
            if b:
                return b
        except ValueError:
            pass

    if request.user.branch_id:
        return request.user.branch

    own_shift = Shift.objects.filter(
        opened_by=request.user, status=Shift.Status.OPEN
    ).select_related('branch').order_by('-opened_at').first()
    if own_shift:
        return own_shift.branch

    any_shift = Shift.objects.filter(
        status=Shift.Status.OPEN
    ).select_related('branch').order_by('-opened_at').first()
    if any_shift:
        return any_shift.branch

    return Branch.objects.filter(is_active=True).first()


# ---------------------------------------------------------------- CORE-3
# POS endpoint'larining boshlanishi 17 marta bir xil yozilgan edi:
#   POST tekshiruvi -> JSON o'qish -> filialni aniqlash -> 403.
# Endi bitta dekorator. `request.branch` va `request.json` tayyor keladi.

def _resolve_branch(request):
    from .web import api_err
    branch = _user_branch_or_403(request)
    if branch is None:
        return api_err('no branch', 403)
    request.branch = branch
    return None


def pos_api(view=None, *, need_body=True, need_branch=True):
    """POST + JSON + (ixtiyoriy) filial. request.json / request.branch."""
    from .web import json_post
    return json_post(view, need_body=need_body,
                     resolve=_resolve_branch if need_branch else None)
