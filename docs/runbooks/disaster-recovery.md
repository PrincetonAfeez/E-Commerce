# Runbook: Disaster Recovery (DR)

How the platform recovers from the loss of the database, the application tier, or a whole
region. Pairs with [backup-restore.md](backup-restore.md) (mechanics) and
[incident-response.md](incident-response.md) (comms/coordination).

## Objectives

| Metric | Target | Basis |
|--------|--------|-------|
| **RPO** (max data loss)      | ≤ 5 min  | Continuous WAL / PITR archiving |
| **RTO** (max time to restore)| ≤ 4 hr   | Provision + restore + verify    |

RPO is bounded by WAL archiving cadence; the daily `pg_dump` is the fallback when PITR is
unavailable (worst-case RPO then = time since last daily backup, ≤ 24 h).

## Scenarios & response

### 1. Database corruption / bad data event
- Prefer **point-in-time restore** to just before the event (meets the 5-min RPO).
- If PITR is unavailable, restore the most recent daily dump per
  [backup-restore.md](backup-restore.md).
- Never restore over the live DB — restore to a new instance, verify, then cut over.

### 2. Application tier down (all app hosts unhealthy)
- The app is stateless (sessions in DB/cache, media in object store). Redeploy the last
  known-good image (see rollback in [operations.md](operations.md)); scale replicas back up.
- `/healthz/` gates the load balancer; hosts rejoin automatically once healthy.

### 3. Region loss
- Provision the app tier in the standby region from the same container image.
- Restore Postgres from the latest cross-region snapshot / WAL; restore media from the
  cross-region bucket replica.
- Repoint DNS (storefront hostnames + platform domain) to the standby region.
- On-demand TLS re-issues per host via the `/internal/tls-check/` authorization endpoint.

## Cutover checklist

1. Restore DB (PITR or dump) into the recovery instance.
2. `migrate --check` + `check --deploy` pass.
3. App tier deployed and pointed at the recovered DB.
4. Media restored/attached.
5. `GET /healthz/` green across replicas.
6. Smoke test: storefront loads, checkout completes (simulated gateway), one order visible
   in `/staff/`.
7. DNS updated; TLS issuing for tenant hosts.
8. Announce recovery per [incident-response.md](incident-response.md).

## DR drills

- **Quarterly**: full region-failover rehearsal into a scratch environment.
- **Monthly**: restore-from-backup drill (doubles as backup verification).
- Record each drill's actual RTO/RPO and file gaps as follow-up work.
