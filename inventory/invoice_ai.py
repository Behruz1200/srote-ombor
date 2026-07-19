"""Faktura (накладная) rasmidan qatorlarni o'qish — Anthropic Claude vision.

Yetkazib beruvchining qog'oz fakturasini telefonda suratga olib, mahsulot
nomi / miqdori / tannarxi avtomatik jadvalga tushadi. Natija HAR DOIM
foydalanuvchi tekshiruvidan o'tadi — to'g'ridan-to'g'ri omborga yozilmaydi.

Kalit: ANTHROPIC_API_KEY (env). Kalitsiz funksiya o'chiq turadi.
"""
import base64
import io
import json
import logging
import re
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
MAX_EDGE = 1600          # rasmni kichraytiramiz — tez va arzon
JPEG_QUALITY = 85

PROMPT = """You are reading a supplier delivery note (накладная / faktura / hisob-faktura)
from a shop in Uzbekistan. The document may be in Russian or Uzbek, may be
photographed at an angle, and may have handwritten ticks or notes on it.

Extract the printed line items. Reply with ONLY a JSON object — no prose, no
markdown, no code fences.

Schema:
{
  "supplier": "seller / Поставщик / Yetkazib beruvchi company name, or \\"\\"",
  "agent": "PERSON who delivered it, or \\"\\"",
  "agent_phone": "that person's phone number as printed, or \\"\\"",
  "invoice_no": "Номер накладной / Расход № / Накладная №, or \\"\\"",
  "date": "YYYY-MM-DD if a shipment date is printed, else \\"\\"",
  "rows": [
    {
      "name": "the WHOLE product cell exactly as printed (keep original language)",
      "product": "base product WITHOUT the flavour and WITHOUT the volume",
      "type": "the flavour / variety / scent / colour, or \\"\\"",
      "size": "the volume / weight / pack count, or \\"\\"",
      "qty": 0,
      "unit": "шт / крб / кг / dona, or \\"\\"",
      "per_case": 0,
      "cost": 0,
      "line_sum": 0
    }
  ]
}

Field rules:
- "supplier" = the SELLING COMPANY (Поставщик / Продавец / Yetkazib beruvchi /
  the letterhead name at the top). Never the buyer.
- "agent" = a PERSON's name printed on a line labelled Агент / Торговый агент /
  Экспедитор / Менеджер / Представитель / Водитель / Отпустил / Сдал / Agent /
  Savdo agenti. Usually a surname + initials ("Каримов А.А."). It may be
  handwritten next to a signature — read it if legible.
  HARD RULES: the agent is NEVER the buyer (Покупатель / Получатель / Кому),
  NEVER the supplier, and NEVER a company name. If no line carries one of
  those labels, return "" — do not fall back to any other name on the page.
- "agent_phone" = a phone printed on or next to that person's line (Тел / Тел. /
  Телефон / Моб / Tel). Uzbek numbers look like +998 90 123 45 67,
  (90) 123-45-67 or 901234567. Copy the digits exactly as printed.
  If several phones appear, take the one closest to the agent's name; if you
  cannot tell whose it is, return "". A company switchboard number printed in
  the letterhead is NOT the agent's phone.
- "invoice_no" = the document number from the line reading НАКЛАДНАЯ / Расход /
  Счёт-фактура / Hisob-faktura, e.g. "N RN-004521" -> "RN-004521".
  HARD RULE: never return an ИНН / СТИР / tax id, ОКЭД, bank account, phone
  number, contract number or date here. If no document number is printed,
  return "".
- Never guess a name, phone or number that is not written on the document.
- "name" = copy the entire product cell: the leading generic word
  (Шампунь / Мыло / Паста / Сок), the brand, and the size or weight
  ("Шампунь Head&Shoulders 400мл"). Do not shorten it to just the brand.

SPLITTING name INTO product / type / size — this matters most:
A delivery note lists every flavour and every pack size on its own line, but
the shop stores ONE product with many variants. Cut each name three ways:

    "Влажные Салфетки ECO Aloelik Yashil 120шт"
        product "Влажные Салфетки ECO"   type "Aloelik Yashil"   size "120шт"
    "Влажные Салфетки ECO Kremli Oq 72шт"
        product "Влажные Салфетки ECO"   type "Kremli Oq"        size "72шт"
    "Зубная паста Colgate Fresh 100мл"
        product "Зубная паста Colgate"   type "Fresh"            size "100мл"
    "Мыло Safeguard 90г"
        product "Мыло Safeguard"         type ""                 size "90г"

- Read ALL the rows first, then decide where to cut. Rows belonging to the same
  family MUST get a byte-identical "product" — that is what groups them.
- "product" = the generic word + brand that the sibling rows share.
- "type"    = what distinguishes this line from its siblings: flavour, scent,
  colour, series, target user (Fresh, Lavanda, Antibacterial, Detskiy,
  Erkaklar uchun, Ko'k). "" if the line has no such word.
- "size"    = number + unit only (100мл, 72шт, 1.8кг, 5 dona). "" if absent.
- NEVER leave the volume inside "product" or "type".
- If a product genuinely has no siblings, still split off its size.
- "qty"      = Количество / Кол-во / Soni for that row.
- "cost"     = UNIT price (Цена / Нархи / Цена с переоценкой). NEVER the row total.
- "line_sum" = row total (Сумма). Use 0 if the column is absent.
- "per_case" = units inside one case when the row states it (e.g. "5 шт" in a
  "Количество в кейсе" column, or a trailing "/48" or "*300" in the name).
  Use 0 when the row is plain pieces.
- Numbers: strip spaces/apostrophes used as thousand separators and convert the
  decimal comma to a dot. "23 508,8" -> 23508.8 ; "1 656 000,00" -> 1656000.0
- Skip total/summary rows (Итого, ВСЕГО, Jami, Общая сумма, Сумма без
  переоценки, задолженность) and any handwritten-only lines.
- Skip rows with no readable product name.
- If a number is unreadable use 0; if a string is unreadable use "".
- Do not invent rows and do not merge two rows together.
"""


