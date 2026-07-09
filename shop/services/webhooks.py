from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request

from django.utils import timezone

from shop.models import OutboxEvent, WebhookDelivery, WebhookEndpoint

MAX_ATTEMPTS = 5


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: dict) -> int:
    """Send the webhook. Isolated so tests can monkeypatch the network call."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - merchant-configured URL
        return resp.getcode()


def enqueue_deliveries(event: OutboxEvent) -> int:
    """Create a pending WebhookDelivery per active, subscribed endpoint (idempotent).

    Endpoints are scoped to the EVENT'S tenant so an event never fans out to another
    store's webhooks (cross-tenant leak prevention)."""
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


def deliver_pending(*, limit: int = 200) -> tuple[int, int]:
    """Attempt pending/failed deliveries under the retry cap. Returns (sent, failed)."""
    sent = 0
    failed = 0
    deliveries = (
        WebhookDelivery.objects.filter(status=WebhookDelivery.Status.PENDING)
        .select_related("endpoint")
        .order_by("created_at")[:limit]
    )
    for delivery in deliveries:
        endpoint = delivery.endpoint
        body = json.dumps(
            {"event": delivery.event_type, "data": delivery.payload}, sort_keys=True
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": delivery.event_type,
            "X-Webhook-Signature": f"sha256={sign(endpoint.secret, body)}",
        }
        delivery.attempts += 1
        try:
            code = _post(endpoint.url, body, headers)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            code = getattr(exc, "code", None)
            delivery.response_code = code
            delivery.last_error = str(exc)[:2000]
            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.status = WebhookDelivery.Status.FAILED
                failed += 1
            delivery.save(
                update_fields=["attempts", "response_code", "last_error", "status", "updated_at"]
            )
            continue
        delivery.response_code = code
        if 200 <= (code or 0) < 300:
            delivery.status = WebhookDelivery.Status.SUCCESS
            delivery.last_error = ""
            sent += 1
        elif delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.last_error = f"HTTP {code}"
            failed += 1
        else:
            delivery.last_error = f"HTTP {code}"
        delivery.save(
            update_fields=["attempts", "response_code", "last_error", "status", "updated_at"]
        )
    return sent, failed


def scan_and_enqueue(*, since_minutes: int = 1440) -> int:
    """Create deliveries for recent outbox events not yet enqueued to each endpoint."""
    if not WebhookEndpoint.objects.filter(active=True).exists():
        return 0
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=since_minutes)
    total = 0
    for event in OutboxEvent.objects.filter(created_at__gte=cutoff):
        total += enqueue_deliveries(event)
    return total
