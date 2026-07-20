# ADR-0004: Gateway-Authoritative Reconciliation
 
## Status
Accepted

## Decision
The simulated gateway exposes `get_payment_status`. A reconciliation command polls checkout attempts stranded in `payment_pending` and drives them to confirmed finalization or failed reservation release based on gateway status.

## Consequences
The system is not dependent on a client redirect or webhook delivery. Lost confirmation is treated as a recoverable intermediate state.
