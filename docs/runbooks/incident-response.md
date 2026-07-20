# Runbook: Incident Response
 
How we detect, triage, communicate, and learn from production incidents.

## Severity levels

| Sev | Definition | Example | Response |
|-----|------------|---------|----------|
| **SEV1** | Platform-wide outage or data loss/exposure | All storefronts down; DB unavailable; suspected breach | Page on-call immediately; all-hands |
| **SEV2** | Major degradation, single subsystem | Checkout failing; email not sending; one region degraded | Page on-call; fix within hours |
| **SEV3** | Minor / limited impact, workaround exists | One tenant's theme broken; slow endpoint | Next business day |

## Roles

- **Incident Commander (IC)** — owns coordination and decisions; usually the on-call.
- **Comms** — owns status updates to stakeholders/affected tenants.
- **Ops/Eng** — investigates and remediates.

For SEV3 one person may hold all roles. On-call rotation and escalation are in
[operations.md](operations.md).

## Flow

1. **Detect** — alert fires (uptime probe on `/healthz/`, error-rate/latency alert, or
   Sentry) or a report comes in.
2. **Declare** — assign a severity and an IC. Open an incident channel/thread.
3. **Assess** — scope: which tenants, which subsystems, since when. Check recent deploys
   first (most incidents follow a change → consider rollback, see
   [operations.md](operations.md)).
4. **Mitigate** — stop the bleeding before root-causing: roll back, scale, disable the
   feature, or fail over ([disaster-recovery.md](disaster-recovery.md)).
5. **Communicate** — post initial status within 30 min of a SEV1/2, then at regular
   intervals until resolved.
6. **Resolve** — confirm recovery via `/healthz/` and a storefront + checkout smoke test.
7. **Review** — blameless postmortem within 3 business days for SEV1/2.

## Security incidents

If a breach or data exposure is suspected: treat as **SEV1**, preserve the append-only
audit log (`AuditLog` is read-only in admin — do not purge), rotate credentials/secrets,
and follow the disclosure obligations in `SECURITY.md`. Because payments are simulated,
there is no cardholder data at risk — but customer PII (names, emails, addresses) is.

## Postmortem template

- **Summary** — what happened, impact, duration.
- **Timeline** — detection → mitigation → resolution (UTC).
- **Root cause** — the actual cause, not the trigger.
- **What went well / what didn't.**
- **Action items** — owned, dated, tracked to completion.
