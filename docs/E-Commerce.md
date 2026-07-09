# Project 81 — E-Commerce Capstone: Enterprise Django + HTMX Commerce System (v3)

**Type:** Standalone build. No dependencies on, and no reuse by, any other project. Scoped as if it is the only thing being built — even though, as the phase capstone, it is conceptually the sum of the phase. Built from scratch.
**One-line:** A production-ready, GTM-grade Django storefront and lightweight merchant operations system integrating authentication, catalog merchandising, cart, promotions, tax/shipping calculators, simulated Stripe-shaped payments, reservation-based inventory, orders, fulfillment, notifications, and a first-class API — fronted by HTMX and built entirely in Python.

**The crux — integration, not any single piece.** Every prior project had one crux. This one's crux is the **seams between subsystems**, concentrated in the **cart → payment → inventory → order transition.** That transition must survivably reserve stock, handle authoritative payment confirmation, decrement inventory, and create the order — with database finalization atomic and external gateway calls safely outside database transactions. It is the capstone's exam.

**The three invariants the whole system protects:**
1. **Never oversell** — `reserved + sold ≤ on-hand stock`, always, even under simultaneous checkouts of the last item — and the reservation-expiry sweep never races a payment in flight to release stock out from under a confirmed purchase.
2. **Never double-charge / double-order / over-redeem** — idempotency survives double-clicks, confirmation replays, and a crash between "payment succeeded" and "order created"; and coupon usage limits are database-enforced contended counters, not application checks, so a limited coupon cannot over-redeem under concurrency.
3. **Payment ↔ order consistency** — no permanent paid-but-no-order, no order-but-unpaid, with replay/reconciliation closing any recoverable intermediate state exactly once — including payments stranded by a lost confirmation, resolved by gateway-authoritative polling rather than waiting on a webhook forever.

**Register:** Mastery of Python + system architecture. Enterprise-grade, production-ready, GTM-grade.
**Stack constraint:** Entire app in the Python ecosystem. Web layer is Django + HTMX. First-class API (DB + API + Web). No Channels/WebSockets.

> **What is new in v2.** This revision keeps the original capstone crux — the cart → payment → inventory → order seam — and expands the scope into a fuller production-grade commerce product. New additions include explicit Python/Django service-layer architecture, ADRs, domain events and outbox, a formal checkout state machine, richer payment lifecycle modeling, staff order operations, customer notifications, tax/shipping adapter boundaries, promotions, catalog merchandising, API endpoint contracts, HTMX requirements, security verification, privacy/PII handling, observability, support tooling, performance targets, and expanded testing.

> **What is new in v3 (second-order correctness pass).** Scope is *not* reduced. v2 already handles every first-order saga concern correctly (the seam, reservations, gateway-outside-transaction, authoritative confirmation, idempotent replay, snapshots, DB constraints, roll-forward reconciliation). v3 closes the second-order holes that survive that discipline:
> - **Coupon over-redemption under concurrency (§23):** promotion usage limits become database-enforced contended counters (atomic conditional update + unique `PromotionRedemption` constraints), the same rigor already applied to inventory — not an application-level validation. (ADR-0013)
> - **Reservation-expiry vs payment-in-flight race (§3, §18):** the sweep never releases reservations for attempts in `payment_pending` or beyond; finalization re-asserts stock under lock with a defined compensating path (auto-refund/backorder) for the unrecoverable case. (ADR-0014)
> - **Gateway-authoritative reconciliation (§6, §19):** a `get_payment_status` gateway method plus a reconciliation job drive payments stranded by a lost confirmation to a terminal state — reconciliation is bidirectional, not webhook-dependent. (ADR-0015)
> - **Refund as a compensating transaction (§19a):** partial-refund allocation across lines (discount/tax/shipping apportioned), compensating `InventoryMovement` on restock, and the coupon-redemption-release decision. (ADR-0016)
> - **Concurrent idempotency semantics (§15.4):** the in-flight same-key case (not just sequential replay) is specified. Plus explicit cart→checkout price-drift and guest-merge over-stock decisions.


---

## 1. Locked Decisions

| # | Area | Decision |
|---|------|----------|
| 1 | Payments | **Simulated**, behind a **swappable gateway interface**, production-shaped (authoritative confirmation + idempotency) so Stripe is a drop-in later |
| 2 | Inventory | **Stock count + checkout reservation with timeout** (reserve at checkout start; release on failure/abandon) |
| 3 | Products | **Variants** (size/color → each variant its own SKU + stock) |
| 4 | Checkout identity | **Guest checkout + optional accounts** (anonymous cart → merge into account on login) |
| 5 | API | **Catalog + cart/order API** (first-class, headless-capable) |
| 6 | Admin | **Django admin** for catalog/stock; **custom views** for order fulfillment |
| 7 | Money | **`Decimal`**, server-side totals, **price snapshotted** onto order items at purchase |
| 8 | Shipping/Tax | **Pluggable calculators** with simple launch implementations: flat/free-threshold shipping and configured-rate tax snapshots; not a full compliance engine |
| 9 | Order lifecycle | User-facing lifecycle is `pending → paid → processing → shipped → delivered`, plus `cancelled` / `refunded`; internally split payment, order, and fulfillment state; server-enforced, audited |
| 10 | Checkout seam | **`CheckoutAttempt` is first-class**: persisted, idempotent, replayable, and the anchor for reservations, payment confirmation, order creation, and crash recovery |
| 11 | Database guarantees | Core invariants are backed by **database constraints, unique keys, row locks, and indexes**, not just application checks |
| 12 | Domain architecture | Business mutations live in application/domain services; views, DRF serializers, admin actions, and jobs never bypass the service layer |
| 13 | Events/outbox | Domain events and an outbox table handle post-commit side effects such as emails, fulfillment hooks, analytics, and support alerts |
| 14 | Promotions | Coupon/discount support is in scope with server-side validation and order snapshots |
| 15 | Catalog merchandising | Categories, collections, slugs, images, active/draft/archive status, search, filters, and stock visibility rules are in scope |
| 16 | API contract | Versioned API with OpenAPI schema, documented idempotency behavior, stable JSON errors, and parity with web flows |
| 17 | Coupon concurrency | Promotion usage limits are **database-enforced contended counters** (atomic conditional update / row lock + unique `PromotionRedemption` constraints), redeemed inside finalization — never an application-level read-then-check |
| 18 | Reservation vs payment race | The expiry sweep **never releases reservations for attempts in `payment_pending` or beyond**; expiry is frozen/extended when payment begins; finalization re-asserts stock under lock with a defined compensating path if stock is genuinely unavailable |
| 19 | Reconciliation direction | Reconciliation is **bidirectional and gateway-authoritative**: a `get_payment_status` gateway method plus a poll job resolve payments stranded by a lost confirmation, in addition to roll-forward on paid-but-no-order |
| 20 | Refund allocation | Partial refunds **apportion order-level discount, tax, and shipping across lines**; restock posts a compensating `InventoryMovement`; the coupon-redemption-release-on-refund behavior is an explicit, tested decision |
| 21 | Idempotency semantics | Concurrent same-key requests are defined: the loser **waits for and returns the winner's stored result** (or a clean in-progress 409); the record stores the response with a documented TTL |
| 22 | Single-store boundary | This is a single-store commerce product, not a multi-merchant platform; global SKU uniqueness and deferred multi-catalog are deliberate. "SaaS/GTM" means sellable and production-grade, not multi-tenant |

---

## 2. Actors & Roles

