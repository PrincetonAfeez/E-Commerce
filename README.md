# Aster Commerce

A multi-tenant commerce SaaS built on Django 5.2 + HTMX, with a versioned DRF API.
One deployment hosts many independent storefronts (tenants), each resolved by hostname,
with per-tenant staff, catalog, orders, and billing.

> **Payments are simulated.** Both storefront checkout and platform billing run through a
> pluggable gateway layer (`PAYMENT_GATEWAY=simulated` by default). No real PSP, no real
> charges. The checkout/billing seams are structured so a real provider can be dropped in
> later without rewriting order finalization.

## Feature overview

- **Multi-tenancy** — shared-schema isolation via `TenantScopedModel` + a contextvar-scoped
  `TenantManager`; `TenantMiddleware` resolves the tenant by host. Per-tenant staff via
  `TenantMembership` (roles: owner / manager / staff).
- **Catalog** — products, variants, categories, collections, merchandising, search & facets.
- **Cart & checkout** — guest and authenticated carts, stock reservations, idempotent
  checkout attempts, coupon redemption, order finalization, refunds, returns.
- **Payments (simulated PSP)** — gateway factory (`shop/services/gateway/`), authorize-only
  API, inbound webhook processing, stranded-payment reconciliation.
- **Storefront ops** — tenant-scoped `/staff/` console for orders, fulfilment, returns,
  inventory, team management, billing, and low-stock alerts.
- **Platform** — self-service store signup, simulated subscription billing, invoices.
- **API** — versioned DRF API under `/api/v1/`, documented with OpenAPI + self-hosted Swagger UI.
- **Privacy** — self-service data export and account deletion (orders retained but anonymized);
  one-click marketing unsubscribe.
- **Ops** — `/healthz/`, `/readyz/`, authenticated `/internal/metrics/`, structured logging,
  optional Sentry, outbox-driven email, worker cadence, and runbooks in `docs/runbooks/`.

## Requirements

- **Python 3.12** (matches `Dockerfile` and CI; 3.11+ may work locally)
- **PostgreSQL 16** + **Redis 7** for CI, Docker, and production
- **SQLite** for quick local dev (default when `DATABASE_URL` is unset)

## Quick start (local)

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`. Copy `.env.example` to `.env` when you need Postgres, Redis,
or production-like settings.

Demo coupons after `seed_demo`: `LAUNCH15`, `SHIPFREE`.

### Docker (Postgres + Redis)

```powershell
docker compose --profile dev up -d db redis
# set DATABASE_URL and CACHE_URL in .env (see .env.example)
python manage.py migrate
python manage.py runserver
```

Production-style stack:

```powershell
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod up -d
```

## Scheduled / maintenance commands

```powershell
python manage.py expire_reservations       # release stock from abandoned checkouts
python manage.py reconcile_payments        # resolve stranded authorizations
python manage.py process_outbox            # send queued transactional email
python manage.py deliver_webhooks          # enqueue + deliver webhook callbacks
python manage.py recover_abandoned_carts   # queue cart-recovery emails (honors unsubscribe)
python manage.py run_billing               # platform subscription billing
python manage.py run_subscription_billing  # customer product subscription renewals
python manage.py cleanup_retention         # prune expired idempotency/carts/attempts
python manage.py backup_db                 # pg_dump logical backup (Postgres only)
python manage.py verify_backup_restore     # pg_dump → pg_restore round-trip verification
python manage.py reprocess_dead_letters    # re-queue failed outbox/webhook deliveries
python manage.py cleanup_orphan_media      # remove unreferenced uploaded media files
```

Capacity smoke (GROWTH evidence):

```powershell
python manage.py runserver 127.0.0.1:8000
python scripts/load_smoke.py http://127.0.0.1:8000
```

Staging environment:

```powershell
copy .env.staging.example .env.staging
docker compose -f docker-compose.yml -f docker-compose.staging.yml --profile staging up -d
```

Production worker cadence is documented in `docs/runbooks/operations.md` and implemented
in `scripts/worker-loop.sh`.

## Testing & checks

The suite has **296 tests** and **~89% line coverage** across `shop/` and `config/`.

```powershell
python -m pip install -r requirements-dev.txt

