"""HMAC-signed webhook enqueue and delivery with retry for outbox domain events"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request

from django.db import transaction
from django.utils import timezone

from shop.models import OutboxEvent, WebhookDelivery, WebhookEndpoint

MAX_ATTEMPTS = 5


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return resp.getcode()


def enqueue_deliveries(event: OutboxEvent) -> int:
    from shop.tenancy import tenant_context

    created = 0
    with tenant_context(event.tenant_id):
        endpoints = list(WebhookEndpoint.objects.filter(active=True))
    for endpoint in endpoints:
        if not endpoint.subscribes_to(event.event_type):
            continue
        _, was_created = WebhookDelivery.objects.get_or_create(
            endpoint=endpoint,
            outbox_event=event,
            defaults={"event_type": event.event_type, "payload": event.payload or {}},
        )
        created += int(was_created)
    return created


def _claim_delivery(delivery_id: int):
    """Lock a pending delivery and bump its attempt counter (no HTTP in this transaction)."""
    with transaction.atomic():
        delivery = (
            WebhookDelivery.objects.select_for_update(skip_locked=True)
            .select_related("endpoint")
            .filter(pk=delivery_id, status=WebhookDelivery.Status.PENDING)
            .first()
        )
        if delivery is None:
            return None
        endpoint = delivery.endpoint
        body = json.dumps({"event": delivery.event_type, "data": delivery.payload}, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": delivery.event_type,
            "X-Webhook-Signature": f"sha256={sign(endpoint.secret, body)}",
        }
        delivery.attempts += 1
        delivery.save(update_fields=["attempts", "updated_at"])
        return delivery.pk, endpoint.url, body, headers, delivery.attempts


def _finalize_delivery(delivery_id: int, *, code: int | None, error: str, attempts: int) -> str:
    """Persist HTTP outcome in a short transaction after the outbound call."""
    with transaction.atomic():
        delivery = WebhookDelivery.objects.select_for_update().get(pk=delivery_id)
        delivery.response_code = code
        delivery.last_error = error[:2000]
        if 200 <= (code or 0) < 300:
            delivery.status = WebhookDelivery.Status.SUCCESS
            delivery.last_error = ""
            outcome = "sent"
        elif attempts >= MAX_ATTEMPTS:
            delivery.status = WebhookDelivery.Status.FAILED
            outcome = "failed"
        else:
            outcome = "retry"
        delivery.save(update_fields=["response_code", "last_error", "status", "updated_at"])
        return outcome


def deliver_pending(*, limit: int = 200) -> tuple[int, int]:
    sent = 0
    failed = 0
    delivery_ids = list(
        WebhookDelivery.objects.filter(status=WebhookDelivery.Status.PENDING)
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
    for delivery_id in delivery_ids:
        claimed = _claim_delivery(delivery_id)
        if claimed is None:
            continue
        pk, url, body, headers, attempts = claimed
        try:
            code = _post(url, body, headers)
            error = ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            code = getattr(exc, "code", None)
            error = str(exc)
        outcome = _finalize_delivery(pk, code=code, error=error or f"HTTP {code}", attempts=attempts)
        if outcome == "sent":
            sent += 1
        elif outcome == "failed":
            failed += 1
    return sent, failed


def scan_and_enqueue(*, since_minutes: int = 1440, limit: int = 500) -> int:
    if not WebhookEndpoint.objects.filter(active=True).exists():
        return 0
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=since_minutes)
    total = 0
    last_id = 0
    while True:
        batch = list(OutboxEvent.objects.filter(created_at__gte=cutoff, pk__gt=last_id).order_by("pk")[:limit])
        if not batch:
            break
        for event in batch:
            total += enqueue_deliveries(event)
        last_id = batch[-1].pk
        if len(batch) < limit:
            break
    return total
