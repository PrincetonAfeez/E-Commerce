# Checkout orchestration: reserve stock, snapshots, credit holds, and order finalization
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from shop.models import (
    AuditLog,
    Cart,
    CheckoutAttempt,
    CheckoutLineSnapshot,
    Fulfillment,
    Order,
    OrderItem,
    OrderStatusEvent,
    OutboxEvent,
    Payment,
)

from .cart import clear_cart, recalculate_cart, refresh_cart_coupon
from .credit import hold_credit, release_hold, spend_hold
from .exceptions import CartError, CheckoutStateError, InvalidCoupon, OutOfStock
from .inventory import consume_reservations, release_reservations, reserve_for_attempt
from .money import allocate_amount, clamp_money, quantize_money
from .pricing import effective_price
from .promotions import redeem_coupon_for_order

logger = logging.getLogger("shop.checkout")

DEFAULT_RESERVATION_WINDOW = timedelta(minutes=15)


REUSABLE_ATTEMPT_STATUSES = {
    CheckoutAttempt.Status.STARTED,
    CheckoutAttempt.Status.RESERVED,
    CheckoutAttempt.Status.PAYMENT_PENDING,
    CheckoutAttempt.Status.PAYMENT_CONFIRMED,
    CheckoutAttempt.Status.FINALIZED,
}


def begin_checkout(
    cart: Cart,
    *,
    idempotency_key: str,
    contact: dict | None = None,
    shipping: dict | None = None,
    shipping_method: str = "Standard",
    expected_subtotal: Decimal | None = None,
    use_store_credit: bool = False,
) -> CheckoutAttempt:
    existing = _reusable_attempt(cart, idempotency_key)
    if existing:
        return existing

    contact = contact or {}
    shipping = shipping or {}
    expires_at = timezone.now() + DEFAULT_RESERVATION_WINDOW

    try:
        with transaction.atomic():
            locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
            if locked_cart.status != Cart.Status.ACTIVE:
                raise CartError("Cart is not active.")
            items = list(
                locked_cart.items.select_related("variant", "variant__product").order_by("variant_id")
            )
            if not items:
                raise CartError("Cart is empty.")

            refresh_cart_coupon(locked_cart)
            totals = recalculate_cart(
                locked_cart,
                shipping_method=shipping_method,
                region=(shipping or {}).get("region", ""),
                country=(shipping or {}).get("country", "US"),
            )
            drift_message = _price_drift_message(expected_subtotal, totals.subtotal)
            price_drift_message = " ".join(part for part in [locked_cart.warning, drift_message] if part)
            attempt = CheckoutAttempt.objects.create(
                cart=locked_cart,
                user=locked_cart.user,
                session_key=locked_cart.session_key,
                idempotency_key=idempotency_key,
                status=CheckoutAttempt.Status.STARTED,
                guest_email=contact.get("email", ""),
                shipping_name=shipping.get("name", contact.get("name", "")),
                shipping_address1=shipping.get("address1", ""),
                shipping_address2=shipping.get("address2", ""),
                shipping_city=shipping.get("city", ""),
                shipping_region=shipping.get("region", ""),
                shipping_postal_code=shipping.get("postal_code", ""),
                shipping_country=shipping.get("country", "US"),
                selected_shipping_method=totals.shipping_method,
                subtotal=totals.subtotal,
                discount_total=totals.discount_total,
                shipping_total=totals.shipping_total,
                tax_total=totals.tax_total,
                total=totals.total,
                coupon_code=totals.coupon,
                expires_at=expires_at,
                price_drift_message=price_drift_message,
            )

            for item in items:
                variant = item.variant
                unit_price = effective_price(variant, locked_cart.user)
                CheckoutLineSnapshot.objects.create(
                    attempt=attempt,
                    variant=variant,
                    sku=variant.sku,
                    product_name=variant.product.name,
                    variant_title=variant.display_name(),
                    attributes=variant.attributes,
                    quantity=item.quantity,
                    unit_price=unit_price,
                    line_subtotal=quantize_money(unit_price * item.quantity),
                )

            reserve_for_attempt(attempt)
            if use_store_credit:
                # Hold store credit like a reservation; charged amount becomes amount_due.
                hold_credit(attempt, attempt.total)
            attempt.status = CheckoutAttempt.Status.RESERVED
            attempt.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                actor=locked_cart.user,
                action="checkout.started",
                object_type="CheckoutAttempt",
                object_id=str(attempt.pk),
                metadata={"cart_id": locked_cart.pk, "total": str(attempt.total)},
            )
            OutboxEvent.objects.create(
                event_type="checkout.started",
                aggregate_type="CheckoutAttempt",
                aggregate_id=str(attempt.pk),
                payload={"cart_id": locked_cart.pk, "total": str(attempt.total)},
            )
            return attempt
    except IntegrityError:
        # A concurrent request with the same (cart, idempotency_key) won the unique
        # constraint race. Return the winner's attempt instead of erroring (spec §21).
        existing = _reusable_attempt(cart, idempotency_key)
        if existing:
            return existing
        raise


