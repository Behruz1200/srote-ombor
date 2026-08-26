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

# OPS-2: The deploy-time production reset was REMOVED. A destructive wipe must
# never live in a deploy path gated on a remembered env var — one re-run or
# rollback with the var still set would empty the business. `reset_for_production`
# is now a manual-only command (run it by hand, it prompts for confirmation).

# Auto-create superuser if DJANGO_SUPERUSER_* env vars are set.
# Idempotent: skips if the username already exists.
python manage.py ensure_superuser

# Optionally seed demo data on first deploy. Set SEED_DEMO_DATA=1 on
# the host to populate 3 branches, 5 sellers (sotuvchi123 password),
# 32 products, ~970 intakes, ~610 historical sales.
# Idempotent: each section of seed.py checks for existing rows.
if [ "$SEED_DEMO_DATA" = "1" ]; then
    echo "SEED_DEMO_DATA=1 → running seed.py"
    python seed.py
else
    echo "SEED_DEMO_DATA not set → skipping demo data"
fi