# Full suite (SQLite by default)
python -m pytest shop/tests/

# With coverage (matches CI threshold)
python -m pytest shop/tests/ --cov=shop --cov=config --cov-report=term-missing --cov-fail-under=85

# Lint
python -m ruff check shop/ config/
python -m ruff format --check shop/ config/

# Migrations & deployment posture
python manage.py makemigrations --check --dry-run
python manage.py check --deploy
python manage.py spectacular --file schema.yml --validate
```

CI (`.github/workflows/ci.yml`) runs the same checks on **Python 3.12** with Postgres and
Redis service containers.

## API

```text
GET  /api/v1/catalog/products/
GET  /api/v1/catalog/categories/
GET  /api/v1/catalog/collections/
GET  /api/v1/cart/
POST /api/v1/cart/items/
POST /api/v1/cart/apply-coupon/
POST /api/v1/checkout/attempts/
POST /api/v1/checkout/attempts/<id>/authorize-payment/
POST /api/v1/checkout/attempts/<id>/confirm-payment/
POST /api/v1/webhooks/payments/<provider>/
GET  /api/v1/orders/                         # authenticated
GET  /api/v1/orders/<order_number>/
POST /api/v1/guest/orders/lookup/
GET  /api/v1/guest/orders/<order_token>/
GET  /api/v1/staff/orders/                   # tenant-scoped; requires TenantMembership
POST /api/v1/staff/orders/<id>/transition/
POST /api/v1/staff/orders/<id>/refund/       # manager+
POST /api/v1/staff/orders/<id>/cancel/        # manager+
POST /api/v1/staff/inventory/adjustments/    # manager+
POST /api/v1/staff/checkout-attempts/<id>/replay-finalization/
GET  /api/v1/schema/                         # OpenAPI schema
GET  /api/v1/docs/                           # Swagger UI
```

Side-effecting checkout, payment, refund, and inventory endpoints require an
`Idempotency-Key` header. Staff API authorization is scoped to the caller's
`TenantMembership` for the request's tenant — global `is_staff` grants nothing cross-tenant.

## Production environment variables

| Variable | Required in prod | Purpose |
|----------|------------------|---------|
| `DJANGO_SECRET_KEY` | yes | Django secret |
| `DJANGO_ALLOWED_HOSTS` | yes | Host allow-list |
| `DJANGO_SITE_URL` | yes | HTTPS base URL for email links |
| `DATABASE_URL` | yes | PostgreSQL connection |
| `CACHE_URL` | yes | Redis (rate limits, locks) |
| `DJANGO_EMAIL_HOST` | yes | SMTP for transactional mail |
| `TLS_CHECK_SECRET` | yes | On-demand TLS authorization (`/internal/tls-check/`) |
| `OPS_METRICS_SECRET` | yes | Protects `/internal/metrics/` |
| `PAYMENT_WEBHOOK_SECRET` | yes* | HMAC for inbound PSP webhooks |
| `PAYMENT_GATEWAY` | no | Gateway adapter (`simulated` default) |
| `BACKUP_S3_BUCKET` | no | Optional off-host DB backup upload |

\* Required when processing payment webhooks. See `.env.example` for the full list.

## Architecture notes

The checkout seam is anchored on `CheckoutAttempt`. Stock is reserved at checkout start,
gateway calls happen **outside** database transactions, and confirmed-payment finalization
runs in one transaction that consumes reservations, creates the order, links payment,
redeems coupons, clears the cart, and writes audit + outbox records.

- Architecture decision records: `docs/adr/`
- Operational runbooks (backup/restore, DR, incident response, day-2 ops, alerting): `docs/runbooks/`
- Security posture, subprocessors, data retention: `SECURITY.md`
- Deployment (Docker, Gunicorn, WhiteNoise): `docs/E-Commerce.md`