def _reusable_attempt(cart: Cart, idempotency_key: str) -> CheckoutAttempt | None:
    existing = CheckoutAttempt.objects.filter(cart=cart, idempotency_key=idempotency_key).first()
    if existing and existing.status in REUSABLE_ATTEMPT_STATUSES:
        return existing
    if existing:
        raise CheckoutStateError(
            "This checkout attempt can no longer be used; start a new checkout.",
            code="checkout_attempt_terminal",
        )
    return None


def _price_drift_message(expected_subtotal: Decimal | None, actual_subtotal: Decimal) -> str:
    # Compare the item subtotal (sum of live line prices), which is independent of the
    # chosen shipping method/tax, so changing shipping never trips a false drift warning.
    if expected_subtotal is None:
        return ""
    if quantize_money(expected_subtotal) != quantize_money(actual_subtotal):
        return (
            f"Item prices changed since you last viewed your cart. The item subtotal is "
            f"now {actual_subtotal} (was {quantize_money(expected_subtotal)})."
        )
    return ""


def _order_from_attempt(attempt: CheckoutAttempt) -> Order | None:
    if attempt.order_id:
        return attempt.order
    order = Order.objects.filter(checkout_attempt=attempt).first()
    if order:
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            order=order,
            status=CheckoutAttempt.Status.FINALIZED,
        )
    return order


def _mark_compensation_required(payment_id: int, message: str) -> None:
    attempt_id = None
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        attempt = CheckoutAttempt.objects.select_for_update().get(pk=payment.checkout_attempt_id)
        attempt_id = attempt.pk
        payment.status = Payment.Status.REQUIRES_REFUND
        payment.failure_code = "stock_unavailable_after_payment"
        payment.save(update_fields=["status", "failure_code", "updated_at"])
        attempt.status = CheckoutAttempt.Status.FAILED
        attempt.save(update_fields=["status", "updated_at"])
        AuditLog.objects.create(
            actor=attempt.user,
            action="checkout.compensation_required",
            object_type="CheckoutAttempt",
            object_id=str(attempt.pk),
            metadata={"payment_id": payment.pk, "message": message},
        )
        OutboxEvent.objects.create(
            event_type="payment.auto_refund_required",
            aggregate_type="Payment",
            aggregate_id=str(payment.pk),
            payload={"checkout_attempt_id": attempt.pk, "message": message},
        )
    if attempt_id:
        released_attempt = CheckoutAttempt.objects.get(pk=attempt_id)
        release_reservations(released_attempt)
        release_hold(released_attempt)


def finalize_confirmed_payment(payment: Payment) -> Order:
    try:
        return _finalize_confirmed_payment(payment)
    except (OutOfStock, InvalidCoupon) as exc:
        _mark_compensation_required(payment.pk, exc.message)
        raise CheckoutStateError(
            "Payment is confirmed, but finalization failed. An automatic refund is required.",
            code="compensation_required",
        ) from exc


