# Runbook: Backup & Restore

Covers logical backups of the production Postgres database and how to restore them.
Storefront/billing payments are simulated, so there is no external PSP ledger to reconcile —
the database is the system of record.

## What is backed up

- The Postgres database (all tenants, shared-schema). This holds catalog, carts, orders,
  payments, subscriptions, invoices, audit log, and outbox.
- Uploaded media (product images, logos) — backed up by the object store / volume snapshot
  policy of the hosting environment, separately from the DB.

## Backup schedule (target)

| Layer            | Method                          | Frequency | Retention |
|------------------|---------------------------------|-----------|-----------|
| Postgres (full)  | `manage.py backup_db` / pg_dump | Daily     | 30 days   |
| Postgres (PITR)  | Managed provider WAL archiving  | Continuous| 7 days    |
| Media            | Volume / bucket snapshot        | Daily     | 30 days   |

RPO/RTO targets are in [disaster-recovery.md](disaster-recovery.md).

## Taking a backup

```bash
# Uses DATABASE_URL (or DATABASES['default']) for connection + credentials.
# Credentials go through libpq env (PGPASSWORD), never the command line.
python manage.py backup_db --out-dir /var/backups/aster
# -> /var/backups/aster/<dbname>-<UTC-timestamp>.dump  (pg_dump custom format)
```

Ship the resulting `.dump` to off-host, versioned, encrypted storage (e.g. an object
bucket with SSE + lifecycle rules). Do not keep the only copy on the app host.

Managed Postgres (RDS/Cloud SQL/etc.): also enable automated snapshots + PITR at the
provider level. `backup_db` is the portable, provider-independent second line.

## Restoring

1. **Provision** a clean, empty Postgres database (do not restore over a live one).
2. **Restore** the dump:
   ```bash
   pg_restore --no-owner --no-privileges --clean --if-exists \
     --dbname "$TARGET_DATABASE_URL" <dbname>-<timestamp>.dump
   ```
3. **Point the app** at the restored DB (`DATABASE_URL`) and run:
   ```bash
   python manage.py migrate --check   # confirm schema matches the code
   python manage.py check --deploy
   ```
4. **Restore media** from the corresponding snapshot for the same timestamp.
5. **Smoke test**: `GET /healthz/`, load a storefront, open one order in `/staff/`.

## Verifying backups (do not skip)

A backup is only real once a restore has succeeded. Monthly: restore the latest dump into a
throwaway database, run `migrate --check` and the smoke test above, record the result. See
the restore-drill note in [disaster-recovery.md](disaster-recovery.md).
