# Runbook: Day-2 Operations

Monitoring, SLOs, on-call, staging, deploys/rollback, and routine maintenance.

## Service Level Objectives (SLOs)

| SLO | Target | Measured by |
|-----|--------|-------------|
| Storefront availability      | 99.9% monthly       | Uptime probe on `/healthz/` |
| API availability             | 99.9% monthly       | Uptime probe on `/healthz/` (or tenant-scoped catalog route) |
| Storefront p95 latency       | < 500 ms            | Reverse-proxy / APM metrics |
| Checkout success rate        | > 99% of attempts   | `CheckoutAttempt` outcomes  |
| Outbox email delivery lag    | < 15 min p95        | `EmailDelivery` queued→sent |

An 0.1% monthly error budget backs the 99.9% targets; burning it pauses risky changes.

## Monitoring & alerting

- **Health probe**: `GET /healthz/` returns 200 when DB and cache are reachable. In
  production, a cache failure returns **503** so load balancers stop routing traffic.
  Wire an external uptime monitor (e.g. UptimeRobot / Pingdom / provider healthcheck)
  to page on failure. `/healthz/` is exempt from HTTPS redirect so plain-HTTP probes work.
- **Readiness**: `GET /readyz/` returns 200 when the process is ready to serve (use for
  deploy rollouts; keep on the readiness probe path, not liveness).
- **Ops metrics**: `GET /internal/metrics/` (authenticated via `OPS_METRICS_SECRET`) exposes
  outbox backlog, stranded payments, and checkout attempt counts. See `alerting.md` for
  threshold recommendations.
- **Errors**: set `SENTRY_DSN` to ship exceptions to Sentry (PII scrubbing on;
  `send_default_pii=False`). Alert on new-issue and error-rate spikes.
- **Logs**: structured logging to stdout (plain-text `asctime level name message` by default),
  collected by the platform log pipeline.
- **Alert on**: `/healthz/` failing, 5xx rate, p95 latency breach, outbox backlog growth,
  DB connections/disk nearing limits, cert issuance failures.

### Slow-query logging

Enable Postgres slow-query logging and review weekly:

```sql
-- log any statement slower than 500ms
ALTER SYSTEM SET log_min_duration_statement = 500;
SELECT pg_reload_conf();
```

On managed Postgres, set the equivalent parameter (`log_min_duration_statement = 500`) in
the parameter group and enable the slow-query / Performance Insights view. Feed findings
into indexing work.

### Cache invalidation

Redis is used for **ephemeral** keys only (rate limits, distributed locks, health probes).
Keys carry short TTLs and require no application-level invalidation. Catalog and order
data are read from Postgres directly — there is no shared application-data cache layer.

### Load / capacity testing

CI runs `scripts/load_smoke.py` against a local runserver (concurrent `/healthz/`,
`/readyz/`, and catalog API reads). For deeper capacity work, re-run with higher
request/worker counts against staging before major releases:

```bash
python scripts/load_smoke.py https://staging.example.com 200 20
```

## On-call

- **Rotation**: weekly primary + secondary; handoff at the start of each week.
- **Escalation**: primary → secondary (15 min no-ack) → engineering lead.
- **Paging sources**: uptime monitor, Sentry alerts, error-rate/latency alerts.
- On-call responsibilities and severities are in
  [incident-response.md](incident-response.md) and [alerting.md](alerting.md).

## Environments

- **Local/dev**: SQLite, `DEBUG` on by default (`DJANGO_DEBUG=1`), simulated gateway.
- **Staging**: Postgres + Redis via `docker compose -f docker-compose.yml -f docker-compose.staging.yml --profile staging up -d`. Copy `.env.staging.example` to `.env.staging`. Production-like security (`DJANGO_ENV=production`), seeded demo tenants. Run `verify-restore` profile service or `python manage.py verify_backup_restore` after deploy.
- **Production**: Postgres, HTTPS-only (HSTS, secure cookies), CSP, per-host TLS.

## Deploys & rollback

Deploys ship an immutable container image (see `docs/E-Commerce.md`). Steps:

1. CI green (ruff, migrate, `verify_backup_restore`, pytest on Postgres + Redis, load smoke,
   `check --deploy`, OpenAPI validation, `docker build`).
2. Deploy to **staging**; run migrations; smoke test.
3. Promote the **same image** to production; run `migrate`; watch `/healthz/` + error rate.

**Rollback:**

- **Code-only change** → redeploy the previous known-good image tag. The app is stateless,
  so this is immediate.
- **Change that included a migration** → prefer roll-forward with a fix. Only reverse a
  migration if it is known-reversible and safe; otherwise restore per
  [backup-restore.md](backup-restore.md). Write migrations to be backward-compatible
  (additive) so the previous image keeps running during a rollout.
- After rollback, confirm `/healthz/` green + run the checkout smoke test, then open an
  incident review if customer impact occurred.

## Routine maintenance (scheduler / cron)

| Command | Cadence | Purpose |
|---------|---------|---------|
| `process_outbox`            | every 1–5 min | send queued transactional email |
| `deliver_webhooks`          | every 1–5 min | deliver pending webhooks |
| `expire_reservations`       | every few min | release stock from abandoned checkouts |
| `reconcile_payments`        | hourly        | resolve stranded authorizations |
| `recover_abandoned_carts`   | hourly        | queue cart-recovery emails (skips unsubscribed) |
| `run_billing`                 | daily         | simulated tenant subscription billing |
| `run_subscription_billing`  | daily         | simulated platform billing run |
| `cleanup_retention`         | daily         | prune expired idempotency records / stale carts |
| `backup_db`                 | daily         | logical DB backup (see backup runbook) |

Each command is tenant-aware where relevant and safe to re-run (idempotent).

**Distributed locking:** background commands that must not overlap across worker replicas
(`process_outbox`, `deliver_webhooks`, `reconcile_payments`, `expire_reservations`,
`recover_abandoned_carts`, `run_billing`, `run_subscription_billing`, `cleanup_retention`,
`backup_db`) acquire a cache-backed `single_instance` lock. A second replica skips the run
when the lock is held. Ensure `CACHE_URL` / `REDIS_URL` is configured in production so
locks and rate limits are shared across Gunicorn workers and scheduler pods.
