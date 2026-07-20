# ADR-0002: Reservation-Based Inventory
 
## Status
Accepted

## Decision
Cart checkout reserves stock by creating active `Reservation` rows under row locks. Physical `ProductVariant.quantity` is decremented only when a confirmed payment is finalized into an order.

The expiry sweep excludes attempts in `payment_pending`, `payment_confirmed`, and `finalized`, so a payment in flight cannot lose its reservation to cleanup.

## Consequences
Available stock is `quantity - active reservations`. Failed and abandoned checkouts release or expire reservations without mutating physical stock.
