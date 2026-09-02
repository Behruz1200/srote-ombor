"""CORE-3 — so'rov/javob qolipi bitta joyda.

Nima uchun kerak edi:

* `if request.method != 'POST': return ...'POST only'...` — 14 marta;
* `_json.loads(request.body)` + `except ValueError` — 13 marta;
* `JsonResponse({'ok': False, 'error': ...})` — 112 marta;
* sana oralig'i tahlili (`strptime` + `try/except`) — 11 marta;
* CSV javobi (BOM + `_csv_safe` + Content-Disposition) — 5 marta;
* `Paginator` + havolani qo'lda qurish — 7 marta.

Har biri qo'lda takrorlangani uchun ular ASTA-SEKIN BIR-BIRIDAN
UZOQLASHIB ketgan edi. Eng yorqin misol: sahifalash havolasi ba'zi
sahifada faqat `q` ni saqlab, sana filtrini yo'qotardi (/audit/ va
/prices/history/ da aynan shu xato bor edi).

Bu modul faqat HTTP qatlami — model va pul mantiqi yo'q, shuning uchun
views.py ni import qilmaydi va aylanma import xavfi yo'q.
"""
import csv
import json
from datetime import datetime, timedelta

from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.utils import timezone


# ---------------------------------------------------------------- JSON API

def api_ok(**data):
    """Muvaffaqiyatli JSON javob. Har doim {'ok': True, ...} shaklida."""
    payload = {'ok': True}
    payload.update(data)
    return JsonResponse(payload)


def api_err(message, status=400, **extra):
    """Xato JSON javob. Barcha endpoint bir xil shaklda javob bersin."""
    payload = {'ok': False, 'error': message}
    payload.update(extra)
    return JsonResponse(payload, status=status)


