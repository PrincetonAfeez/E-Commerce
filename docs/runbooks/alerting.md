# Alerting and observability hooks
 
This document describes how to wire external monitoring to the platform ops
endpoints and logs. The app exports health/readiness probes, authenticated
metrics, and structured logs suitable for Sentry and uptime checks.

## Probes

| Endpoint | Purpose | Expected |
|----------|---------|----------|
| `GET /healthz/` | Liveness + DB/cache | `200 {"status":"ok"}` |
| `GET /readyz/` | Process readiness | `200 {"status":"ready"}` |
| `GET /internal/metrics/?secret=…` | Ops counters | `200` JSON payload |

Configure your load balancer to use `/healthz/` for liveness and `/readyz/` for
readiness (during deploys, stop sending traffic before migrations complete).

## Metrics payload

`GET /internal/metrics/` returns:

- `outbox_pending` — undelivered outbox events
- `outbox_failed` — permanently failed outbox events
- `stranded_payment_pending` — checkout attempts stuck in payment pending
- `payments_requires_refund` — confirmed payments needing compensation
- `checkout_attempts_by_status` — attempt state histogram

Set `OPS_METRICS_SECRET` in production and pass it as `X-Ops-Metrics-Secret`
or `?secret=` (prefer header).

### Suggested alert thresholds

| Signal | Warning | Critical |
|--------|---------|----------|
| `/healthz/` non-200 | 2 consecutive failures | 5 min down |
| `outbox_pending` | > 50 for 15 min | > 200 for 15 min |
| `outbox_failed` | > 0 (new) | > 10 |
| `webhook_deliveries_failed` | > 0 (new) | > 10 |
| `stranded_payment_pending` | > 0 for 30 min | > 5 for 60 min |
| `payments_requires_refund` | > 0 | > 0 for 30 min |
| Worker heartbeat file stale | > 5 min | > 15 min |
| Backup heartbeat stale | > 26 h | > 49 h |

## Sentry

Set `SENTRY_DSN` and optionally:

- `SENTRY_ENVIRONMENT=production`
- `SENTRY_RELEASE` or `GIT_SHA`
- `SENTRY_TRACES_SAMPLE_RATE=0.1` for latency traces

Create alert rules for:

- New issues on `shop.checkout` / `shop.payments` / `shop.psp_webhooks`
- Error rate spikes on checkout confirm path

## PSP webhooks

Inbound payment webhooks: `POST /api/v1/webhooks/payments/<provider>/`

- Header `X-Payment-Signature` — HMAC-SHA256 of body with `PAYMENT_WEBHOOK_SECRET`
- Optional `X-Tenant-ID` for tenant scoping

Alert on sustained `403` (signature failures) — possible misconfiguration or attack.

## Dead-letter queue (DLQ)

Terminal failures are stored as `OutboxEvent.status=failed` and
`WebhookDelivery.status=failed`. Metrics expose counts via `/internal/metrics/`.

**Inspect:**

```bash
python manage.py reprocess_dead_letters --dry-run
```

**Re-drive (after fixing root cause):**

```bash
python manage.py reprocess_dead_letters --outbox --webhooks --limit 200
python manage.py process_outbox
python manage.py deliver_webhooks
```

## Backup alerts

When `BACKUP_S3_BUCKET` is set, `backup_db` uploads after a successful dump and
exits non-zero on upload failure. Monitor:

- Daily successful backup (S3 object age < 26 h)
- `backup` container healthcheck (heartbeat file mtime)

## Docker healthchecks

- `web` — curls `/healthz/`
- `worker` — `/tmp/worker-heartbeat` updated only after a successful cycle
- `backup` — `/tmp/backup-heartbeat` updated only after successful `backup_db`

## Smoke test after deploy

```bash
curl -fsS https://<host>/healthz/
curl -fsS https://<host>/readyz/
curl -fsS -H "X-Ops-Metrics-Secret: $OPS_METRICS_SECRET" https://<host>/internal/metrics/
```

Run one API checkout smoke (cart → checkout → authorize → confirm) against staging
before promoting production traffic.
