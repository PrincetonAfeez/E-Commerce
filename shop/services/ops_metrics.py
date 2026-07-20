"""Platform operations metrics for on-call dashboards and alerting."""
from __future__ import annotations

from django.db.models import Count

from shop.models import CheckoutAttempt, OutboxEvent, Payment
from shop.services.dead_letters import dead_letter_counts


def collect_ops_metrics() -> dict:
    outbox = OutboxEvent._base_manager.filter(status=OutboxEvent.Status.PENDING).count()
    dlq = dead_letter_counts()
    stranded = CheckoutAttempt._base_manager.filter(status=CheckoutAttempt.Status.PAYMENT_PENDING).count()
    requires_refund = Payment._base_manager.filter(status=Payment.Status.REQUIRES_REFUND).count()
    attempts_by_status = {
        row["status"]: row["count"]
        for row in CheckoutAttempt._base_manager.values("status").annotate(count=Count("id"))
    }
    from shop.feature_flags import enabled_flags

    return {
        "outbox_pending": outbox,
        "outbox_failed": dlq["outbox_failed"],
        "webhook_deliveries_failed": dlq["webhook_deliveries_failed"],
        "stranded_payment_pending": stranded,
        "payments_requires_refund": requires_refund,
        "checkout_attempts_by_status": attempts_by_status,
        "feature_flags": enabled_flags(),
    }