def _finalize_confirmed_payment(payment: Payment) -> Order:
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
        # Lock only the attempt row itself (of=("self",)); select_related here pulls in
        # nullable FKs (coupon_code, user) whose outer joins PostgreSQL refuses to lock.
        attempt = (
            CheckoutAttempt.objects.select_for_update(of=("self",))
            .select_related("cart", "coupon_code", "user")
            .get(pk=locked_payment.checkout_attempt_id)
        )

        existing_order = _order_from_attempt(attempt)
        if existing_order:
            if not locked_payment.order_id:
                locked_payment.order = existing_order
                locked_payment.status = Payment.Status.CONFIRMED
                locked_payment.save(update_fields=["order", "status", "updated_at"])
            return existing_order

        if locked_payment.status != Payment.Status.CONFIRMED:
            raise CheckoutStateError("Payment must be confirmed before finalization.")
        if attempt.status not in {
            CheckoutAttempt.Status.PAYMENT_CONFIRMED,
            CheckoutAttempt.Status.PAYMENT_PENDING,
        }:
            raise CheckoutStateError("Checkout attempt is not confirmable.")

        order = Order.objects.create(
            user=attempt.user,
            checkout_attempt=attempt,
            guest_email=attempt.guest_email,
            subtotal=attempt.subtotal,
            discount_total=attempt.discount_total,
            shipping_total=attempt.shipping_total,
            tax_total=attempt.tax_total,
            total=attempt.total,
            currency=attempt.currency,
            coupon_code=attempt.coupon_code,
            shipping_name=attempt.shipping_name,
            shipping_address1=attempt.shipping_address1,
            shipping_address2=attempt.shipping_address2,
            shipping_city=attempt.shipping_city,
            shipping_region=attempt.shipping_region,
            shipping_postal_code=attempt.shipping_postal_code,
            shipping_country=attempt.shipping_country,
            selected_shipping_method=attempt.selected_shipping_method,
        )

        lines = list(attempt.line_snapshots.select_related("variant").order_by("id"))
        weights = [line.line_subtotal for line in lines]
        discount_allocations = allocate_amount(attempt.discount_total, weights)
        tax_allocations = allocate_amount(attempt.tax_total, weights)
        shipping_allocations = allocate_amount(attempt.shipping_total, weights)

        for line, line_discount, line_tax, line_shipping in zip(
            lines, discount_allocations, tax_allocations, shipping_allocations, strict=True
        ):
            line_total = clamp_money(line.line_subtotal - line_discount + line_tax + line_shipping)
            OrderItem.objects.create(
                order=order,
                variant=line.variant,
                sku=line.sku,
                product_name=line.product_name,
                variant_title=line.variant_title,
                attributes=line.attributes,
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount_total=line_discount,
                tax_total=line_tax,
                shipping_total=line_shipping,
                line_total=line_total,
            )

        redeem_coupon_for_order(order)
        consume_reservations(attempt, order)
        spend_hold(attempt, order)
        from .subscriptions import activate_subscriptions_for_order

        activate_subscriptions_for_order(order)

        locked_payment.order = order
        locked_payment.status = Payment.Status.CONFIRMED
        locked_payment.save(update_fields=["order", "status", "updated_at"])

        attempt.status = CheckoutAttempt.Status.FINALIZED
        attempt.order = order
        attempt.save(update_fields=["status", "order", "updated_at"])
        clear_cart(attempt.cart)

        Fulfillment.objects.create(order=order)
        logger.info(
            "order.finalized",
            extra={"event": "order.finalized", "order_number": order.order_number,
                   "checkout_attempt_id": attempt.pk, "payment_id": locked_payment.pk},
        )
        OrderStatusEvent.objects.create(
            order=order,
            event_type="order.created",
            from_status="",
            to_status=order.status,
            actor=attempt.user,
            note="Order finalized from confirmed payment.",
        )
        AuditLog.objects.create(
            actor=attempt.user,
            action="order.created",
            object_type="Order",
            object_id=str(order.pk),
            metadata={"checkout_attempt_id": attempt.pk, "payment_id": locked_payment.pk},
        )
        OutboxEvent.objects.create(
            event_type="order.confirmation_email",
            aggregate_type="Order",
            aggregate_id=str(order.pk),
            payload={"order_number": order.order_number, "email": order.guest_email},
        )
        return order


def replay_finalization(attempt: CheckoutAttempt) -> Order:
    payment = attempt.payments.filter(status=Payment.Status.CONFIRMED).order_by("-created_at").first()
    if not payment:
        raise CheckoutStateError("No confirmed payment exists for this checkout attempt.")
    return finalize_confirmed_payment(payment)
