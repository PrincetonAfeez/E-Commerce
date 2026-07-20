#!/bin/sh
# Background worker loop with runbook-aligned cadence.
set -eu

HEARTBEAT=/tmp/worker-heartbeat
CYCLE=0
SHUTDOWN=0

finish_cycle() {
  SHUTDOWN=1
  echo "worker received shutdown signal; finishing current cycle" >&2
}

trap finish_cycle TERM INT

run_if_due() {
  name="$1"
  interval="$2"
  shift 2
  if [ "$SHUTDOWN" -eq 1 ]; then
    return 0
  fi
  if [ $((CYCLE % interval)) -eq 0 ]; then
    if "$@"; then
      echo "ok $name"
    else
      echo "failed $name" >&2
      return 1
    fi
  fi
  return 0
}

while [ "$SHUTDOWN" -eq 0 ]; do
  FAILED=0
  run_if_due process_outbox 1 python manage.py process_outbox || FAILED=1
  run_if_due deliver_webhooks 1 python manage.py deliver_webhooks || FAILED=1
  run_if_due reconcile_payments 60 python manage.py reconcile_payments || FAILED=1
  run_if_due expire_reservations 1 python manage.py expire_reservations || FAILED=1
  run_if_due recover_abandoned_carts 60 python manage.py recover_abandoned_carts || FAILED=1
  run_if_due run_billing 1440 python manage.py run_billing || FAILED=1
  run_if_due run_subscription_billing 1440 python manage.py run_subscription_billing || FAILED=1
  run_if_due cleanup_retention 1440 python manage.py cleanup_retention --days "${RETENTION_DAYS:-365}" || FAILED=1
  run_if_due reprocess_dead_letters 1440 python manage.py reprocess_dead_letters --dry-run || FAILED=1
  run_if_due cleanup_orphan_media 10080 python manage.py cleanup_orphan_media || FAILED=1

  if [ "$FAILED" -eq 0 ]; then
    date -Iseconds > "$HEARTBEAT"
  else
    echo "worker cycle $CYCLE had failures" >&2
  fi
  CYCLE=$((CYCLE + 1))
  if [ "$SHUTDOWN" -eq 1 ]; then
    break
  fi
  sleep 60 &
  wait $!
done

echo "worker shut down gracefully"
