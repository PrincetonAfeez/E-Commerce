from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from shop.models import Cart, CartItem, CustomerSubscription
from shop.tenancy import tenant_context

from .exceptions import CommerceError

INTERVALS = {
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=91),
    "annual": timedelta(days=365),
}


def interval_delta(interval: str) -> timedelta:
    return INTERVALS.get(interval, timedelta(days=30))


def activate_subscriptions_for_order(order) -> None:
    """On finalize, start a subscription for each subscription line (idempotent).

    Renewal orders also pass through here; get_or_create on the active (user, variant)
    finds the existing subscription, so no duplicate is created."""
    if not order.user_id:
        return
    for item in order.items.select_related("variant"):
        variant = item.variant
        if not variant or not variant.is_subscription:
            continue
        CustomerSubscription.objects.get_or_create(
            user_id=order.user_id,
            variant=variant,
            status=CustomerSubscription.Status.ACTIVE,
            defaults={
                "interval": variant.subscription_interval,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "next_renewal_at": timezone.now() + interval_delta(variant.subscription_interval),
                "last_order": order,
            },
        )


def generate_due_renewals(*, now=None) -> int:
    """Charge and fulfil subscriptions whose next renewal is due (reuses the seam)."""
    now = now or timezone.now()
    from .checkout import begin_checkout
    from .payments import authorize_payment, confirm_payment

    resolved = 0
    due = list(
        CustomerSubscription.objects.filter(
            status=CustomerSubscription.Status.ACTIVE, next_renewal_at__lte=now
        )
    )
    for sub in due:
        with tenant_context(sub.tenant_id):
            key = f"renewal-{sub.pk}-{sub.next_renewal_at.date().isoformat()}"
            try:
                cart = Cart.objects.create(user_id=sub.user_id)
            except IntegrityError:
                # The customer currently has a live cart; retry on the next run.
                continue
            try:
                CartItem.objects.create(cart=cart, variant=sub.variant, quantity=sub.quantity)
                attempt = begin_checkout(cart, idempotency_key=key)
                payment = authorize_payment(attempt, idempotency_key=f"{key}-pay")
                order = confirm_payment(payment, idempotency_key=f"{key}-cf")
            except CommerceError:
                # Stock/payment failure — flag for the merchant, free the cart slot.
                Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.ABANDONED)
                CustomerSubscription.objects.filter(pk=sub.pk).update(
                    status=CustomerSubscription.Status.PAST_DUE
                )
                continue
            CustomerSubscription.objects.filter(pk=sub.pk).update(
                next_renewal_at=now + interval_delta(sub.interval), last_order=order
            )
            resolved += 1
    return resolved
