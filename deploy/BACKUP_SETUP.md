# OPS-1 — Kunlik shifrlangan offsite zaxira (Telegram)

Har kecha 03:00 da baza `pg_dump` qilinadi, AES256 bilan shifrlanadi va
Telegram'ga yuboriladi. Mahalliy nusxa 30 kundan keyin o'chiriladi.

## 1. Serverda `gpg` va `pg_dump` borligini tekshiring

```bash
gpg --version && pg_dump --version
# yo'q bo'lsa:
apt-get update && apt-get install -y gnupg postgresql-client
```

## 2. Sirlarni `/etc/yurit/env` ga qo'shing (chatga YOZMANG)

```
BACKUP_GPG_PASSPHRASE=<kuchli-parol-shu-yerda>
# MUHIM: zaxirani ilova papkasidan TASHQARIDA saqlang — aks holда deploy'даgi
# `rsync --delete` uni o'chirib yuborishi mumkin.
BACKUP_DIR=/opt/yurit/backups
# Ixtiyoriy: zaxira alohida maxfiy kanalга borsin (bo'sh bo'lsa —
# odatdagi TELEGRAM_CHAT_IDS ning birinchisiga ketadi):
BACKUP_TELEGRAM_CHAT_ID=<maxfiy-kanal-yoki-chat-id>
# Ixtiyoriy: standart 30 kun.
BACKUP_RETAIN_DAYS=30
```

> `BACKUP_DIR=/opt/yurit/backups` ni albatta qo'ying. deploy `rsync --delete`
> `backups` ni istisno qiladi, lekin papka ilovadan tashqarida bo'lса butunlay
> xavfsiz.

> ⚠️ `BACKUP_GPG_PASSPHRASE` ni XAVFSIZ joyda alohida saqlang. U yo'qolса,
> shifrlangan zaxiralarни OCHIB BO'LMAYDI. Bu parol serverdan tashqarida
> (parol menejeringizда) ham turishi shart.

## 3. Bir marta qo'lda sinab ko'ring

```bash
cd /opt/yurit/app
/opt/yurit/venv/bin/python manage.py backup_db
# Telegram'да shifrlangan .dump.gpg fayl paydo bo'lishi kerak.
```

## 4. systemd timer'ni o'rnating

```bash
cp /opt/yurit/app/deploy/yurit-backup.service /etc/systemd/system/
cp /opt/yurit/app/deploy/yurit-backup.timer   /etc/systemd/system/
# .service dagi venv yo'lini tekshiring (yurit.service bilan bir xil bo'lsin).
systemctl daemon-reload
systemctl enable --now yurit-backup.timer
systemctl list-timers yurit-backup.timer   # keyingi ishga tushish vaqtini ko'rsatadi
```

## Tiklash (restore) — sinab ko'ring!

```bash
# 1. Telegram'дан .dump.gpg ni yuklab oling, so'ng ochib oling:
gpg --batch --pinentry-mode loopback --passphrase '<parol>' \
    --decrypt yurit-YYYYMMDD-HHMMSS.dump.gpg > restore.dump

# 2. Bo'sh/test bazaga tiklang (custom format => pg_restore):
pg_restore --no-owner --dbname <test_db> restore.dump
```

Zaxira ISHLAYOTGANINI bilishning yagona yo'li — vaqti-vaqti bilan tiklashni
sinab ko'rish. Kamida bir marta yuqoridagi restore'ni bajarib ko'ring.

## Xatolik bo'lsa

Zaxira biror bosqichda yiqilса, bot Telegram'ga «🛑 Zaxira XATOSI» xabarини
yuboradi — jimgina to'xtaб qolmaydi. Log:

```bash
journalctl -u yurit-backup.service --since '1 day ago'
```
