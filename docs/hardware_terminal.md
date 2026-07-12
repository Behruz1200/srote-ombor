# Hardware POS terminal integratsiyasi (UzCard / Humo)

Bu doc UzCard yoki Humo fizik POS terminalini yurit tizimiga ulashning
amaliy yo'lini tasvirlaydi. **Hozir kod yo'q** — chunki bu integratsiyaning
har bir bosqichi real apparat va vendor SDK'ga bog'liq. Bu fayl
boshlash uchun yo'l xaritasi.

## Konteksti

Hozir POS'da to'lov turlari:
- Naqd, Karta (faqat belgi), O'tkazma — fizik terminal yo'q
- QR to'lov: Click/Payme/Uzum (`inventory/payments.py` — stub)
- Mixed payment: bitta chekni bo'lish

UzCard/Humo fizik terminali — bu **alohida apparat**, kassir uni POS sahifa
bilan sinxron ishlatadi (mijoz kartani urganini kutadi). Brauzer kompyuterga
ulangan terminal bilan bevosita gaplasholmaydi.

## Variantlar

### A. Manual confirmation (eng oson, hozir ham qila olamiz)
Kassir terminalga summa kiritadi, mijoz kartani uradi, terminal o'zi
chekka chiqaradi. POS'da kassir shunchaki "Karta" tugmasini bosadi.
Ikki tizim mustaqil — moslama yo'q.

**Yaxshi:** hech qanday qo'shimcha ish kerak emas. Hozirgi `payment_method='card'`
xuddi shunday ishlaydi.

**Yomon:** ikki marta ma'lumot kiritish, summa noto'g'ri kiritilsa farq paydo bo'ladi.

### B. Web Serial API (Chrome 89+)
Brauzer to'g'ridan-to'g'ri RS-232/USB-to-serial orqali terminalga ulanadi:

```javascript
const port = await navigator.serial.requestPort();
await port.open({baudRate: 9600});
const writer = port.writable.getWriter();
await writer.write(new TextEncoder().encode('PAY:50000\r\n'));
```

**Yaxshi:** brauzer'da ishlaydi, alohida ilova kerak emas.

**Yomon:**
- Faqat HTTPS'da ishlaydi (localhost yoki real sertifikat)
- iOS Safari'da yo'q
- Har vendor o'z RS-232 protokoliga ega — UzCard, Humo, Verifone, Ingenico
  hammasi farqli
- Real testlash uchun fizik terminal kerak

**Boshlash uchun:** vendor'dan terminalning RS-232 protokol hujjatini
oling. Asosiy buyruqlar:
- `INIT` — boshlanish
- `PAY <amount>` — to'lov so'rovi
- `STATUS` — natija (success/decline/cancel)
- `RECEIPT` — chek chop etish

### C. Native bridge (eng kuchli, eng ko'p ish)
Mahalliy daemon (Python yoki Go) terminalga RS-232/USB orqali ulanadi
va `localhost:8001` da REST API ko'rsatadi. Brauzer shu API'ga
`POST /pay` yuboradi.

```
+----------+   HTTP   +----------+   serial   +----------+
| Brauzer  | -------> | Daemon   | ---------> | Terminal |
+----------+          +----------+            +----------+
```

**Yaxshi:** har qanday brauzer'da ishlaydi, vendor SDK to'liq ishlatiladi
(masalan UzCard'ning rasmiy C++ SDK'si).

**Yomon:**
- Har kassa kompyuteriga daemon o'rnatish kerak
- Daemon yangilash deploy chiqaradi
- Cross-platform mas'uliyat (Windows + Linux + Mac)

## Tavsiya

Kichik biznes uchun **A variant** yetarli. Apparat va POS mustaqil ishlaydi,
to'lov muvaffaqiyatli bo'lgach kassir "Karta" tanlaydi. Hisobotlarda
match qilish uchun:

- Karta to'lovlari `payment_method='card'` saqlanadi
- Smen yopilganda kassir terminalning Z-otchetini POS'ning kassa hisoboti
  bilan solishtiradi
- Farq bo'lsa shift_close formada izoh sifatida yoziladi

Ko'lam o'sganda **C variant** (native bridge) eng yaxshi yechim.

## Vendor kontaktlari

- **UzCard:** integration@uzcard.uz — terminal SDK + sertifikat so'rash
- **Humo:** humo.uz orqali ariza
- **Verifone (terminal apparati):** verifone.uz

## Talab qilingan ishlar (kelajakda)

- [ ] Vendor'dan protokol hujjati va test terminali olish
- [ ] Python daemon prototipi (variant C)
- [ ] POS UI'da "Karta terminalida to'lash" tugmasi
- [ ] Smen yopilganda Z-otchet bilan auto-match
- [ ] Audit log'da terminal txn ID saqlash
