#!/usr/bin/env bash
# Build script run by Render on every deploy.
# Stops on first error.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# Collect static files (WhiteNoise serves them)
python manage.py collectstatic --no-input

# Apply migrations
python manage.py migrate --no-input

# Auto-create superuser if DJANGO_SUPERUSER_* env vars are set.
# Idempotent: skips if the username already exists.
python manage.py ensure_superuser
