# Aster Commerce

A multi-tenant commerce SaaS built on Django 5.2 + HTMX, with a versioned DRF API.
One deployment hosts many independent storefronts (tenants), each resolved by hostname,
with per-tenant staff, catalog, orders, and billing.

> **Payments are simulated.** Both storefront checkout and platform billing run through an
> in-process `SimulatedPaymentGateway` — no real PSP, no real charges. This is intentional;
> the checkout/billing seams are structured so a real gateway could be dropped in later.

## Feature overview

- **Multi-tenancy** — shared-schema isolation via `TenantScopedModel` + a contextvar-scoped
  `TenantManager`; `TenantMiddleware` resolves the tenant by host. Per-tenant staff via
  `TenantMembership` (roles: owner / manager / staff).
- **Catalog** — products, variants, categories, collections, merchandising, search & facets.
- **Cart & checkout** — guest and authenticated carts, stock reservations, idempotent
  checkout attempts, coupon redemption, order finalization, refunds, returns.
- **Storefront ops** — a tenant-scoped `/staff/` console for orders, fulfilment, returns,
  inventory, team management, billing, and low-stock alerts.
- **Platform** — self-service store signup, simulated subscription billing, invoices.
- **API** — versioned DRF API under `/api/v1/`, documented with an OpenAPI schema and a
  self-hosted Swagger UI.
- **Privacy** — self-service data export and account deletion (order records retained but
  anonymized); one-click marketing unsubscribe.
- **Ops** — health probe, structured logging, optional Sentry, outbox-driven email,
  scheduled maintenance commands, and operational runbooks (see `docs/runbooks/`).

## Quick start (local)

```powershell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`. Local/dev uses SQLite; CI and production use Postgres.

Demo coupons: `LAUNCH15`, `SHIPFREE`.

## Scheduled / maintenance commands

```powershell
python manage.py expire_reservations       # release stock from abandoned checkouts
python manage.py reconcile_payments        # resolve stranded authorizations
python manage.py process_outbox            # send queued transactional email
python manage.py deliver_webhooks          # deliver pending webhooks
python manage.py recover_abandoned_carts   # queue cart-recovery emails (honors unsubscribe)
python manage.py run_subscription_billing  # simulated platform billing run
python manage.py cleanup_retention         # prune expired idempotency/carts/attempts
python manage.py backup_db                 # pg_dump logical backup (see runbook)
```

## Testing & checks

```powershell
python -m pytest
python -m ruff check shop/ config/
python manage.py makemigrations --check --dry-run
python manage.py check --deploy
```

## API

```text
/api/v1/catalog/products/
/api/v1/cart/
/api/v1/checkout/attempts/
/api/v1/orders/
/api/v1/staff/orders/          # tenant-scoped; requires TenantMembership
/api/v1/schema/                # OpenAPI schema
/api/v1/docs/                  # Swagger UI (when drf-spectacular is installed)
```

Side-effecting checkout / payment / refund endpoints require an `Idempotency-Key` header.
Staff API endpoints authorize against the caller's `TenantMembership` for the request's
tenant — global `is_staff` grants nothing cross-tenant.

## Architecture notes

The checkout seam is anchored on `CheckoutAttempt`. Stock is reserved at checkout start,
gateway calls happen **outside** database transactions, and confirmed-payment finalization
runs in one transaction that consumes reservations, creates the order, links payment,
redeems coupons, clears the cart, and writes audit + outbox records.

- Architecture decision records: `docs/adr/`
- Operational runbooks (backup/restore, DR, incident response, day-2 ops): `docs/runbooks/`
- Security posture, subprocessors, data retention: `SECURITY.md`
- Deployment (Docker, Gunicorn, WhiteNoise): `docs/E-Commerce.md`
