# Runbook: Day-2 Operations

Monitoring, SLOs, on-call, staging, deploys/rollback, and routine maintenance.

## Service Level Objectives (SLOs)

| SLO | Target | Measured by |
|-----|--------|-------------|
| Storefront availability      | 99.9% monthly       | Uptime probe on `/healthz/` |
| API availability             | 99.9% monthly       | Uptime probe on `/api/v1/schema/` (or `/healthz/`) |
| Storefront p95 latency       | < 500 ms            | Reverse-proxy / APM metrics |
| Checkout success rate        | > 99% of attempts   | `CheckoutAttempt` outcomes  |
| Outbox email delivery lag    | < 15 min p95        | `EmailDelivery` queued→sent |

An 0.1% monthly error budget backs the 99.9% targets; burning it pauses risky changes.

## Monitoring & alerting

- **Health probe**: `GET /healthz/` returns 200 + checks DB connectivity. Wire an external
  uptime monitor (e.g. UptimeRobot / Pingdom / provider healthcheck) to page on failure.
  `/healthz/` is exempt from HTTPS redirect so plain-HTTP probes work.
- **Errors**: set `SENTRY_DSN` to ship exceptions to Sentry (PII scrubbing on;
  `send_default_pii=False`). Alert on new-issue and error-rate spikes.
- **Logs**: structured logging to stdout (JSON-ish `asctime level name message`), collected
  by the platform log pipeline.
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

## On-call

- **Rotation**: weekly primary + secondary; handoff at the start of each week.
- **Escalation**: primary → secondary (15 min no-ack) → engineering lead.
- **Paging sources**: uptime monitor, Sentry alerts, error-rate/latency alerts.
- On-call responsibilities and severities are in
  [incident-response.md](incident-response.md).

## Environments

- **Local/dev**: SQLite, `DEBUG` off by default, simulated gateway.
- **Staging**: Postgres, production-like config, `IS_PRODUCTION` security flags on, seeded
  demo tenants. All changes deploy to staging and pass smoke tests before production.
- **Production**: Postgres, HTTPS-only (HSTS, secure cookies), CSP, per-host TLS.

## Deploys & rollback

Deploys ship an immutable container image (see `docs/E-Commerce.md`). Steps:

1. CI green (ruff, `makemigrations --check`, pytest on Postgres, `check --deploy`, OpenAPI
   validation).
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
| `run_subscription_billing`  | daily         | simulated platform billing run |
| `cleanup_retention`         | daily         | prune expired idempotency records / stale carts |
| `backup_db`                 | daily         | logical DB backup (see backup runbook) |

Each command is tenant-aware where relevant and safe to re-run (idempotent).
