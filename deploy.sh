#!/bin/bash
# yurit POS -> koreysbozor.uz deploy
# Ishlatish: ./deploy.sh
set -e
cd "$(dirname "$0")"
echo "== Django check =="
./venv/bin/python manage.py check
echo "== JS syntax check (inline <script> on all pages) =="
./venv/bin/python manage.py check_js
echo "== Kod sinxron (rsync) =="
rsync -az --delete \
  --exclude venv --exclude .git --exclude __pycache__ --exclude '*.pyc' \
  --exclude db.sqlite3 --exclude 'db.sqlite3-*' --exclude staticfiles --exclude .DS_Store \
  ./ root@45.138.159.120:/opt/yurit/app/
echo "== Server deploy (migrate + collectstatic + restart) =="
ssh root@45.138.159.120 '/usr/local/bin/yurit-deploy'
echo "== Smoke test =="
sleep 3
curl -s -o /dev/null -w "https://koreysbozor.uz/login/ -> %{http_code}\n" https://koreysbozor.uz/login/
