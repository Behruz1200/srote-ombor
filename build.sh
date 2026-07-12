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

# One-shot production reset. Set RESET_ON_DEPLOY=1 in the host env,
# push (or trigger a manual deploy), then REMOVE the env var so it
# never runs again. Preserves Users; optionally also wipes Branches
# and Categories.
if [ "$RESET_ON_DEPLOY" = "1" ]; then
    ARGS="--yes"
    if [ "$RESET_WIPE_BRANCHES" = "1" ]; then
        ARGS="$ARGS --wipe-branches"
    fi
    if [ "$RESET_WIPE_CATEGORIES" = "1" ]; then
        ARGS="$ARGS --wipe-categories"
    fi
    echo "RESET_ON_DEPLOY=1 → running reset_for_production $ARGS"
    python manage.py reset_for_production $ARGS
    echo "⚠  Remember to REMOVE RESET_ON_DEPLOY env var before the next deploy."
else
    echo "RESET_ON_DEPLOY not set → skipping production reset"
fi

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