| Role | Can do |
|------|--------|
| **Guest** | Browse catalog; build a session cart; check out and order without an account |
| **Customer** (account) | Everything a guest can, plus order history, saved details, and **guest-cart merge on login** |
| **Staff/Admin** | Manage catalog & stock (Django admin); fulfill orders / drive the status workflow (custom views) |

Authorization server-side everywhere. Order/cart data is owner-scoped (a customer sees only their orders; a guest's order is bound to their session/order token).

---

## 3. The Transactional Seam (the crux — build the project around this)

**The flow:**
1. **Cart** holds line items (variant + qty).
2. **Checkout begins** → create or reuse a persisted `CheckoutAttempt` by idempotency key, snapshot the cart lines/totals server-side, validate availability, and **reserve** stock for each line under a row lock, creating Reservation records with an `expires_at`. Reservation is what guarantees the items can't be sold out from under the buyer mid-checkout.
3. **Payment** → invoke the gateway (simulated) **outside any open database transaction**. **Payment confirmation is the authoritative trigger** — the client's "it worked" is never trusted; the confirmation event drives finalization (mirrors the real webhook pattern).
4. **On confirmed payment** → in a single database transaction: lock the `CheckoutAttempt`, record/link the confirmed Payment, convert reservations to sold (decrement on-hand), create the Order with **snapshotted prices**, clear the cart, and mark the attempt finalized.
5. **On declined/failed payment** → release reservations; cart intact; surface the error.
6. **On abandonment** (no completion within the window) → reservations expire via the scheduled sweep; stock returns to available.

**The replay anchor:**
- **CheckoutAttempt** — FK Cart, nullable FK User, guest token/session key, idempotency key, status (`started` / `reserved` / `payment_pending` / `payment_confirmed` / `finalized` / `failed` / `expired`), reserved total, shipping snapshot, nullable FK Order, gateway reference, created/updated timestamps.
- The attempt owns the reservation set and is the durable state machine for checkout. Every payment confirmation, retry, browser refresh, webhook replay, and crash-recovery pass resolves through the attempt.
- A confirmed payment with no order is not treated as impossible; it is treated as a **recoverable intermediate state**. Replay/reconciliation must create or link the order exactly once.

**The failure modes it must survive (the exam):**
- **Concurrent checkout of the last item** → reservation under `select_for_update` (or atomic conditional decrement); the second checkout finds nothing to reserve → clean "out of stock." Never oversells.
- **Payment succeeds but stock vanished** → cannot happen, *because* stock was reserved at checkout start. (This is the reason reservations exist.)
- **Stock reserved but payment fails** → reservation released, stock returns.
- **Double-click / double-submit checkout** → an **idempotency key** on the checkout/payment ensures the same attempt cannot create two orders or charge twice.
- **Crash between payment-confirmed and order-created** → confirmation is **replayable/idempotent** through `CheckoutAttempt`: on replay, if the order exists, no-op; if payment is confirmed but no order exists, create it. No permanent paid-but-no-order, no double order.
- **Reservation expiry races a payment in flight** → the sweep **must not release reservations for an attempt that has reached `payment_pending` or beyond.** Once payment is initiated the reservation is protected (expiry frozen/extended) until the payment resolves to a terminal state, so the sweep cannot return stock to the pool while a confirmation is in flight and hand a paid buyer's unit to someone else. Finalization additionally re-asserts stock under lock; if stock is genuinely unavailable (a bug or an out-of-band adjustment), a **defined compensating path** runs (auto-refund, or backorder) rather than leaving a paid order unfulfillable.
- **Confirmation event is lost (gateway timeout / dropped webhook)** → the attempt does not sit in `payment_pending` forever. A **gateway-authoritative reconciliation job** polls `get_payment_status` for stranded attempts and drives them to confirmed (→ finalize) or failed (→ release). Reconciliation is bidirectional, not webhook-dependent.
- **Concurrent redemption of a limited coupon** → coupon usage is a **database-enforced contended counter** (atomic conditional update / row lock + unique `PromotionRedemption`), so two simultaneous last-use redemptions cannot both succeed — the same protection stock already has.

These behaviors are **tested** (concurrency + idempotency + crash-replay + expiry-vs-payment race + lost-confirmation reconciliation), not assumed.

---

## 4. Inventory Model (variants + reservations)

- **Product** — name, description, category.
- **ProductVariant** — FK Product, SKU, attributes (e.g. size/color), price (`Decimal`), **on-hand `quantity`** (physical truth).
- **Reservation** — FK Variant, qty, FK Cart/checkout attempt, `expires_at`, status (`active` / `consumed` / `released` / `expired`).
- **InventoryMovement** — FK Variant, qty delta, reason (`seed` / `manual_adjustment` / `reservation_consumed` / `return` / `correction`), optional FK Reservation/Order/Staff user, created-at. This is the audit ledger for why stock changed.
- **Availability** = `on_hand quantity − Σ active reservations`. On confirmed sale, `quantity` decrements and the reservation is `consumed`; on failure/expiry it's `released`. `quantity` is never touched speculatively — only reservations move during checkout, keeping physical stock truthful.
- **Stock changes are ledgered** — every physical quantity adjustment creates an `InventoryMovement`; staff/admin changes cannot silently mutate stock without an audit trail.

**Database guarantees and indexes:**
- Unique `ProductVariant.sku`.
- Nonnegative stock checks (`quantity >= 0`, reservation qty > 0, order item qty > 0).
- Unique idempotency keys scoped to the actor/operation (`CheckoutAttempt`, Payment, refund).
- At most one finalized Order per `CheckoutAttempt`.
- Unique gateway payment reference once a payment is confirmed.
- Index active reservations by `(variant_id, status, expires_at)` for availability checks and expiry sweeps.
- Index customer/session order lookup fields and owner-scoping fields.
- Use database transactions, row locks, `F()` expressions, and constraints as the source of truth; application validation improves errors but never replaces the database guarantees.

---

## 5. Cart, Auth & Guest→Account Merge

- **Cart** — session-bound for guests (unguessable cart/session token), user-bound for customers. Line items reference variant + qty.
- **Guest checkout** — a cart exists before any user does; an order can be placed bound to the session/order token.
- **Account merge** — on login/registration, an existing guest cart **merges** into the user's cart (combine quantities, re-validate availability). This anonymous-cart→user-cart merge is a real design wrinkle and is in scope. When combined quantities exceed available stock, the merge **caps at available and surfaces a clear warning** rather than silently dropping or failing.
- **Cart→checkout price drift (explicit decision)** — the cart displays live prices; totals are **authoritatively snapshotted at checkout-start** on the `CheckoutAttempt`. The honored price is the checkout-start price, and if it differs from what the cart last showed, the customer is **notified of the change before payment** rather than surprised at the charge.
- **Auth** — Django auth for accounts; guest flows require no account.

---

## 6. Payments (simulated, Stripe-shaped)

- **Gateway interface** — a clean abstraction (`authorize` / `confirm` / `refund` / `get_payment_status`); the **simulated implementation** approves/declines (and can model delayed/async confirmation, and a *dropped-confirmation* mode) so the seam handles real-world payment asynchrony, including lost webhooks.
- **Authoritative confirmation** — order finalization is triggered by the confirmation event, not the client; idempotent and replayable (the webhook pattern, simulated).
- **Bidirectional reconciliation** — because confirmations can be lost, a reconciliation job polls `get_payment_status` for attempts stranded in `payment_pending` past a threshold and drives them to a terminal state. Waiting on a webhook is never the only recovery path.
- **Payment calls outside DB transactions** — gateway authorization/confirmation/refund calls are not made while holding database row locks; DB finalization happens after an authoritative gateway result is available.
- **Idempotency keys** on checkout/payment/refund prevent duplicate charges, duplicate orders, and duplicate refunds.
- **Confirmation event log** — simulated gateway callbacks are recorded with provider reference, payload, status, and processing result so replays and reconciliation are observable.
- **Refunds** modeled (status transition + gateway refund call) for the `refunded` lifecycle state.
- **No raw payment details** stored. The system stores provider references, status, amount, currency, and last-safe display metadata only.
- **Swappability** — replacing the simulated gateway with Stripe must not touch the cart→order seam.

---

## 7. Orders

- **Order** — FK user (nullable for guests), FK `CheckoutAttempt`, public order number, status (`placed` / `cancelled`), total (`Decimal`), shipping details snapshot, created-at.
- **OrderItem** — FK Order, FK Variant, **snapshotted SKU/price/attributes at purchase** (never references live price, which can change later), qty, line total.
- **Payment** — FK Order or FK `CheckoutAttempt` before order creation, status (`pending` / `authorized` / `confirmed` / `failed` / `refunded`), gateway reference, amount, currency, idempotency key.
- **Fulfillment** — FK Order, status (`unfulfilled` / `processing` / `shipped` / `delivered`), optional carrier/tracking fields, shipped-at/delivered-at.
- **User-facing lifecycle** — `pending → paid → processing → shipped → delivered`, plus `cancelled` and `refunded`, derived from order/payment/fulfillment state.
- **Transitions** are **server-enforced** (illegal jumps rejected) and **audited** (who/when/from→to). Fulfillment is driven from custom staff views; catalog/stock remains in Django admin.
- **OrderStatusEvent / AuditLog** — records status changes, staff actions, refund attempts, inventory adjustments, and payment confirmation processing.

---

## 8. API Layer (DB + API + Web) — first-class

- **Catalog API** — browse/search products + variants (read; public; paginated).
- **Cart/Order API** — add/update/remove cart items, begin checkout, submit payment, fetch order status (headless-commerce capable).
- **Auth** — session for the app's own frontend; token for programmatic/headless clients; guest cart via cart/order token.
- **Same guarantees as web** — overselling protection, idempotency, money handling, and owner-scoping all apply identically; the API is never a bypass. Consistent JSON error contract; cursor-paginated lists.
- **Versioned contract** — `/api/v1/...`; breaking changes require a new version.
- **OpenAPI schema** — generated and served for developers; request/response examples include checkout, idempotency, validation errors, and owner-scoping failures.
- **Idempotency header** — write endpoints that can create money/order side effects require or accept `Idempotency-Key`; behavior is documented and tested.
- **Stable error envelope** — machine-readable `code`, human message, field errors when relevant, and request/correlation id for support.

---

## 9. Security & Production Requirements

- **The three invariants (§1) are the #1 requirement** and are tested (concurrency, idempotency, crash-replay).
- All money is `Decimal`; totals computed server-side; client never trusted for prices/totals; **prices snapshotted** on order items.
- Guest cart/order tokens are unguessable; orders owner-scoped (customer or session); no cross-customer order access.
- Order status transitions server-enforced and audited.
- Account flows include registration, login, logout, password reset, email normalization, and optional email verification before privileged account actions.
- Staff/admin permissions are role-based; staff-only views require explicit permissions, and production staff accounts should use MFA where the host/provider supports it.
- `DEBUG=False` in prod; real `ALLOWED_HOSTS`; secrets (incl. `SECRET_KEY`, future payment keys) from env, never committed.
- HTTPS-only: SSL redirect, secure session + CSRF cookies, HSTS.
- CSRF on all web forms and session-authenticated API writes.
- Rate limiting on auth, checkout, and write endpoints.
- CORS is locked down for the API; CSP and secure upload validation protect the HTMX/web surface.
- Product image uploads validate content type, size, extension, and storage path; no user-controlled executable uploads.
- No raw payment credentials or card data are stored. Payment records contain provider-safe references and display-safe metadata only.

---

## 10. Production Scaffolding

- **Database:** PostgreSQL (dev + prod). No SQLite — the concurrency guarantees rely on real row-locking semantics.
- **Background work:** a scheduled **reservation-expiry sweep** (release abandoned reservations) — cron'd management command, or Celery Beat if otherwise justified. (Real async payment confirmation would warrant a worker; the simulated gateway keeps it light.)
- **File storage:** product images via local volume (dev) / object storage (prod).
- **Containerization:** `Dockerfile` (slim base, non-root, collected static, Gunicorn) + `docker-compose.yml` (web + postgres) for dev.
- **Settings:** env-driven config split; `.env.example` committed; real `.env` git-ignored.
- **Static:** WhiteNoise or host-native.
- **Seed data:** a realistic catalog with variants and stock so checkout/concurrency are exercised against real data.
- **Observability:** error tracking (e.g. Sentry), structured logging (incl. checkout lifecycle, reservation events, payment confirmations, status transitions), `/healthz` (200 + DB connectivity).
- **CI:** run formatting/linting, tests against PostgreSQL, migration checks, and basic security checks on every push/PR.
- **Release discipline:** migrations are reviewed before deploy; deploy command runs collectstatic, migrations, and health checks in a predictable order.
- **Backups/restore:** production posture includes managed Postgres backups and a documented restore check, even if the capstone deploy is small.
- **Host:** a PaaS/container host with managed Postgres, real domain, valid TLS. (Recommended default — swappable.)

---

## 11. Tech Stack (named, to prevent drift)

- Python 3.x, Django (latest stable)
- HTMX (cart updates, checkout flow, inline catalog interactions, admin fulfillment — no SPA framework)
- PostgreSQL (row-locking for reservations: `select_for_update`, `F()` atomic updates)
- Python `decimal.Decimal` for all money; price snapshots on order items
- A payment gateway abstraction + simulated implementation (Stripe-swappable)
- `django-environ` (config)
- A scheduler for the reservation-expiry sweep (cron'd management command, or Celery Beat if justified)
- DRF (recommended for the first-class API: serializers, token auth, throttling) — or plain Django
- OpenAPI generation (e.g. `drf-spectacular`) for the versioned API contract
- `django-storages` + object storage (product images) in prod
- A rate-limiting lib / DRF throttling
- CSP/CORS hardening (`django-csp`, `django-cors-headers` if API origins require it)
- Sentry (errors)
- pytest + pytest-django for unit/integration/concurrency tests
- Ruff/Black/isort and optional mypy/django-stubs for code quality
- Gunicorn + WhiteNoise
- Docker + docker-compose

---

## 12. Definition of Done

1. Five subsystems integrate end to end: a guest browses a variant catalog, builds a cart, checks out, "pays" (simulated), and receives an order — and a logged-in customer's guest cart merges on login.
2. **Never oversell:** a test simulating concurrent checkouts of the last unit of a variant yields exactly one successful order; availability never goes negative.
3. **Never double-charge/double-order:** a test with double-submitted checkout and replayed payment confirmation produces exactly one order and one charge (idempotency).
4. **Payment↔order consistency:** a simulated crash between payment-confirmed and order-created reconciles correctly on replay through `CheckoutAttempt` — no permanent paid-but-no-order, no order-but-unpaid, no duplicate order.
5. Reservations hold stock during checkout and **expire** via the sweep on abandonment, returning stock to available; failed payment releases immediately.
6. `CheckoutAttempt` is the center of checkout replay/idempotency: reservations, payment confirmation, final order, and failure/expiry states are visible and testable.
7. Variants carry their own SKU/price/stock; order items **snapshot** price/SKU at purchase and don't change when the live product price later changes.
8. Inventory changes are traceable through `InventoryMovement`; staff stock adjustments cannot happen without an audit entry.
9. Order, payment, and fulfillment state are internally separate; the public lifecycle remains simple and staff-friendly.
10. Order lifecycle transitions are server-enforced (illegal transitions rejected) and audited; staff fulfill orders via custom views; catalog/stock managed via Django admin.
11. The catalog + cart/order API mirrors the web with token auth, cursor pagination, OpenAPI docs, stable JSON errors, `Idempotency-Key`, and **identical** overselling/idempotency/money guarantees.
12. Core database constraints exist and are tested: unique SKU, unique idempotency keys, nonnegative quantities, one order per checkout attempt, unique confirmed gateway reference, and active-reservation indexes.
13. Security/production posture verifiable: `Decimal` money + server-side totals + price snapshots, `DEBUG=False`, secrets in env, HTTPS + secure cookies, CSRF, rate limits, unguessable guest tokens, owner-scoped orders, safe uploads, CORS/CSP posture, errors to Sentry, `/healthz` green.
14. CI runs lint/format checks, migration checks, and tests against PostgreSQL; release/deploy and backup/restore expectations are documented.
15. The payment gateway is swappable (Stripe could replace the simulator without touching the cart→order seam); `docker-compose up` runs web + postgres with seed catalog; the reservation sweep runs on schedule; no secret committed.

---

## 13. Suggested Build Order

1. Django + Postgres via docker-compose; env-driven settings; `.env.example`; seed catalog (products + variants + stock).
2. Catalog: Product/ProductVariant (SKU/price/quantity); browse/search; product images.
3. Database integrity pass: constraints/indexes for SKU, stock, reservations, idempotency keys, checkout attempts, payments, and order ownership.
4. Cart: session cart for guests, line items (variant + qty); add/update/remove via HTMX.
5. Auth (accounts) + **guest-cart→account merge** on login.
6. **Inventory reservation core:** Reservation model, `InventoryMovement`, availability calc, `select_for_update` reserve-at-checkout — then write the **concurrent-last-item test** and make it pass.
7. **CheckoutAttempt core:** persisted attempt state machine, cart/totals snapshot, owner/session binding, expiry handling, idempotency-key lookup.
8. **Payment gateway abstraction** + simulated implementation (approve/decline, authoritative confirmation), with **idempotency keys** and confirmation event logging.
9. **The seam:** assemble cart → attempt → reserve → pay → (confirm) → DB-finalize order (snapshot prices) → clear cart. Write the **double-submit / replay** idempotency test and the **crash-replay** reconciliation test.
10. Reservation expiry sweep (release abandoned holds); failed-payment release; reconciliation command for confirmed-payment/no-order attempts.
11. Order/payment/fulfillment lifecycle: server-enforced, audited status workflow; custom fulfillment views; Django admin for catalog/stock.
12. Refunds (status + gateway refund) for the `refunded` path.
13. First-class API (catalog + cart/order), token auth, cursor pagination, OpenAPI, stable errors, `Idempotency-Key`, **re-using the same seam/guarantees**; prove web/API parity on overselling + idempotency.
14. Security pass: Decimal/price-snapshot audit, CSRF, rate limiting/throttling, HTTPS/cookies, guest-token + owner-scoping checks, safe uploads, CORS/CSP, secrets audit.
15. CI/quality pass: pytest against Postgres, migration checks, lint/format, optional type checks.
16. Observability: checkout/reservation/payment/status logging, Sentry, `/healthz`. Deploy (host + managed Postgres, TLS); walk the full Definition of Done.
---

## 14. Product Boundary & GTM Readiness

This build is not a toy checkout demo. It is scoped as a production-shaped merchant storefront and lightweight order-management system that can be shown to a real seller, operated by staff, and extended toward a real payment provider without rewriting the core domain.

### 14.1 Product promise
The application answers five merchant-critical questions:
- Can customers browse, select variants, and check out reliably?
- Can the store prevent overselling under real concurrency?
- Can the system recover from payment/order edge cases without manual database repair?
- Can staff operate orders from paid to shipped to delivered?
- Can customers receive trustworthy order status and transactional communication?

### 14.2 Non-goals and boundaries
The product remains intentionally focused:
- Not a marketplace platform with multiple sellers.
- Not a warehouse-management system.
- Not a full tax-compliance product.
- Not a shipping-label purchasing platform.
- Not a subscription/recurring billing system.
- Not a SPA; Django + HTMX is the first-class web layer.

The architecture must still be shaped so tax, shipping, payment, and fulfillment providers can be swapped behind interfaces later.

---

## 15. Python/Django Architecture Standards

The codebase must demonstrate senior-level Python architecture: explicit domain boundaries, predictable transaction handling, testable services, and a clean separation between web/API transport and business behavior.

### 15.1 Required Django apps
Recommended app boundaries:
- `accounts`: registration, login, logout, password reset, email verification, account profile.
- `catalog`: products, variants, categories, collections, images, merchandising, search/filter selectors.
- `carts`: guest/user carts, cart items, cart merge, cart calculations.
- `checkout`: checkout attempts, checkout state machine, idempotency, snapshots.
- `inventory`: reservations, stock availability, inventory movements, stock adjustment workflow.
- `payments`: gateway abstraction, simulated provider, payment records, confirmation event log, refunds.
- `orders`: orders, order items, order lifecycle, fulfillment, customer order history.
- `staff_ops`: order queue, fulfillment screens, refund/cancel operations, support-facing workflows.
- `promotions`: coupon codes, discount rules, usage tracking, order discount snapshots.
- `shipping`: shipping calculators, shipping methods, shipping snapshots.
- `taxes`: tax calculator interface, simple configured-rate implementation, tax snapshots.
- `notifications`: email templates, transactional delivery log, queued notifications.
- `audit`: audit log, domain event history, staff action history.
- `api`: DRF routers/serializers/viewsets, OpenAPI configuration, token auth.
- `support`: checkout inspector, replay tooling, operational diagnostics.

### 15.2 Layering rules
- Views, HTMX endpoints, DRF serializers, Django admin actions, and scheduled jobs must call application services, not mutate checkout/payment/order/inventory state directly.
- Business invariants live in domain/application services and database constraints.
- Query-heavy pages use selectors/query services rather than embedding business queries in views.
- External provider behavior lives behind adapter interfaces.
- Background jobs call the same application services as web/API flows.
- Forms and serializers validate transport shape; services validate business rules.
- Database transactions are explicit and deliberately scoped.
- Gateway calls, email delivery, and other external side effects must not run inside long database transactions.

### 15.3 Core services
The implementation should include command-style services such as:
- `CartService`
- `CartMergeService`
- `CheckoutAttemptService`
- `InventoryReservationService`
- `PaymentService`
- `PaymentConfirmationService`
- `PaymentReconciliationService`
- `OrderFinalizationService`
- `FulfillmentService`
- `RefundService`
- `PromotionService`
- `ShippingCalculatorService`
- `TaxCalculatorService`
- `NotificationService`
- `AuditService`
- `IdempotencyService`

Each service should expose typed input/output objects where practical, return predictable result types, and raise domain-specific exceptions rather than leaking low-level errors to views.

### 15.4 Transaction discipline
- Reservation creation and order finalization must use `transaction.atomic()` with row locks or conditional atomic updates.
- Payment gateway calls occur outside active database transactions.
- Finalization after payment confirmation must be idempotent and safe to retry.
- Inventory movement creation must be in the same transaction as the stock mutation it explains.
- Any mutation that can create a charge, order, refund, reservation, or fulfillment transition must be protected by idempotency where applicable.
- **Concurrent same-key idempotency is defined, not just sequential replay.** When two requests share an idempotency key and race, the unique key lets one insert win; the loser **waits for and returns the winner's stored result**, or returns a clean in-progress `409` — it never errors out or starts a second operation. The `IdempotencyRecord` stores the response payload with a documented TTL.
- **Coupon redemption is atomic.** Usage-limit enforcement uses an atomic conditional update or promotion row lock plus unique `PromotionRedemption` constraints, committed inside finalization (§23.2) — never a read-then-check.

---

## 16. Architecture Decision Records

The repository must include ADRs documenting the system's important architectural commitments.

Required ADRs:
- **ADR-0001:** PostgreSQL is required; SQLite is not supported for this project because checkout correctness depends on real concurrency and row-lock behavior.
- **ADR-0002:** `CheckoutAttempt` is the replay and idempotency anchor for checkout.
- **ADR-0003:** Gateway calls happen outside database transactions.
- **ADR-0004:** Inventory uses reservation records, not speculative stock decrements.
- **ADR-0005:** Prices, discounts, shipping, tax, SKU, and product attributes are snapshotted onto orders.
- **ADR-0006:** Domain services own financial, inventory, checkout, order, and fulfillment mutations.
- **ADR-0007:** HTMX is the first-class web layer; DRF is the API surface.
- **ADR-0008:** Outbox events handle post-commit side effects.
- **ADR-0009:** The simulated gateway must remain Stripe-swappable.
- **ADR-0010:** Staff operations use custom views; Django admin is not the order-management system.
- **ADR-0011:** Guest checkout and guest order lookup use unguessable scoped tokens.
- **ADR-0012:** Product catalog changes never rewrite historical order records.
- **ADR-0013:** Coupon usage limits are database-enforced contended counters (atomic conditional update + unique PromotionRedemption), not application checks.
- **ADR-0014:** The reservation-expiry sweep never releases reservations for attempts in payment_pending or beyond; finalization re-asserts stock under lock with a compensating path.
- **ADR-0015:** Reconciliation is bidirectional and gateway-authoritative via get_payment_status; lost confirmations are polled to a terminal state, not waited on.
- **ADR-0016:** Refunds are compensating transactions — partial refunds apportion discount/tax/shipping, restock posts an InventoryMovement, and coupon-release is an explicit decision.
- **ADR-0017:** Concurrent same-key idempotency returns the winner's stored result (or in-progress 409); the record stores the response with a TTL.

---

## 17. Domain Events & Outbox

Production e-commerce requires reliable side effects. Order finalization should not directly send emails, call fulfillment hooks, or update analytics inside the checkout transaction. The system uses domain events plus an outbox table so side effects are durable, retryable, and observable.

### 17.1 Domain events
Important events include:
- `CartItemAdded`
- `CartItemUpdated`
- `GuestCartMerged`
- `CheckoutAttemptStarted`
- `CheckoutAttemptReserved`
- `InventoryReserved`
- `ReservationReleased`
- `ReservationExpired`
- `PaymentAuthorized`
- `PaymentAuthorizationFailed`
- `PaymentConfirmed`
- `PaymentConfirmationFailed`
- `OrderFinalized`
- `OrderCancelled`
- `OrderRefundRequested`
- `RefundCompleted`
- `InventoryAdjusted`
- `FulfillmentProcessingStarted`
- `OrderShipped`
- `OrderDelivered`
- `TransactionalEmailQueued`
- `TransactionalEmailDelivered`
- `TransactionalEmailFailed`

### 17.2 Outbox table
`OutboxEvent` fields:
- `id`
- `event_type`
- `aggregate_type`
- `aggregate_id`
- `payload` JSONB
- `status`: `pending` / `processing` / `processed` / `failed`
- `attempts`
- `last_error`
- `created_at`
- `processed_at`

Outbox processing requirements:
- Events are written in the same database transaction as the state change they represent.
- Event handlers are idempotent.
- Failed events retry with bounded backoff.
- Repeated failure lands in an operator-visible failed state.
- Support/admin tooling can inspect failed outbox events without silently mutating order state.

---

## 18. Checkout State Machine

The checkout attempt is a durable state machine. It is not a loose set of booleans.

### 18.1 CheckoutAttempt states
Recommended internal states:
- `started`
- `reserved`
- `payment_pending`
- `payment_confirmed`
- `finalizing`
- `finalized`
- `failed`
- `expired`
- `cancelled`

### 18.2 Allowed transitions
| From | To | Source | Notes |
|---|---|---|---|
| none | `started` | customer/system | Attempt created or reused by idempotency key |
| `started` | `reserved` | system | Cart snapshot validated and stock reserved |
| `reserved` | `payment_pending` | system | Gateway authorization/confirmation process begins |
| `payment_pending` | `payment_confirmed` | gateway event | Authoritative confirmation received |
| `payment_confirmed` | `finalizing` | system | Finalization lock acquired |
| `finalizing` | `finalized` | system | Order created, reservations consumed, cart cleared |
| `reserved` | `failed` | gateway/system | Payment failed; reservations released |
| `payment_pending` | `failed` | gateway/system | Authorization/confirmation failed; reservations released |
| `started` | `expired` | scheduler | No reserved stock or attempt timeout |
| `reserved` | `expired` | scheduler | Reservation timeout; stock released |
| `finalized` | any earlier state | never | Illegal |

### 18.3 State-machine rules
- Illegal transitions are rejected and audited.
- Replaying the same valid event is a no-op when the target state has already been reached.
- A confirmed payment with no order is recoverable: finalization runs again and creates or links the order exactly once.
- Expired or failed attempts cannot be finalized unless a valid confirmed payment event already exists and the system is reconciling a recoverable intermediate state.
- **The expiry sweep only acts on attempts in `started` or `reserved`.** An attempt that has reached `payment_pending`, `payment_confirmed`, or `finalizing` is protected from expiry — its reservations are frozen/extended until the payment resolves — so the sweep can never release stock while a confirmation is in flight (§3).
- **A `payment_pending` attempt stranded past a threshold is resolved by the reconciliation job**, which polls `get_payment_status` and drives it to `payment_confirmed` (→ finalize) or `failed` (→ release) — it is not silently expired.
- **Finalization re-asserts stock under lock.** If a confirmed attempt cannot obtain its reserved stock (out-of-band adjustment or bug), it enters a defined compensating path (auto-refund/backorder) rather than overselling or stranding a paid order.
- Every transition stores actor/source, from state, to state, reason, and request/correlation ID.

---

## 19. Payment Lifecycle Detail

The simulated payment implementation must behave like a production gateway seam, not like a boolean success flag.

### 19.1 Payment states
Recommended states:
- `requires_payment_method`
- `authorized`
- `authorization_failed`
- `confirmation_pending`
- `confirmed`
- `confirmation_failed`
- `cancelled`
- `refunded`
- `partially_refunded`
- `disputed`

### 19.2 Payment requirements
- All gateway actions require idempotency keys.
- Gateway references are unique once confirmed.
- Confirmation events are authoritative and replayable.
- Duplicate confirmation events do not create duplicate orders.
- Gateway timeouts create recoverable states; they do not assume success or failure without confirmation.
- **A stranded `payment_pending` is resolved by polling `get_payment_status`**, not by waiting indefinitely for a webhook; the reconciliation job is the recovery path for lost confirmations.
- Refunds support full and partial amounts.
- Refund records have their own idempotency keys and event log.
- Payment and refund records store provider-safe metadata only.
- Raw card data, CVV, or payment credentials are never stored.

### 19.3 Simulated webhook behavior
The simulated gateway should support:
- immediate success
- immediate decline
- delayed confirmation
- duplicated confirmation event
- out-of-order confirmation event
- gateway timeout
- **dropped/lost confirmation** (event never arrives; only `get_payment_status` polling can resolve it)
- refund success
- refund failure

These modes exist to prove the system can survive real payment provider behavior later.

### 19.4 Reconciliation job
A scheduled reconciliation job closes recoverable intermediate states without human database surgery:
- **Paid-but-no-order** (confirmation arrived, finalization didn't complete) → replay finalization (create/link order exactly once).
- **Stranded `payment_pending`** (confirmation lost) → poll `get_payment_status`; drive to confirmed (→ finalize) or failed (→ release reservations).
- The job is idempotent and safe to run repeatedly; every action it takes is audited.
- Confirmed-payment-no-order and stranded-pending counts are metrics and alerts (§29).

---

## 19a. Refund as a Compensating Transaction

Refunds are not a money-only status flip; they are compensating actions across money, inventory, and promotions.

### 19a.1 Partial-refund allocation
- A partial refund of a multi-item order **apportions order-level discount, tax, and (where applicable) shipping across the refunded lines**, using the same deterministic allocation discipline as split reconciliation — you cannot refund a line's list price while ignoring the coupon that reduced the order total.
- Allocation is computed server-side and snapshotted onto the refund record; totals reconcile exactly (no lost or extra cents).

### 19a.2 Inventory coupling
- When restock is chosen, the refund posts a compensating `InventoryMovement` (reason `return`), so refunded stock re-enters availability through the ledger, never silently.

### 19a.3 Promotion coupling
- Whether a refund **releases the customer's coupon redemption** (freeing a use against a usage/per-customer limit) is an explicit, tested decision. If released, the `PromotionRedemption` counter is decremented under the same atomic discipline used to increment it.

### 19a.4 Idempotency
- Refund records have their own idempotency keys and event log; refund replay does not double-refund, double-restock, or double-release a redemption.

---

## 20. Staff Operations / Lightweight OMS

The product must include enough merchant operations to run orders after checkout. Django admin is acceptable for catalog setup and stock records, but fulfillment requires a custom staff experience.

### 20.1 Staff order queue
Staff views must include:
- order queue grouped by status
- search by order number, email, customer, SKU, tracking number
- filters by paid, unfulfilled, processing, shipped, delivered, cancelled, refunded
- order detail page with customer, shipping address, items, payment state, fulfillment state, audit timeline
- staff notes visible only internally
- customer-visible timeline preview

### 20.2 Fulfillment workflow
Required staff actions:
- mark order as processing
- enter carrier and tracking number
- mark shipped
- mark delivered
- cancel unfulfilled order when allowed
- initiate refund
- choose restock behavior for refunds/cancellations:
  - refund only
  - refund and restock
  - refund and do not restock

### 20.3 Inventory operations
Staff/admin operations must include:
- low-stock report
- variant stock adjustment with reason
- inventory movement history by SKU
- high-risk stock adjustment flagging
- no silent stock mutation without `InventoryMovement`

### 20.4 Audit expectations
Every staff operation records:
- actor
- timestamp
- source IP/request ID where available
- before/after state
- reason when the action changes payment, order, fulfillment, or inventory state

---

## 21. Customer Notifications

The storefront must send or queue transactional notifications for core customer events. Notifications are part of the production product, not optional decoration.

### 21.1 Required notification templates
- account registration / verification
- password reset
- order confirmation
- payment failed
- order shipped
- order delivered
- order cancelled
- refund issued
- guest order lookup link

### 21.2 Notification delivery log
`EmailDelivery` fields:
- `id`
- `recipient_email`
- `template_key`
- `related_order`
- `related_user`
- `status`: `queued` / `sent` / `failed` / `suppressed`
- `provider_message_id`
- `error_message`
- `queued_at`
- `sent_at`
- `failed_at`

### 21.3 Notification rules
- Email sending is triggered through outbox events, not directly inside checkout finalization.
- Templates render from snapshotted order data, not live product data.
- Failed notifications are visible in staff/support tooling.
- Notification handlers are idempotent.
- Guest order emails include scoped, unguessable lookup tokens.

---

## 22. Tax and Shipping Adapter Boundaries

Tax and shipping are deliberately simple in this build, but the architecture should not trap the product in toy assumptions.

### 22.1 Tax calculator
Tax is not a full compliance engine in this phase. The system includes a pluggable `TaxCalculator` interface with a simple configured-rate implementation for development and demo use.

Requirements:
- Order stores subtotal, discount, shipping, tax, and total separately.
- Tax amount is calculated server-side and snapshotted onto the checkout attempt and order.
- Tax calculator input includes shipping address, taxable subtotal, currency, and product tax flags where available.
- A real tax provider can replace the simple implementation later without rewriting checkout finalization.

### 22.2 Shipping calculator
Shipping uses a pluggable `ShippingCalculator` interface.

Launch implementation supports:
- flat-rate shipping
- free-shipping threshold
- simple method labels such as `Standard` / `Express` if desired

Requirements:
- Order snapshots selected shipping method, amount, recipient, and delivery address.
- Shipping amount is calculated server-side.
- Shipping changes after order placement do not mutate historical totals.

---

## 23. Promotions and Discounts

A production-shaped storefront needs basic discount support while preserving server-authoritative totals.

### 23.1 Promotion types
Supported promotion rules:
- percentage discount
- fixed amount discount
- free shipping discount
- minimum order subtotal
- expiration date
- usage limit
- per-customer usage limit where a user exists
- active/inactive status

### 23.2 Coupon behavior
- Coupon codes are normalized server-side.
- Validation happens on every cart/checkout recalculation.
- Discounts are snapshotted onto checkout attempts and orders.
- Expired or exhausted coupons fail with stable user-facing errors.
- Coupon application/removal works via HTMX and API.
- Promotions cannot reduce totals below zero.
- **Usage limits are database-enforced contended counters, not application checks.** Redemption uses an atomic conditional update (`UPDATE ... SET used = used + 1 WHERE used < limit`, checking rows-affected) or a row lock on the promotion, plus a **unique constraint on `PromotionRedemption(promotion, order)`** and on `(promotion, customer)` where per-customer-limited. Redemption is committed **inside the order-finalization transaction**. This gives coupons the same concurrency protection inventory already has: two simultaneous last-use redemptions cannot both succeed.
- **Refund/cancel release** of a redemption (if policy frees the use) decrements the counter under the same atomic discipline (§19a.3).

### 23.3 Data model
Recommended models:
- `Promotion`
- `CouponCode`
- `PromotionRedemption`
- `OrderDiscountSnapshot`

---

## 24. Catalog Merchandising

The catalog should be strong enough to feel like a real storefront, not just a table of SKUs.

### 24.1 Catalog features
Required catalog capabilities:
- product categories
- collections
- product status: `draft` / `active` / `archived`
- product slugs
- SEO title/meta description fields
- product images with sort order
- variant option groups such as size/color
- search
- filtering by category, price range, availability, and variant attributes
- featured products or collection placement
- related products/manual recommendations

### 24.2 Stock visibility rules
Per product or store setting:
- show exact quantity
- show `low stock`
- hide quantity but show availability
- hide out-of-stock variants
- show out-of-stock variants as disabled

### 24.3 Catalog invariants
- Archived products remain visible on historical orders.
- Live product price changes never affect existing order items.
- SKU uniqueness is global unless the system explicitly supports multiple catalogs later.
- Product images are validated for type, size, and safe storage path.

---

## 25. API Endpoint Contract

The API is first-class and must expose the same core product capabilities as the Django + HTMX web app without bypassing domain guarantees.

### 25.1 Public/customer endpoints
Recommended endpoint groups:

```text
/api/v1/catalog/products/
/api/v1/catalog/products/{slug}/
/api/v1/catalog/categories/
/api/v1/catalog/collections/
/api/v1/cart/
/api/v1/cart/items/
/api/v1/cart/apply-coupon/
/api/v1/cart/remove-coupon/
/api/v1/checkout/attempts/
/api/v1/checkout/attempts/{id}/confirm-payment/
/api/v1/orders/
/api/v1/orders/{order_number}/
/api/v1/guest/orders/{order_token}/
```

### 25.2 Staff endpoints
Recommended staff endpoint groups:

```text
/api/v1/staff/orders/
/api/v1/staff/orders/{id}/transition/
/api/v1/staff/orders/{id}/refund/
/api/v1/staff/orders/{id}/cancel/
/api/v1/staff/fulfillments/
/api/v1/staff/inventory/adjustments/
/api/v1/staff/checkout-attempts/{id}/
/api/v1/staff/checkout-attempts/{id}/replay-finalization/
```

### 25.3 API standards
- Versioned namespace: `/api/v1/`.
- OpenAPI schema generated and reviewed in CI.
- Cursor pagination for lists.
- Stable error envelope with `code`, `message`, `field_errors`, and `request_id`.
- `X-Request-ID` accepted and propagated.
- `Idempotency-Key` required for endpoints that can create payment, order, refund, reservation, or checkout side effects.
- Token authentication for programmatic clients; session authentication for the first-party web app.
- Object-level permissions on every cart/order/staff endpoint.
- API and HTMX flows must reuse the same domain services.
- Contract tests cover success, validation failure, permission failure, idempotency replay, and concurrency-sensitive paths.

---

## 26. HTMX Web Layer Requirements

HTMX is the first-class web UX. The app must feel modern without becoming a SPA.

### 26.1 HTMX rules
- Every HTMX mutation has a full-page fallback where practical.
- HTMX partials are first-class templates with tests.
- Mutations return focused partial updates plus out-of-band updates for cart count, totals, stock warnings, and checkout state.
- Inline validation must preserve user-entered data.
- CSRF protection applies to all HTMX forms.
- Server-rendered templates remain accessible without JavaScript for primary read paths.
- Loading, disabled, and duplicate-submit states are visible on checkout actions.

### 26.2 Recommended template structure
```text
templates/
  catalog/
    product_list.html
    product_detail.html
    partials/
      _product_grid.html
      _variant_picker.html
      _stock_badge.html
  carts/
    cart_detail.html
    partials/
      _cart_lines.html
      _cart_totals.html
      _cart_badge.html
      _coupon_form.html
  checkout/
    checkout_start.html
    payment.html
    complete.html
    partials/
      _checkout_summary.html
      _payment_status.html
  orders/
    order_detail.html
    order_history.html
  staff_ops/
    order_queue.html
    order_detail.html
    partials/
      _order_status_controls.html
      _fulfillment_panel.html
      _refund_panel.html
```

---

## 27. Security Verification Baseline

This app is money-adjacent and customer-data-bearing. Security must be testable, not aspirational.

### 27.1 Baseline
Required security baseline:
- Django deployment checklist reviewed before production release.
- OWASP ASVS-inspired controls for authentication, access control, validation, session management, logging, and data protection.
- `DEBUG=False`, strict `ALLOWED_HOSTS`, secure cookies, HSTS, CSRF, and HTTPS enforcement in production.
- Secrets in environment/secret manager only.
- No raw payment credentials stored.
- PII redacted from logs.
- CORS locked to approved origins.
- CSP configured for the web layer.
- Upload validation for product images.
- Rate limits on login, password reset, checkout, payment confirmation, refund, guest order lookup, and staff-sensitive endpoints.

### 27.2 Authorization tests
Required permission tests:
- customer cannot access another customer's order
- guest cannot guess another guest order
- user cannot mutate another user's cart
- staff endpoint denies non-staff users
- staff user without refund permission cannot refund
- staff user without inventory permission cannot adjust stock
- API token cannot bypass web permissions

### 27.3 Staff/admin hardening
- Staff accounts use least-privilege groups.
- Staff MFA expectation is documented.
- No shared staff accounts.
- Sensitive staff reads/actions are audited.
- Support impersonation, if included, requires reason, time limit, and audit entry.

---

## 28. Data Retention, Privacy & PII Handling

### 28.1 PII boundaries
PII includes customer name, email, shipping address, billing address if present, phone number if present, and order communication metadata.

Requirements:
- PII fields are not logged in raw form.
- Staff views reveal only what is needed to fulfill/support the order.
- Exports are staff-permission-protected.
- Guest order lookup tokens are scoped, unguessable, and revocable.
- Account deletion/export procedures are documented.

### 28.2 Retention behavior
- Orders remain retained for merchant records unless explicitly deleted/anonymized under a documented process.
- Payment provider references are retained as necessary for refunds/reconciliation.
- Expired carts and expired checkout attempts can be cleaned up after a documented retention window.
- Audit logs are append-only and retained according to operational policy.

---

## 29. Observability & Operational Metrics

Production readiness requires visibility into the checkout seam.

### 29.1 Structured logs
Structured logs must include:
- request ID
- user/session/cart token reference where safe
- checkout attempt ID
- order number
- payment reference
- SKU/variant ID where relevant
- event type
- state transition
- error code

PII and payment-sensitive fields must be redacted.

### 29.2 Metrics
Track:
- checkout attempts started
- reservation success/failure rate
- payment authorization failure rate
- payment confirmation latency
- order finalization latency
- finalization failure count
- confirmed-payment-no-order count
- reservation expiry count
- reservation release count
- oversell-prevention count
- idempotency replay count
- refund success/failure count
- cart abandonment count
- email delivery success/failure
- API error rate and latency
- staff fulfillment cycle time

### 29.3 Alerts
Alert on:
- confirmed payments without finalized orders
- repeated finalization failures
- outbox backlog above threshold
- failed reservation sweep
- elevated payment failures
- elevated 500s on checkout/API endpoints
- failed transactional email queue

---

## 30. Support Console & Operational Tooling

A production-grade product needs tools to understand and fix recoverable states without direct database surgery.

### 30.1 Checkout inspector
Support/staff tooling should show:
- checkout attempt state
- cart snapshot
- reservations
- payment records
- payment confirmation events
- linked order
- audit trail
- outbox events
- retry/replay eligibility

### 30.2 Safe support actions
Support actions may include:
- replay order finalization for a confirmed-payment/no-order attempt
- release expired reservations
- resend transactional email
- inspect failed outbox event
- view payment confirmation event payload metadata

All support actions must be permission-protected and audited. No support action may silently mutate financial, inventory, or order state.

---

## 31. Performance Targets

Required performance targets:
- Catalog page p95 server response: under 300ms excluding image delivery.
- Product detail p95 server response: under 300ms.
- Cart mutation p95 server response: under 250ms.
- Checkout finalization p95 database transaction: under 500ms excluding gateway call.
- API list endpoints are paginated and indexed.
- Product image delivery is object-storage/CDN-ready.
- Concurrent checkout test covers at least 20 simultaneous attempts against a last-stock SKU.
- Reservation sweep is idempotent and safe to rerun.
- Search/filter queries have appropriate indexes and do not cause unbounded table scans at expected catalog size.

---

## 32. Expanded Testing Strategy

Testing must prove the invariants and the architecture, not just happy-path views.

### 32.1 Unit tests
- money calculations with `Decimal`
- cart totals and coupon validation
- shipping and tax calculators
- checkout state transitions
- payment state transitions
- inventory availability calculation
- order lifecycle derivation

### 32.2 Integration tests
- guest browse → cart → checkout → payment → order
- logged-in customer cart and order history
- guest-cart-to-account merge
- coupon apply/remove/recalculate
- failed payment releases reservations
- abandoned checkout expires reservations
- refund with restock
- refund without restock
- order shipped notification queued

### 32.3 Concurrency and replay tests
Required tests:
- concurrent checkout of last unit produces exactly one successful order
- double-submit checkout creates one attempt/order/payment
- duplicated payment confirmation creates one order
- crash after payment confirmation but before order creation is recoverable
- reservation sweep can run twice without double-release
- refund replay does not double-refund
- concurrent last-use coupon redemption produces exactly one redemption (no over-redeem)
- expiry sweep does not release a reservation whose payment is in flight/confirmed (no oversell, no stranded paid order)
- a lost confirmation is resolved by the reconciliation poll, not left pending forever
- partial refund apportions discount/tax/shipping and reconciles exactly to the cent
- concurrent same-key requests both resolve to the single winner's result
- API and HTMX checkout paths share the same guarantees

### 32.4 Security tests
- owner-scoping tests for orders and carts
- guest token lookup tests
- staff permission tests
- CSRF checks on web/HTMX mutations
- API auth/throttling checks
- upload validation tests

### 32.5 Contract and UI tests
- OpenAPI generation check
- API contract tests for stable errors and idempotency behavior
- HTMX partial tests
- Playwright smoke tests for customer checkout and staff fulfillment
- Accessibility smoke tests for core pages

---

## 33. Expanded Definition of Done

The original Definition of Done remains binding. Add these additional completion gates:

1. Domain services own checkout, payment, inventory, order, promotion, shipping, tax, refund, and fulfillment behavior; views/API/admin actions do not bypass them.
2. ADRs exist for the major architecture decisions listed in §16.
3. Domain events and outbox records are written for major checkout, payment, inventory, order, fulfillment, refund, and notification events.
4. Checkout state machine transitions are explicit, enforced, audited, and tested.
5. Payment lifecycle supports delayed confirmation, duplicate confirmation, timeout, full refund, partial refund, and replay behavior through the simulated gateway.
6. Staff order-management views support order queue, fulfillment, cancellation, refund, restock choice, staff notes, and audit timeline.
7. Customer notifications are queued through the outbox and logged in `EmailDelivery`.
8. Tax and shipping use pluggable calculator interfaces with simple launch implementations and order snapshots.
9. Promotions/coupons support percentage, fixed amount, free shipping, usage limits, expiration, and order snapshots.
10. Catalog merchandising supports categories, collections, product statuses, slugs, SEO metadata, images, variant options, search, filters, and stock visibility rules.
11. API endpoints are documented, versioned, idempotency-aware, throttled, permission-protected, and covered by contract tests.
12. HTMX partials support cart, checkout, coupon, stock warning, and staff fulfillment interactions with progressive fallback where practical.
13. Security baseline is documented and tested: owner scoping, staff permissions, rate limits, CSRF, safe uploads, CORS/CSP, secure settings, and PII redaction.
14. Support console can inspect checkout attempts, payment events, reservations, order links, audit trail, and outbox status.
15. Metrics/logs/alerts cover the checkout seam, reservations, payments, finalization, refunds, and notifications.
16. Performance targets are documented and tested where practical.
17. Coupon usage limits are database-enforced; a concurrent test proves a last-use coupon cannot over-redeem.
18. The expiry sweep cannot release a reservation for an attempt whose payment is in flight or confirmed; a test proves the sweep-vs-payment race does not oversell or strand a paid order.
19. A stranded `payment_pending` (lost confirmation) is resolved by the reconciliation job polling `get_payment_status`, driving it to a terminal state.
20. Partial refunds apportion discount/tax/shipping across lines, post a compensating `InventoryMovement` on restock, and apply the coupon-release decision; refund replay is idempotent.
21. Concurrent same-key idempotency returns the winner's result (or in-progress 409) rather than erroring or duplicating.
22. Guest-cart merge caps at available stock with a warning; cart→checkout price drift is snapshotted at checkout-start and surfaced to the customer.

---

## 34. Revised Build Order for the Full Product

This is not an MVP sequence. It is an implementation order for the full enterprise-grade product so foundations land before dependent capabilities.

1. Project scaffolding: Django, Postgres, Docker, env settings, health check, lint/test tooling.
2. Core architecture: app boundaries, base models, service/result patterns, domain exceptions, audit foundation.
3. Catalog foundation: products, variants, categories, collections, images, slugs, active/draft/archive status.
4. Inventory foundation: variant stock, reservations, inventory movements, constraints, indexes.
5. Cart foundation: guest cart, customer cart, cart items, HTMX cart interactions, server-side totals.
6. Auth and account flows: registration, login, logout, password reset, optional verification, guest-cart merge.
7. Tax/shipping/promotion calculators: simple implementations plus snapshots and cart recalculation; coupon redemption as a database-enforced contended counter (atomic conditional update + unique constraints).
8. CheckoutAttempt state machine: attempt creation/reuse, idempotency (including concurrent same-key semantics), cart snapshot, state transitions.
9. Reservation core: reserve under lock, release, expire, consume, inventory ledger, concurrent last-item test; expiry sweep that protects in-flight-payment attempts.
10. Payment gateway abstraction: simulated provider, delayed/duplicate/out-of-order/dropped confirmation, `get_payment_status`, event log, idempotency.
11. Order finalization seam: confirmed payment → atomic finalization (re-asserting stock under lock) → order/items/payment/fulfillment/cart clearing/coupon redemption/outbox.
12. Replay/reconciliation commands: confirmed-payment-no-order recovery, stranded-payment-pending polling via `get_payment_status`, expired reservation sweep, failed outbox inspection.
13. Orders and customer account: order history, guest lookup, order detail timeline.
14. Staff operations: order queue, fulfillment, shipment/tracking, cancel, refund (partial-refund allocation + restock InventoryMovement + coupon-release), restock choice, staff notes.
15. Notifications: templates, outbox handlers, delivery log, resend support.
16. API: catalog/cart/checkout/orders/staff endpoints, token auth, OpenAPI, stable errors, idempotency headers.
17. HTMX polish: partials, out-of-band updates, duplicate-submit prevention, inline validation, mobile-first views.
18. Security pass: permissions, rate limits, CSRF, CORS/CSP, safe uploads, secure settings, PII redaction.
19. Observability/support: structured logs, metrics, checkout inspector, outbox dashboard, Sentry, alerts.
20. Testing pass: unit, integration, concurrency, replay, contract, HTMX, Playwright smoke, accessibility smoke.
21. Production deployment: managed Postgres, object storage, TLS, backups, restore check, release runbook.

---

**End of revised scope.**
