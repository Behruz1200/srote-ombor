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
# 1568 — Anthropic vision uzun chekka uchun samarali chegara. Kattaroq rasmni
# API baribir shu o'lchamga tushiradi, ya'ni 1600 yuborish token tejamaydi,
# faqat yuklashni sekinlashtiradi. Shuning uchun 1568.
MAX_EDGE = 1568
JPEG_QUALITY = 82

PROMPT = """You are reading a supplier delivery note (накладная / faktura / hisob-faktura)
from a shop in Uzbekistan. The document may be in Russian or Uzbek, may be
photographed at an angle, and may have handwritten ticks or notes on it.

The photo may also be ROTATED (the sheet is landscape but shot in portrait, so
the text runs sideways). Read it in whatever orientation it is — do not skip
rows and do not invent a row out of the column headers.

IGNORE ALL HANDWRITING. The shop staff tick rows off and pencil their own
intended retail prices next to the printed ones ("30", "36", "25"). Those
handwritten digits are NOT invoice data — never use them as qty, cost or sum.
Only printed values count.

Extract the printed line items. Reply with ONLY a JSON object — no prose, no
markdown, no code fences.

Schema:
{
  "supplier": "seller / Поставщик / Yetkazib beruvchi company name, or \\"\\"",
  "agent": "PERSON who delivered it, or \\"\\"",
  "agent_phone": "that person's phone number as printed, or \\"\\"",
  "invoice_no": "Номер накладной / Расход № / Накладная №, or \\"\\"",
  "date": "YYYY-MM-DD shipment date, else \\"\\"",
  "total": 0,
  "rows": [
    {
      "name": "the WHOLE product cell exactly as printed (keep original language)",
      "product": "base product WITHOUT the flavour and WITHOUT the volume",
      "type": "the flavour / variety / scent / colour, or \\"\\"",
      "size": "the volume / weight / pack count, or \\"\\"",
      "barcode": "the ШТРИХ КОД / штрих-код column digits (12-14 digit EAN), or \\"\\"",
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
  the distributor's letterhead or logo). Never the buyer.
  WATCH OUT: on many Uzbek delivery notes the block at the top is the BUYER's
  own outlet — "Наименование", "Получатель", "Адрес", "Код" all describe the
  shop receiving the goods. Do NOT copy those into "supplier". If the seller is
  not printed anywhere, return "".
- "agent" = a PERSON's name printed on a line labelled Агент / Торговый агент /
  ТП / Торговый представитель / Экспедитор / Менеджер / Представитель /
  Водитель / Отпустил / Сдал / Agent / Savdo agenti. Usually a surname + initials ("Каримов А.А."). It may be
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
  number, contract number or date here. In particular "Код:" near the buyer's
  name is the CUSTOMER code, not the invoice number — never use it. If no
  document number is printed, return "".
- Never guess a name, phone or number that is not written on the document.
- Some suppliers prefix the name with their own article code:
  "1130 - GOLD шампунь 1,4 л ( 6 шт )". Keep the whole text in "name", but
  leave that leading number OUT of "product" — it is not part of the product
  name. A trailing "( 6 шт )" is the pack size, so it belongs in "size".
- DIGITS vs LETTERS: read numbers inside names carefully. A leading "5" is the
  digit five, not the letter "s" ("5Protection", not "sProtection"); "0" is a
  zero, not "O/о". Copy punctuation exactly as printed — do NOT insert a hyphen
  between two separate words that are printed with a space ("Порошок чистящий",
  not "Порошок-чистящий").
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
- "barcode" = the value in the ШТРИХ КОД / Штрих код / Barcode column. It is a
  long number, usually 13 digits (EAN-13), e.g. "8683130033210". Copy the
  digits exactly.
  HARD RULE: this is NOT the Артикул / Код column. The Артикул is a shorter
  supplier code (7-8 digits like "64318029") in its own column — never put it
  in "barcode". If the ШТРИХ КОД cell for a row is blank, return "" — do not
  borrow the barcode from another row.
- "cost"     = the price the shop actually PAYS for ONE PIECE.
  IMPORTANT: if the invoice has BOTH a list price ("Цена") AND a discounted
  price ("Цена после скидки" / "Цена со скидкой" / "Цена После Скидки"), use
  the DISCOUNTED price as "cost" — that is what is really paid. Then "line_sum"
  must be the DISCOUNTED line total ("Общая сумма после скидки" / "Сумма со
  скидкой"), so that line_sum ÷ cost = qty. NEVER pair a pre-discount price
  with a post-discount total. If there is no discount column, use the plain
  "Цена" and its "Сумма".
- PROMO / BONUS ROWS: some invoices split one item into a main row and a
  follow-up row whose code starts with "Promo-" / "Промо" (or is labelled
  бонус / акция). That promo row repeats the SAME product with the SAME
  Артикул and SAME ШТРИХ КОД, just extra quantity. MERGE it into the main row:
  add the quantities together (and the line sums) and output a SINGLE row for
  that product. This is the ONLY situation where you combine rows.
- "qty"      = the number in the Количество / Кол-во / Soni column, EXACTLY as
  printed, together with its unit. Do not convert it.
- "unit"     = the unit written next to it: шт (pieces), крб / кор / коробка /
  кейс (a BOX), кг, л, dona. Copy it — it tells us whether qty counts pieces
  or boxes.
- "cost"     = the price in the Цена column. This is normally the price of ONE
  PIECE even when the quantity is given in boxes. NEVER the row total.
- "line_sum" = row total (Сумма). Copy it carefully — the app divides it by the
  price to work out how many pieces a box holds, so an error here is costly.
- "date"  = the SHIPMENT date (Дата отгрузки / Отгружено). If only an order
  date (Дата заказа) exists, use that. Uzbek notes print DD.MM.YYYY, so
  "04.06.2026" is 2026-06-04 — never swap day and month.
- "total" = the grand total printed on the Итого / ВСЕГО / Jami line, as a
  number. 0 if absent. Do not compute it yourself — copy what is printed.
- "per_case" = units inside one pack ONLY if the row states it in a separate
  column. Pack markings inside the NAME ("12X1Л", "/16", "280/12") are part of
  the product name — do NOT turn them into per_case and never multiply "qty"
  by them. The app derives the real piece count from Сумма / Цена instead.
- Numbers: strip spaces/apostrophes used as thousand separators and convert the
  decimal comma to a dot. "23 508,8" -> 23508.8 ; "1 656 000,00" -> 1656000.0
- Skip total/summary rows (Итого, ВСЕГО, Jami, Общая сумма, Сумма без
  переоценки, задолженность) and any handwritten-only lines.
- Skip rows with no readable product name.
- If a number is unreadable use 0; if a string is unreadable use "".
- Do not invent rows. Do not merge two DIFFERENT products — the ONLY rows you
  merge are a "Promo-"/bonus row into its matching main row (same barcode).
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


def _units(qty, cost, line_sum):
    """Fakturaning O'Z arifmetikasidan haqiqiy DONA sonini chiqaramiz.

    "Цена" deyarli har doim bitta donaning narxi, "Сумма" esa qator jami.
    Demak:  dona = Сумма / Цена.  Bu ikkala yozuv uslubini ham hal qiladi:

        "5 шт  x 34 000 = 170 000"  -> 170000/34000 =  5 dona  (qty = dona)
        "1 крб x  4 120 = 296 640"  -> 296640/4120  = 72 dona  (qty = QUTI)

    Ya'ni "1 крб" bitta quti, ichida 72 dona. Nomdagi "12X1Л" kabi
    belgilarga ISHONMAYMIZ — faqat qog'ozdagi raqamlar hal qiladi.

    Qaytaradi: (dona, quti_ichida, ishonchli_mi)
    """
    if cost > 0 and line_sum > 0:
        n = line_sum / cost
        r = round(n)
        # butun songa yaqin bo'lsa — hisoblaymiz
        if r >= 1 and abs(n - r) <= max(0.02, n * 0.005):
            per_box = (r / qty) if qty > 0 else 0
            # STK-13: hisoblangan dona (r) qog'ozdagi qty bilan MOS kelsagina
            # "ishonchli". Farq qilsa (masalan Сумма xato o'qilib 5 -> 50, yoki
            # quti holati qty=1/r=72) — TRUSTED emas, xodim tekshirsin. Ilgari
            # doim True qaytarib, xato son "ishonchli" ko'rinardi.
            trusted = (qty > 0 and int(r) == int(qty))
            return float(r), (per_box if per_box > 1.001 else 0), trusted
        return qty, 0, False          # bo'linmadi — foydalanuvchi tekshirsin
    # narx yoki summa yo'q (masalan bonus tovar) — qty o'zi
    return qty, 0, True


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


def _fix_ocr(s):
    """Nom matnidagi tez-tez uchraydigan OCR xatolarini tuzatadi.

    Modelga ko'rsatma bergan bo'lsak ham, ba'zan raqamni harf deb o'qiydi.
    Bu yerda faqat XAVFSIZ, aniq holatlar to'g'irlanadi (sonlar/narxlarga
    tegilmaydi — faqat nom matni).
    """
    if not s:
        return s
    # "sProtection"/"5 protection" -> "5Protection" (DEONICA liniyasi: raqam 5)
    s = re.sub(r'\bs(?=\s?protection\b)', '5', s, flags=re.IGNORECASE)
    # "Порошок-чистящий" -> "Порошок чистящий" (ikki so'z orasiga qo'yilgan
    #  ortiqcha chiziqcha; bu aniq iboraga cheklaymiz)
    s = re.sub(r'(Порошок)\s*-\s*(чистящ)', r'\1 \2', s, flags=re.IGNORECASE)
    return s


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


def _confidence(data):
    """Natija qanchalik ishonchli — fakturaning O'Z arifmetikasiga qarab.

    Burilgan rasmda model qatorlarni chalkashtiradi va qty x narx = summa
    tenglik buziladi. Shu bilan qaysi burilish to'g'ri ekanini bilamiz.
    """
    rows = data.get('rows') or []
    if not rows:
        return -1.0
    ok = sum(1 for r in rows if r.get('sum_ok'))
    score = ok / len(rows)
    total = data.get('total') or 0
    if total > 0:
        calc = sum((r.get('total_qty') or 0) * (r.get('cost') or 0) for r in rows)
        if abs(calc - total) <= max(1.0, total * 0.01):
            score += 0.5
    return score


def extract_invoice(django_file, timeout=120):
    """Rasm -> {supplier, invoice_no, date, rows[]}. Xatoda InvoiceAIError.

    Rasm yonboshiga burilgan bo'lsa (nakladnoy albom, telefon tik ushlagan),
    model qatorlarni chalkashtiradi. Shuning uchun natija ishonchsiz chiqsa
    rasmni burib qayta o'qiymiz va eng yaxshisini olamiz.
    """
    raw, media_type = prepare_image(django_file)
    best = _extract_bytes(raw, media_type, timeout)
    best_score = _confidence(best)
    best['rotated'] = 0
    if best_score >= 1.4:                 # hammasi joyida — burishga hojat yo'q
        return best

    # TOKEN TEJASH: rasm har doim to'g'ri (tik emas) suratga olinsa, burib
    # qayta o'qish (har biri yana bitta to'liq API chaqiruvi) shart emas.
    # AI_INVOICE_ROTATE=0 qo'ysangiz — faqat bitta chaqiruv bo'ladi.
    if not getattr(settings, 'AI_INVOICE_ROTATE', True):
        return best

    for angle in (270, 90, 180):
        try:
            rotated = _rotate_jpeg(raw, angle)
        except Exception:                 # pragma: no cover
            continue
        if rotated is None:
            continue
        try:
            cand = _extract_bytes(rotated, media_type, timeout)
        except InvoiceAIError:
            continue
        score = _confidence(cand)
        if score > best_score:
            best, best_score = cand, score
            best['rotated'] = angle
            if best_score >= 1.4:
                break
    return best


def _rotate_jpeg(raw, angle):
    try:
        from PIL import Image
    except ImportError:                                   # pragma: no cover
        return None
    img = Image.open(io.BytesIO(raw)).rotate(angle, expand=True)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _extract_bytes(raw, media_type, timeout=120):
    """Bitta rasm baytlarini AI'ga yuborib, tozalangan natijani qaytaradi."""
    key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not key:
        raise InvoiceAIError(
            "AI kaliti sozlanmagan. Administrator ANTHROPIC_API_KEY ni "
            "qo'shishi kerak."
        )
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-sonnet-4-5')

    # TOKEN TEJASH — prompt caching:
    # Katta ko'rsatma (~2500 token) SYSTEM blokka o'tkazildi va "ephemeral"
    # kesh bilan belgilandi. Shunda u BIR MARTA to'liq hisoblanadi, keyingi
    # chaqiruvlar (rasmni burib qayta o'qish, ko'p varaqli faktura, ketma-ket
    # fakturalar — 5 daqiqa ichida) o'sha promptni keshdan oladi va ~90% arzon
    # bo'ladi. Rasm har chaqiruvda o'zgargani uchun u keshlanmaydi, lekin
    # prompt ulushi tejaladi.
    body = json.dumps({
        'model': model,
        'max_tokens': 8000,
        'system': [
            {'type': 'text', 'text': PROMPT,
             'cache_control': {'type': 'ephemeral'}},
        ],
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {
                    'type': 'base64',
                    'media_type': media_type,
                    'data': base64.b64encode(raw).decode('ascii'),
                }},
                {'type': 'text',
                 'text': 'Read this delivery note and return ONLY the JSON '
                         'object described in the instructions.'},
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
        u = payload.get('usage') or {}
        # Kesh ishlayaptimi — logdan ko'rish uchun (cache_read > 0 = tejaldi)
        logger.info(
            'invoice_ai tokens: in=%s cache_write=%s cache_read=%s out=%s',
            u.get('input_tokens'), u.get('cache_creation_input_tokens'),
            u.get('cache_read_input_tokens'), u.get('output_tokens'))
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
        name = _fix_ocr((r.get('name') or '').strip())
        if not name:
            continue
        qty = _num(r.get('qty'))
        cost = _num(r.get('cost'))
        line_sum = _num(r.get('line_sum'))
        unit = (r.get('unit') or '').strip()[:20]
        # Miqdorni qog'ozning o'z hisobidan chiqaramiz (izohga qarang).
        total_qty, per_case, sum_ok = _units(qty, cost, line_sum)
        qty_note = ''
        if per_case > 1:
            qty_note = '%g %s × %g dona' % (qty, unit or 'quti', per_case)
        product = _fix_ocr((r.get('product') or '').strip())[:200]
        # Shtrix-kod: faqat raqamlar; Артикул (7-8 xonali) tasodifan tushmasin —
        # haqiqiy EAN odatda 12-14 xonali. Qisqasini tashlab yuboramiz.
        barcode = re.sub(r'\D', '', str(r.get('barcode') or ''))
        if len(barcode) < 8 or len(barcode) > 14:
            barcode = ''
        rows.append({
            'name': name[:200],
            # AI bo'lmagan/bo'sh qoldirgan bo'lsa — butun nom mahsulot bo'ladi
            'product': product or name[:200],
            'type': _fix_ocr((r.get('type') or '').strip())[:120],
            'size': (r.get('size') or '').strip()[:60],
            'barcode': barcode,
            'qty': qty,
            'unit': unit,
            'per_case': per_case,
            'total_qty': total_qty,
            'qty_note': qty_note,
            'cost': cost,
            'line_sum': line_sum,
            'sum_ok': sum_ok,
        })

    return {
        'supplier': (data.get('supplier') or '').strip()[:200],
        'agent': (data.get('agent') or '').strip()[:120],
        'agent_phone': _phone(data.get('agent_phone')),
        'invoice_no': (data.get('invoice_no') or '').strip()[:80],
        'date': (data.get('date') or '').strip()[:20],
        'total': _num(data.get('total')),
        'rows': rows,
    }
