"""Customer subscription activation, renewal order generation, and cancellation"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from shop.models import Cart, CartItem, CustomerSubscription
from shop.tenancy import tenant_context

from .exceptions import CommerceError

logger = logging.getLogger("shop.subscriptions")

INTERVALS = {
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=91),
    "annual": timedelta(days=365),
}


def interval_delta(interval: str) -> timedelta:
    return INTERVALS.get(interval, timedelta(days=30))


def activate_subscriptions_for_order(order) -> None:
    """On finalize, start a subscription for each subscription line (idempotent)."""
    if not order.user_id:
        return
    for item in order.items.select_related("variant"):
        variant = item.variant
        if not variant or not variant.is_subscription:
            continue
        sub = CustomerSubscription.objects.filter(
            user_id=order.user_id,
            variant=variant,
        ).first()
        renewal_at = timezone.now() + interval_delta(variant.subscription_interval)
        if sub is None:
            CustomerSubscription.objects.create(
                user_id=order.user_id,
                variant=variant,
                status=CustomerSubscription.Status.ACTIVE,
                interval=variant.subscription_interval,
                quantity=item.quantity,
                unit_price=item.unit_price,
                next_renewal_at=renewal_at,
                last_order=order,
            )
        else:
            CustomerSubscription.objects.filter(pk=sub.pk).update(
                status=CustomerSubscription.Status.ACTIVE,
                quantity=item.quantity,
                unit_price=item.unit_price,
                next_renewal_at=renewal_at,
                last_order=order,
            )


def _renewal_cart(sub: CustomerSubscription) -> Cart:
    """Dedicated scratch cart for billing — never mutates the shopper's active cart."""
    cart = Cart.objects.create(
        user=None,
        session_key=f"renewal-{sub.pk}",
        status=Cart.Status.ABANDONED,
    )
    Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.ACTIVE)
    cart.refresh_from_db()
    CartItem.objects.create(cart=cart, variant=sub.variant, quantity=sub.quantity)
    return cart


def generate_due_renewals(*, now=None) -> int:
    """Charge and fulfil subscriptions whose next renewal is due (reuses the seam)."""
    now = now or timezone.now()
    from .checkout import begin_checkout
    from .payments import authorize_payment, confirm_payment

    resolved = 0
    due = list(CustomerSubscription.objects.filter(status=CustomerSubscription.Status.ACTIVE, next_renewal_at__lte=now))
    for sub in due:
        with tenant_context(sub.tenant_id):
            key = f"renewal-{sub.pk}-{sub.next_renewal_at.date().isoformat()}"
            from shop.models import CheckoutAttempt, Order

            existing_order = Order.objects.filter(
                checkout_attempt__idempotency_key=key,
                checkout_attempt__status=CheckoutAttempt.Status.FINALIZED,
            ).first()
            if existing_order:
                CustomerSubscription.objects.filter(pk=sub.pk).update(
                    next_renewal_at=now + interval_delta(sub.interval), last_order=existing_order
                )
                resolved += 1
                continue
            cart = None
            try:
                cart = _renewal_cart(sub)
                attempt = begin_checkout(
                    cart,
                    idempotency_key=key,
                    contact={"email": sub.user.email},
                )
                if not attempt.user_id:
                    CheckoutAttempt.objects.filter(pk=attempt.pk).update(user_id=sub.user_id)
                    attempt.refresh_from_db()
                payment = authorize_payment(attempt, idempotency_key=f"{key}-pay")
                order = confirm_payment(payment, idempotency_key=f"{key}-cf")
            except CommerceError:
                if cart is not None:
                    Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.ABANDONED)
                CustomerSubscription.objects.filter(pk=sub.pk).update(status=CustomerSubscription.Status.PAST_DUE)
                continue
            except Exception:
                logger.exception("Unexpected renewal failure for subscription %s", sub.pk)
                if cart is not None:
                    Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.ABANDONED)
                CustomerSubscription.objects.filter(pk=sub.pk).update(status=CustomerSubscription.Status.PAST_DUE)
                continue
            CustomerSubscription.objects.filter(pk=sub.pk).update(
                next_renewal_at=now + interval_delta(sub.interval), last_order=order
            )
            resolved += 1
    return resolved
