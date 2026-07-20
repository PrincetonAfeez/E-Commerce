"""Dead-letter queue re-drive for failed outbox events and webhook deliveries."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from shop.models import OutboxEvent, WebhookDelivery


def requeue_failed_outbox(*, limit: int = 200) -> int:
    """Move terminal FAILED outbox events back to PENDING for operator re-drive."""
    requeued = 0
    failed = list(
        OutboxEvent._base_manager.filter(status=OutboxEvent.Status.FAILED)
        .order_by("updated_at")
        .values_list("pk", flat=True)[:limit]
    )
    for pk in failed:
        with transaction.atomic():
            event = OutboxEvent._base_manager.select_for_update().filter(pk=pk).first()
            if event is None or event.status != OutboxEvent.Status.FAILED:
                continue
            event.status = OutboxEvent.Status.PENDING
            event.attempts = 0
            event.last_error = ""
            event.available_at = timezone.now()
            event.sent_at = None
            event.save(update_fields=["status", "attempts", "last_error", "available_at", "sent_at", "updated_at"])
            requeued += 1
    return requeued


def requeue_failed_webhooks(*, limit: int = 200) -> int:
    """Move terminal FAILED webhook deliveries back to PENDING for operator re-drive."""
    requeued = 0
    failed = list(
        WebhookDelivery.objects.filter(status=WebhookDelivery.Status.FAILED)
        .order_by("updated_at")
        .values_list("pk", flat=True)[:limit]
    )
    for pk in failed:
        with transaction.atomic():
            delivery = WebhookDelivery.objects.select_for_update().filter(pk=pk).first()
            if delivery is None or delivery.status != WebhookDelivery.Status.FAILED:
                continue
            delivery.status = WebhookDelivery.Status.PENDING
            delivery.attempts = 0
            delivery.last_error = ""
            delivery.response_code = None
            delivery.save(update_fields=["status", "attempts", "last_error", "response_code", "updated_at"])
            requeued += 1
    return requeued


def dead_letter_counts() -> dict[str, int]:
    return {
        "outbox_failed": OutboxEvent._base_manager.filter(status=OutboxEvent.Status.FAILED).count(),
        "webhook_deliveries_failed": WebhookDelivery.objects.filter(status=WebhookDelivery.Status.FAILED).count(),
    }