class InvoiceAIError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xato."""


def is_enabled():
    return bool(getattr(settings, 'ANTHROPIC_API_KEY', ''))


def prepare_image(django_file):
    """Rasmni kichraytirib, JPEG bytes qaytaradi (tez + arzon so'rov uchun)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:                                  # pragma: no cover
        django_file.seek(0)
        return django_file.read(), 'image/jpeg'
    django_file.seek(0)
    img = Image.open(django_file)
    img = ImageOps.exif_transpose(img)                   # telefon burilishini to'g'rilash
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), 'image/jpeg'


def _num(v):
    """Faktura raqamlarini floatga o'giradi.

    Ikki xil yozuv aralash keladi:
      rus uslubi  — vergul O'NLIK ajratgich:  "23 508,8" -> 23508.8
      ingliz usl. — vergul MINGLIK ajratgich: "13,000"   -> 13000.0
    Farqlash: vergul ortidan ROPPA-ROSA 3 raqam bo'lsa — minglik,
    1-2 raqam bo'lsa — o'nlik ajratgich.
    """
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or '').strip()
    if not s:
        return 0.0
    for ch in ('\u00a0', '\u202f', '\u2009', ' ', "'", '`', '\u2019'):
        s = s.replace(ch, '')
    s = re.sub(r'[^0-9,.\-]', '', s)
    if s in ('', '-', '.', ','):
        return 0.0
    neg = s.startswith('-')
    s = s.lstrip('-')
    if s.count('.') > 1:
        s = s.replace('.', '')
    if s.count(',') > 1:
        s = s.replace(',', '')
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        frac = s.rsplit(',', 1)[-1]
        s = s.replace(',', '') if len(frac) == 3 else s.replace(',', '.')
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if neg else val


def _phone(v):
    """Telefonni O'zbekiston formatiga keltiradi: +998 XX XXX XX XX.

    Tanib bo'lmasa — asl matnni qaytaradi (qo'lda tuzatiladi).
    """
    s = str(v or '').strip()
    if not s:
        return ''
    digits = re.sub(r'\D', '', s)
    if len(digits) == 12 and digits.startswith('998'):
        pass
    elif len(digits) == 9:                    # 901234567
        digits = '998' + digits
    elif len(digits) == 10 and digits.startswith('8'):   # 8 90 123 45 67
        digits = '998' + digits[1:]
    else:
        return s[:40]                          # notanish shakl — o'zgartirmaymiz
    return '+{} {} {} {} {}'.format(
        digits[:3], digits[3:5], digits[5:8], digits[8:10], digits[10:12])


