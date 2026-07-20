"""Transactional email rendering and delivery logged via EmailDelivery records"""
from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
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
    from shop.tenancy import tenant_context

    subject, template = spec
    with tenant_context(event.tenant_id):
        return _deliver_outbox_event_inner(event, subject=subject, template=template)


def _deliver_outbox_event_inner(event, *, subject: str, template: str) -> EmailDelivery | None:
    payload = event.payload or {}

    order = None
    recipient = payload.get("email", "")
    context = {"payload": payload, "site_url": site_url()}
    # Marketing emails carry a one-click unsubscribe link (signed email token).
    if event.event_type == "cart.recovery_email" and recipient:
        from django.core import signing

        from shop.tenancy import get_current_tenant_id

        token = signing.dumps(
            {"email": recipient, "tenant_id": get_current_tenant_id()},
            salt="unsubscribe",
        )
        context["unsubscribe_url"] = f"{site_url()}{reverse('unsubscribe', args=[token])}"
    if event.aggregate_type == "Order":
        order = Order.objects.filter(pk=event.aggregate_id).prefetch_related("items").first()
        if order:
            recipient = order.guest_email or (order.user.email if order.user_id else "")
            context.update(_order_context(order))

    with transaction.atomic():
        delivery, _created = EmailDelivery.objects.get_or_create(
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
        delivery = EmailDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status == EmailDelivery.Status.SENT:
            return delivery
        delivery.status = EmailDelivery.Status.SENDING
        delivery.save(update_fields=["status", "updated_at"])

    if not recipient:
        with transaction.atomic():
            delivery = EmailDelivery.objects.select_for_update().get(pk=delivery.pk)
            delivery.status = EmailDelivery.Status.FAILED
            delivery.error = "No recipient email on record."
            delivery.save(update_fields=["status", "error", "updated_at"])
        return delivery

    html = render_to_string(f"shop/email/{template}.html", context)
    text = strip_tags(html)
    message = EmailMultiAlternatives(subject=subject, body=text, to=[recipient])
    message.attach_alternative(html, "text/html")
    delivery.refresh_from_db()
    send_nonce = f"event:{event.pk}"
    if delivery.error == f"sent:{send_nonce}":
        with transaction.atomic():
            locked = EmailDelivery.objects.select_for_update().get(pk=delivery.pk)
            if locked.status != EmailDelivery.Status.SENT:
                locked.status = EmailDelivery.Status.SENT
                locked.sent_at = timezone.now()
                locked.error = ""
                locked.save(update_fields=["status", "sent_at", "error", "updated_at"])
        return delivery
    with transaction.atomic():
        delivery = EmailDelivery.objects.select_for_update().get(pk=delivery.pk)
        delivery.error = f"sent:{send_nonce}"
        delivery.save(update_fields=["error", "updated_at"])
    try:
        message.send()
    except Exception:
        with transaction.atomic():
            delivery = EmailDelivery.objects.select_for_update().get(pk=delivery.pk)
            delivery.error = f"failed:{send_nonce}"
            delivery.status = EmailDelivery.Status.QUEUED
            delivery.save(update_fields=["error", "status", "updated_at"])
        raise

    with transaction.atomic():
        delivery = EmailDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status == EmailDelivery.Status.SENT:
            return delivery
        delivery.status = EmailDelivery.Status.SENT
        delivery.sent_at = timezone.now()
        delivery.error = f"sent:{send_nonce}"
        delivery.save(update_fields=["status", "sent_at", "error", "updated_at"])
    return delivery


def _hash_email(value: str) -> str:
    return hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()
