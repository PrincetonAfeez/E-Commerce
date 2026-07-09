# Transactional email rendering and delivery logged via EmailDelivery records
from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import strip_tags

from shop.models import EmailDelivery, Order

# event_type -> (subject, template). Templates live in shop/email/.
EMAIL_EVENTS = {
    "order.confirmation_email": ("Your Aster Commerce order is confirmed", "order_confirmation"),
    "order.shipped_email": ("Your order has shipped", "order_shipped"),
    "order.delivered_email": ("Your order was delivered", "order_delivered"),
    "order.cancelled_email": ("Your order was cancelled", "order_cancelled"),
    "order.refund_email": ("A refund was issued for your order", "order_refunded"),
    "payment.failed_email": ("There was a problem with your payment", "payment_failed"),
    "cart.recovery_email": ("You left items in your cart", "cart_recovery"),
}


def site_url() -> str:
    # Prefer the active tenant's own domain so links point at the right storefront.
    from shop.models import Tenant
    from shop.tenancy import get_current_tenant_id

    tid = get_current_tenant_id()
    if tid is not None:
        tenant = Tenant.objects.filter(pk=tid).first()
        if tenant and tenant.primary_domain:
            scheme = "https" if getattr(settings, "IS_PRODUCTION", False) else "http"
            return f"{scheme}://{tenant.primary_domain}"
    return getattr(settings, "SITE_URL", "http://localhost:8000").rstrip("/")


def order_link(order: Order) -> str:
    path = reverse("orders:detail", args=[order.order_number])
    if not order.user_id:
        return f"{site_url()}{path}?token={order.order_token}"
    return f"{site_url()}{path}"


def _order_context(order: Order) -> dict:
    return {
        "order": order,
        "items": list(order.items.all()),
        "order_link": order_link(order),
        "site_url": site_url(),
    }


def deliver_outbox_event(event) -> EmailDelivery | None:
    """Render and actually send the transactional email for an outbox event.

    Returns the EmailDelivery record (idempotent per event), or None if the event
    is not an email event. Raises on send failure so the outbox can retry (§17.2).
    """
    spec = EMAIL_EVENTS.get(event.event_type)
    if spec is None:
        return None
    from shop.tenancy import set_current_tenant

    # Scope to the event's store so settings/links resolve to the right storefront.
    set_current_tenant(event.tenant_id)
    subject, template = spec
    payload = event.payload or {}

    order = None
    recipient = payload.get("email", "")
    context = {"payload": payload, "site_url": site_url()}
    # Marketing emails carry a one-click unsubscribe link (signed email token).
    if event.event_type == "cart.recovery_email" and recipient:
        from django.core import signing

        token = signing.dumps(recipient, salt="unsubscribe")
        context["unsubscribe_url"] = f"{site_url()}{reverse('unsubscribe', args=[token])}"
    if event.aggregate_type == "Order":
        order = Order.objects.filter(pk=event.aggregate_id).prefetch_related("items").first()
        if order:
            recipient = order.guest_email or (order.user.email if order.user_id else "")
            context.update(_order_context(order))

    # Idempotent: one delivery row per (outbox_event, template).
    delivery, created = EmailDelivery.objects.get_or_create(
        outbox_event=event,
        template=event.event_type,
        defaults={
            "order": order,
            "to_email_hash": _hash_email(recipient),
            "status": EmailDelivery.Status.QUEUED,
        },
    )
    if delivery.status == EmailDelivery.Status.SENT:
        return delivery

    if not recipient:
        delivery.status = EmailDelivery.Status.FAILED
        delivery.error = "No recipient email on record."
        delivery.save(update_fields=["status", "error", "updated_at"])
        return delivery

    html = render_to_string(f"shop/email/{template}.html", context)
    text = strip_tags(html)
    message = EmailMultiAlternatives(subject=subject, body=text, to=[recipient])
    message.attach_alternative(html, "text/html")
    message.send()  # raises on hard failure -> outbox retries

    from django.utils import timezone

    delivery.status = EmailDelivery.Status.SENT
    delivery.sent_at = timezone.now()
    delivery.error = ""
    delivery.save(update_fields=["status", "sent_at", "error", "updated_at"])
    return delivery


def _hash_email(value: str) -> str:
    return hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()
