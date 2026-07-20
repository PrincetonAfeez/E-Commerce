# ADR-0003: Coupon Usage Is a Contended Counter

## Status
Accepted
 
## Decision
Promotion usage is redeemed inside order finalization. The service locks the promotion and uses an atomic conditional update when a global usage limit exists. `PromotionRedemption` unique constraints prevent duplicate order redemption and duplicate active per-customer use for one-use customer limits.

## Consequences
Concurrent last-use coupon attempts cannot both commit. If redemption fails, the surrounding order finalization transaction rolls back.
