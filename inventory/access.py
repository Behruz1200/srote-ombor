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

from django.db.models import Q
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
        # ROLE-1: filiali biriktirilmagan admin — YOPIQ. Aks holda
        # scope_branch() False qaytarib, sahifalar bo'sh chiqardi va
        # sababi tushunarsiz bo'lardi.
        if request.user.scope_branch() is False:
            return HttpResponseForbidden(
                "<h3>Filial biriktirilmagan.</h3>"
                "<p>Hisobingizga filial biriktirilmaguncha bu sahifa "
                "ochilmaydi. Do'kon egasiga murojaat qiling.</p>"
            )
        return view_func(request, *args, **kwargs)
    return wrapper

def owner_required(view_func):
    """ROLE-2 — faqat EGASI (SuperUser).

    Butun tizimga tegadigan ishlar shu yerdan o'tadi: filial ochish,
    global katalog, umumiy narx ro'yxati, aksiyalar, audit jurnali,
    admin tayinlash. Filial admini bularni ko'rmasin ham, o'zgartira
    olmasin ham — bitta filialdagi qaror boshqa filialga tegib ketmasin.
    """
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_owner():
            return HttpResponseForbidden(
                "<h3>Ruxsat yo'q.</h3>"
                "<p>Bu sahifa butun tizimga tegadi, shuning uchun faqat "
                "do'kon egasi (SuperUser) uchun ochiq.</p>"
            )
        return view_func(request, *args, **kwargs)
    wrapper.__name__ = getattr(view_func, '__name__', 'wrapped')
    wrapper.__doc__ = view_func.__doc__
    return wrapper


# ---------------------------------------------------------------- ROLE-3
# FILIAL CHEGARASI — bitta joyda.
#
# Ilgari har bir sahifa filialni o'zi tanlardi:
#     branch_id = request.GET.get('branch') or ''
#     qs = qs.filter(branch_id=int(branch_id))
# va ro'yxatni `Branch.objects.filter(is_active=True)` dan qurardi.
# Ya'ni ?branch=2 deb yozgan HAR KIM boshqa filial raqamlarini ko'rardi.
#
# Endi uchala savolga uchta funksiya javob beradi va ular MAJBURLAYDI:
#   visible_branches(request) — tanlash ro'yxati (adminga faqat o'ziniki)
#   scoped(qs, request, field) — so'rovni filialga qisadi
#   picked_branch_id(request, raw) — GET dagi tanlovni tekshiradi


def visible_branches(request, *, active_only=True, order='name'):
    """Bu foydalanuvchiga ko'rinadigan filiallar.

    Egasi — hammasi; boshqasi — faqat o'ziniki (biriktirilmagan bo'lsa
    bo'sh ro'yxat). Sahifalardagi "Filial" ochiladigan ro'yxati SHU
    yerdan qurilsin — shunda adminda tanlov bitta bo'ladi va u boshqa
    filial nomini bilib ham olmaydi.
    """
    qs = Branch.objects.all()
    if active_only:
        qs = qs.filter(is_active=True)
    scope = request.user.scope_branch()
    if scope is None:
        return qs.order_by(order) if order else qs
    if scope is False:
        return qs.none()
    qs = qs.filter(pk=scope.pk)
    return qs.order_by(order) if order else qs


def scoped(qs, request, field='branch'):
    """So'rovni foydalanuvchi filiali bilan cheklaydi.

    Egasi uchun so'rov o'zgarmaydi. Filial admini/sotuvchisi uchun
    `field` bo'yicha qisiladi; filiali yo'q bo'lsa — BO'SH natija
    (ochiq qoldirilmaydi).

    `field` ko'p pog'onali bo'lishi mumkin ("sale__branch"), va bir
    nechta yo'lni vergul bilan berish mumkin ("from_branch,to_branch") —
    u holda ULARDAN BIRI mos kelsa yetarli (transfer ikki filialga
    tegadi: admin o'ziniki ishtirok etgan transferni ko'rishi kerak).
    """
    scope = request.user.scope_branch()
    if scope is None:
        return qs
    if scope is False:
        return qs.none()
    fields = [f.strip() for f in field.split(',') if f.strip()]
    if not fields:
        return qs.none()
    cond = Q()
    for f in fields:
        cond |= Q(**{f: scope})
    return qs.filter(cond)


def visible_users(request, *, active_only=True, order='username'):
    """Bu foydalanuvchiga ko'rinadigan xodimlar.

    Egasi — hammasi; filial admini — FAQAT o'z filiali xodimlari.
    "Sotuvchi bo'yicha" filtrlari shu yerdan qurilsin: ilgari ular
    `User.objects.filter(is_active=True)` dan qurilar va Xonqa
    adminiga Koreys Bozor kassirlarining ismini ko'rsatardi.
    """
    from .models import User
    qs = User.objects.all()
    if active_only:
        qs = qs.filter(is_active=True)
    scope = request.user.scope_branch()
    if scope is False:
        return qs.none()
    if scope is not None:
        qs = qs.filter(branch=scope)
    return qs.order_by(order) if order else qs


def picked_branch_id(request, raw):
    """Sahifadagi "Filial" filtri uchun yakuniy qiymat.

    Egasi nimani tanlagan bo'lsa — o'shani (yaroqsiz bo'lsa bo'sh).
    Boshqa hamma uchun — HAR DOIM o'z filiali, GET nima deyishidan
    qat'i nazar. Qaytadi: (branch_id_str, branch_obj_or_None).
    """
    scope = request.user.scope_branch()
    if scope is not None:
        return ('' if scope is False else str(scope.pk)), (scope or None)
    if not raw:
        return '', None
    try:
        b = Branch.objects.filter(pk=int(raw)).first()
    except (TypeError, ValueError):
        return '', None
    return (str(b.pk), b) if b else ('', None)


def branch_or_403(request, branch):
    """Bitta yozuvni ochishdan oldin: u shu foydalanuvchinikimi?

    Sahifa ro'yxatni to'g'ri qisgani bilan, kimdir /shifts/57/ deb
    to'g'ridan-to'g'ri kirishi mumkin. Har bir DETAL sahifasi shu
    tekshiruvdan o'tsin.
    """
    if request.user.can_see_branch(branch):
        return None
    return HttpResponseForbidden(
        "<h3>Ruxsat yo'q.</h3><p>Bu yozuv boshqa filialga tegishli.</p>"
    )


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
    # ROLE-3: pastdagi "boshqa filialga o'tish" zanjiri FAQAT egasi
    # uchun. Ilgari sharti is_admin() edi — ya'ni filial admini
    # ?branch_id= bilan yoki hatto biriktirilmagan bo'lsa "birinchi
    # ochiq smena" orqali BOSHQA filial kassasiga tushib qolardi.
    if not request.user.is_owner():
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
