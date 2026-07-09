# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the platform administrator rather than
opening a public issue. Include steps to reproduce and impact. We aim to acknowledge within
2 business days and to remediate valid, high-severity issues promptly. Please do not run
disruptive tests (DoS, mass scanning) against production or against other tenants.

## Payments

Storefront checkout and platform billing use an in-process **simulated payment gateway**.
No real card data is collected, stored, or transmitted, and there is no PSP integration.
As a result the platform is **out of scope for PCI DSS** — there is no cardholder data
environment. The checkout and billing code is structured behind a gateway seam so a real,
PCI-compliant provider could be integrated later without reworking order finalization.

## Tenant isolation

- Shared-schema multi-tenancy: every tenant-scoped model carries a `tenant` FK and is
  filtered by a contextvar-scoped manager; `TenantMiddleware` binds the tenant per request
  by hostname.
- Staff authorization is **per-tenant** via `TenantMembership` (roles: owner / manager /
  staff), enforced on both the web `/staff/` console and the DRF `/api/v1/staff/` endpoints.
  A global Django `is_staff` flag grants **no** cross-tenant access.
- The Django admin is the platform-operator console and is restricted to superusers; store
  staff never use `/admin/`.

## Authentication & access control

- Password auth with Django's validators; login, registration, and password-reset endpoints
  are rate-limited. The DRF API is throttled (anon + user scopes) and returns HTTP 429 when
  limits are exceeded.
- Email verification is required and surfaced via a banner until completed.
- **Multi-factor authentication (MFA):** not currently implemented. This is a known,
  accepted gap for this deployment; it is the top candidate for the auth roadmap. Operators
  needing MFA today should front the app with an SSO/IdP that enforces it, or restrict
  operator access to a VPN.

## Transport & application hardening

- Production is HTTPS-only: HSTS (with preload + subdomains), secure + HTTP-only session and
  CSRF cookies, `SECURE_SSL_REDIRECT`, and `X-Forwarded-Proto` awareness behind the proxy.
- Content Security Policy restricts scripts/styles to self-hosted assets (no inline JS).
  `X-Frame-Options: DENY`, `nosniff`, and a locked-down CORS allow-list are set.
- Per-host TLS is issued on demand, authorized by `/internal/tls-check/` so certificates are
  only minted for hostnames belonging to active tenants.

## Data protection & privacy

- **Data export**: customers can export their personal data (JSON) from privacy settings.
- **Account deletion**: customers can delete their account; order records are **retained but
  anonymized** (PII stripped from orders, reviews anonymized, subscriptions cancelled,
  addresses/wishlist/store-credit removed) to satisfy tax/accounting retention while honoring
  erasure of personal data.
- **Marketing opt-out**: every marketing email (e.g. cart recovery) carries a one-click,
  signed unsubscribe link; suppressed addresses are skipped on future sends.
- **Audit log**: `AuditLog` is append-only — it is read-only in the admin (no add/change/
  delete) to preserve integrity of the security trail.

## Data retention

| Data | Retention |
|------|-----------|
| Order & invoice records | Retained for legal/accounting needs; PII anonymized on account deletion |
| Idempotency records     | Pruned after their TTL (`cleanup_retention`) |
| Stale carts / abandoned checkout attempts | Pruned after ~30 days (`cleanup_retention`) |
| Audit log               | Retained (append-only) for security |
| Backups                 | 30 days (see `docs/runbooks/backup-restore.md`) |

## Data residency

Customer data resides in the region(s) of the configured Postgres database and object
store. Choose the hosting region to meet applicable residency requirements; cross-region
copies exist only as encrypted backups/replicas for disaster recovery.

## Subprocessors

The platform itself uses a minimal set of infrastructure subprocessors; keep this list
current for customer due-diligence:

| Subprocessor | Purpose |
|--------------|---------|
| Cloud hosting provider | Application compute + managed Postgres |
| Object storage provider | Uploaded media (product images, logos) |
| Email delivery provider | Transactional + marketing email (via SMTP/outbox) |
| Sentry (optional)      | Error monitoring (PII scrubbing enabled) |

There is **no payment subprocessor** — payments are simulated in-process.

## Penetration testing

- Automated: CI runs linting, dependency updates via Dependabot (pip, GitHub Actions,
  Docker), and the test suite (including tenant-isolation and authorization tests) on every
  change.
- Manual: perform a security review / penetration test before major releases and at least
  annually, covering tenant isolation, authz, authentication, and the API surface. Track
  findings to remediation and record the date and scope of the most recent test here.
- Most recent test: _not yet performed on this deployment — schedule before GA._