def _parse_payload(text):
    """Model javobidan JSON ajratib olamiz (fence bo'lsa ham)."""
    t = (text or '').strip()
    if t.startswith('```'):
        t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
        t = re.sub(r'```\s*$', '', t).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r'\{.*\}', t, re.S)      # matn ichidagi birinchi JSON
        if not m:
            raise InvoiceAIError("AI javobini o'qib bo'lmadi. Rasmni aniqroq oling.")
        try:
            return json.loads(m.group(0))
        except ValueError:
            raise InvoiceAIError("AI javobini o'qib bo'lmadi. Rasmni aniqroq oling.")


def extract_invoice(django_file, timeout=120):
    """Rasm -> {supplier, invoice_no, date, rows[]}. Xatoda InvoiceAIError."""
    key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not key:
        raise InvoiceAIError(
            "AI kaliti sozlanmagan. Administrator ANTHROPIC_API_KEY ni "
            "qo'shishi kerak."
        )
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-sonnet-4-5')

    raw, media_type = prepare_image(django_file)
    body = json.dumps({
        'model': model,
        'max_tokens': 8000,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {
                    'type': 'base64',
                    'media_type': media_type,
                    'data': base64.b64encode(raw).decode('ascii'),
                }},
                {'type': 'text', 'text': PROMPT},
            ],
        }],
    }).encode('utf-8')

    req = urllib.request.Request(API_URL, data=body, method='POST')
    req.add_header('content-type', 'application/json')
    req.add_header('x-api-key', key)
    req.add_header('anthropic-version', ANTHROPIC_VERSION)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = json.loads(e.read().decode('utf-8')).get('error', {}).get('message', '')
        except Exception:
            pass
        logger.warning('invoice_ai HTTP %s: %s', e.code, detail)
        if e.code == 401:
            raise InvoiceAIError("AI kaliti noto'g'ri (401).")
        if e.code == 429:
            raise InvoiceAIError("AI limiti tugadi yoki band (429). Birozdan keyin urining.")
        raise InvoiceAIError(f"AI xatosi ({e.code}). {detail}"[:200])
    except urllib.error.URLError as e:
        raise InvoiceAIError(f"Tarmoq xatosi: {e.reason}")
    except Exception as e:                                # pragma: no cover
        logger.exception('invoice_ai failed')
        raise InvoiceAIError(f"Kutilmagan xato: {e}")

    parts = payload.get('content') or []
    text = ''.join(p.get('text', '') for p in parts if p.get('type') == 'text')
    data = _parse_payload(text)

    rows = []
    for r in (data.get('rows') or []):
        name = (r.get('name') or '').strip()
        if not name:
            continue
        qty = _num(r.get('qty'))
        per_case = _num(r.get('per_case'))
        cost = _num(r.get('cost'))
        line_sum = _num(r.get('line_sum'))
        # kеys bo'lsa — jami dona
        total_qty = qty * per_case if per_case > 1 else qty
        product = (r.get('product') or '').strip()[:200]
        rows.append({
            'name': name[:200],
            # AI bo'lmagan/bo'sh qoldirgan bo'lsa — butun nom mahsulot bo'ladi
            'product': product or name[:200],
            'type': (r.get('type') or '').strip()[:120],
            'size': (r.get('size') or '').strip()[:60],
            'qty': qty,
            'unit': (r.get('unit') or '').strip()[:20],
            'per_case': per_case,
            'total_qty': total_qty,
            'cost': cost,
            'line_sum': line_sum,
            # сумма ustuni bo'lsa — qty × cost bilan solishtiramiz
            'sum_ok': (line_sum <= 0 or cost <= 0 or qty <= 0
                       or abs(qty * cost - line_sum) <= max(1.0, line_sum * 0.02)),
        })

    return {
        'supplier': (data.get('supplier') or '').strip()[:200],
        'agent': (data.get('agent') or '').strip()[:120],
        'agent_phone': _phone(data.get('agent_phone')),
        'invoice_no': (data.get('invoice_no') or '').strip()[:80],
        'date': (data.get('date') or '').strip()[:20],
        'rows': rows,
    }