def read_json(request):
    """So'rov tanasidan JSON o'qiydi. Buzuq bo'lsa None qaytaradi.

    Lug'atdan boshqa narsa kelsa ham None — endpoint'lar hammasi
    `data.get(...)` deb yozadi, ro'yxat kelsa AttributeError bo'lardi.
    """
    try:
        data = json.loads((request.body or b'').decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_form_json(request, field):
    """Forma MAYDONIDAGI JSON (multipart yuklashda ishlatiladi).

    Rasm bilan birga yuborilgan qatorlar shu ko'rinishda keladi:
    `request.POST['payload']` ichida JSON matn.
    """
    try:
        data = json.loads(request.POST.get(field) or '{}')
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def json_post(view=None, *, need_body=True, resolve=None):
    """Dekorator: faqat POST + JSON tanasi.

    Tekshirilgan lug'at view'ga `request.json` sifatida beriladi.
    Xato javoblar barcha endpoint uchun BIR XIL bo'ladi — ilgari har
    biri o'z qatorini yozar va vaqt o'tib bir-biridan farq qilardi.

    resolve — ixtiyoriy funksiya(request) -> HttpResponse yoki None.
    None qaytarsa view chaqiriladi; javob qaytarsa o'sha javob beriladi.
    (access.py shu orqali filialni aniqlaydi — web.py model bilmaydi.)
    """
    def wrap(fn):
        from functools import wraps

        @wraps(fn)
        def inner(request, *a, **kw):
            if request.method != 'POST':
                return api_err('POST only', 405)
            if need_body:
                data = read_json(request)
                if data is None:
                    return api_err('bad JSON', 400)
            else:
                data = {}
            request.json = data
            if resolve is not None:
                bad = resolve(request)
                if bad is not None:
                    return bad
            return fn(request, *a, **kw)
        return inner
    return wrap(view) if view else wrap


# ------------------------------------------------------------ sana oralig'i

DATE_FMT = '%Y-%m-%d'


def parse_day(raw):
    """'2026-09-02' -> date. Noto'g'ri yoki bo'sh bo'lsa None."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, DATE_FMT).date()
    except (ValueError, TypeError):
        return None


def filter_by_day_range(qs, field, raw_from, raw_to, *, aware=True):
    """Queryset'ni sana oralig'iga cheklaydi.

    aware=True  -> `field >= kun boshi` va `field < ertangi kun boshi`.
                   Bu shakl INDEKSNI ishlatadi. `field__date__gte` esa
                   har qatorda sanani hisoblab, indeksni ishlatmaydi va
                   katta jadvalda to'liq skanerga olib keladi.
    aware=False -> `field__date__gte / __lte` (DateField uchun).

    Noto'g'ri sana JIM tashlanadi va TOZALANGAN satr qaytariladi, shunda
    forma bo'sh katak ko'rsatadi (foydalanuvchi nima yozganini tushunadi).

    -> (qs, clean_from, clean_to)
    """
    d_from, d_to = parse_day(raw_from), parse_day(raw_to)
    if aware:
        tz = timezone.get_current_timezone()
        if d_from:
            qs = qs.filter(**{f'{field}__gte': timezone.make_aware(
                datetime.combine(d_from, datetime.min.time()), tz)})
        if d_to:
            qs = qs.filter(**{f'{field}__lt': timezone.make_aware(
                datetime.combine(d_to + timedelta(days=1),
                                 datetime.min.time()), tz)})
    else:
        if d_from:
            qs = qs.filter(**{f'{field}__date__gte': d_from})
        if d_to:
            qs = qs.filter(**{f'{field}__date__lte': d_to})
    return (qs,
            d_from.strftime(DATE_FMT) if d_from else '',
            d_to.strftime(DATE_FMT) if d_to else '')


# ---------------------------------------------------------------- sahifalash

def paginate(request, qs, per_page, *, drop=('page', 'export')):
    """Sahifalash + FILTRLARNI SAQLAYDIGAN so'rov satri.

    -> (page, page_qs)

    `page_qs` — joriy GET parametrlari, `page` va `export` olib
    tashlangan holda. Shablon `?{{ page_qs }}&page=2` deb yozadi va
    HECH QANDAY filtr yo'qolmaydi.

    Ilgari har sahifa havolani qo'lda quriar edi
    (`{% if q %}q={{ q }}&{% endif %}...`) va yangi filtr qo'shilganda
    uni havolaga qo'shish UNUTILARDI: /audit/ da sana, /prices/history/
    da esa `q` dan boshqa hamma narsa 2-sahifada yo'qolardi.
    """
    page = Paginator(qs, per_page).get_page(request.GET.get('page'))
    params = request.GET.copy()
    for k in drop:
        params.pop(k, None)
    return page, params.urlencode()


def querystring(request, *, drop=('page', 'export'), **overrides):
    """Joriy GET satri, ba'zi parametrlar olib tashlangan/almashtirilgan."""
    params = request.GET.copy()
    for k in drop:
        params.pop(k, None)
    for k, v in overrides.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    return params.urlencode()


# ----------------------------------------------------------------- CSV

def csv_safe(value):
    """SEC-20: CSV in'ektsiyasi.

    Excel `=`, `+`, `-`, `@` bilan boshlanadigan katakni FORMULA deb
    o'qiydi. Mahsulot nomiga `=cmd|...` yozib qo'ygan kishi eksportni
    ochgan odam kompyuterida buyruq ishga tushirishi mumkin edi.
    """
    s = '' if value is None else str(value)
    if s[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + s
    return s


def csv_writer(filename):
    """CSV javobining SARLAVHASI: fayl nomi + BOM + writer.

    -> (response, writer). Har bir eksport o'z qatorlarini o'zi yozadi,
    lekin sarlavha/BOM/fayl nomi qolipi bitta joyda.

    BOM (\ufeff) shart: usiz Excel lotin/kirill harflarini buzib
    ko'rsatadi — bu ilgari har bir eksportda qo'lda yozilardi va
    bittasida tushib qolsa hech kim sezmasdi.
    """
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp.write('\ufeff')
    return resp, csv.writer(resp)


def csv_response(filename, header, rows, *, prefix_rows=(), suffix_rows=(),
                 max_rows=None, limit_note=''):
    """Tayyor CSV javobi: BOM + xavfsiz kataklar + fayl nomi.

    BOM (\\ufeff) kerak — usiz Excel kirill/lotin harflarini buzib
    ko'rsatadi. `max_rows` berilsa va chegaraga yetilsa, fayl OXIRIGA
    ogohlantirish yoziladi — jim kesilib qolmasin.
    """
    resp, w = csv_writer(filename)
    for r in prefix_rows:
        w.writerow([csv_safe(c) for c in r])
    if header:
        w.writerow([csv_safe(c) for c in header])
    n = 0
    for r in rows:
        if max_rows and n >= max_rows:
            w.writerow([limit_note or
                        f'... eksport {max_rows} qator bilan cheklandi'])
            break
        w.writerow([csv_safe(c) for c in r])
        n += 1
    for r in suffix_rows:
        w.writerow([csv_safe(c) for c in r])
    return resp
