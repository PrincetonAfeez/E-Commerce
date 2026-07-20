#!/usr/bin/env sh
set -e

# Migrations are opt-in so worker/backup replicas do not race on migrate.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  python manage.py migrate --noinput
fi

exec "$@"
