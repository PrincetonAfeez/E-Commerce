# ADR-0005: Refunds Are Compensating Transactions
 
## Status
Accepted

## Decision
Refunds are idempotent by payment and key. Refund amounts are apportioned across order lines using line totals that already include allocated discount, tax, and shipping. Optional restock creates compensating `InventoryMovement` rows. Full refunds can release coupon redemptions when the promotion policy allows it.

## Consequences
Refund replay does not double-refund or double-restock, and support actions remain audited.
