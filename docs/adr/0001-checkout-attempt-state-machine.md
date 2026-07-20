# ADR-0001: CheckoutAttempt Anchors Checkout Finalization


## Status
Accepted

## Decision
Every checkout creates or reuses a persisted `CheckoutAttempt` scoped by cart and idempotency key. The attempt owns cart price snapshots, reservations, payment references, and the final order link.

Gateway calls happen outside database transactions. Once a payment is confirmed, finalization locks the attempt, consumes reservations, creates the order, links payment, redeems coupon usage, clears the cart, and writes outbox/audit records in one database transaction.

## Consequences
Confirmation replay is safe: if the order already exists, the service returns it. If a confirmed payment has no order, replay can roll forward exactly once.
