#!/usr/bin/env sh
set -e

# Apply database migrations before serving traffic. Safe to run on every boot.
python manage.py migrate --noinput

exec "$@"
