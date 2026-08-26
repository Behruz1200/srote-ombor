#!/bin/bash
# yurit POS -> koreysbozor.uz deploy
# Ishlatish: ./deploy.sh
set -e
cd "$(dirname "$0")"
# Lokal darvoza (gate) tekshiruvlari dev rejimida ishlaydi — SEC-10 tufayli
# DEBUG=0 da SECRET_KEY talab qilinadi; serverда u /etc/yurit/env'da bor.
echo "== Django check =="
DEBUG=1 ./venv/bin/python manage.py check
echo "== JS syntax check (inline <script> on all pages) =="
DEBUG=1 ./venv/bin/python manage.py check_js
echo "== Kod sinxron (rsync) =="
rsync -az --delete --chmod=Da+rx,Fa+r \
  --exclude venv --exclude .git --exclude __pycache__ --exclude '*.pyc' \
  --exclude db.sqlite3 --exclude 'db.sqlite3-*' --exclude staticfiles --exclude .DS_Store \
  --exclude media --exclude .env \
  --exclude scripts --exclude seed.py \
  ./ root@45.138.159.120:/opt/yurit/app/
echo "== Server deploy (migrate + collectstatic + restart) =="
ssh root@45.138.159.120 '/usr/local/bin/yurit-deploy'
echo "== Smoke test =="
sleep 3
# OPS-6: smoke test endi DEPLOYNI YIQITADI agar sayt 200 qaytarmasa (ilgari
# har doim 0 bilan chiqib, 500 qaytarayotgan deployni "yashil" ko'rsatardi).
code=$(curl -s -o /dev/null -w "%{http_code}" https://koreysbozor.uz/login/)
echo "https://koreysbozor.uz/login/ -> $code"
if [ "$code" != "200" ]; then
  echo "❌ SMOKE TEST FAILED (kutilgan 200, keldi $code) — deployни tekshiring!"
  exit 1
fi
echo "✅ Smoke OK"
