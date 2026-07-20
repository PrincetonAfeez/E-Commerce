"""Order fulfillment transitions, cancellation, and status event outbox publishing"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from shop.models import AuditLog, Fulfillment, Order, OrderStatusEvent, OutboxEvent, Payment

from .exceptions import CheckoutStateError
from .money import clamp_money

FULFILLMENT_TRANSITIONS = {
    Fulfillment.Status.UNFULFILLED: {Fulfillment.Status.PROCESSING},
    Fulfillment.Status.PROCESSING: {Fulfillment.Status.SHIPPED},
    Fulfillment.Status.SHIPPED: {Fulfillment.Status.DELIVERED},
    Fulfillment.Status.DELIVERED: set(),
}


def transition_fulfillment(
    order: Order,
    *,
    target_status: str,
    actor=None,
    carrier: str = "",
    tracking_number: str = "",
    note: str = "",
) -> Fulfillment:
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        if locked_order.status in {Order.Status.CANCELLED, Order.Status.REFUNDED}:
            raise CheckoutStateError(f"Cannot fulfill a {locked_order.status} order.", code="checkout_state_error")
        fulfillment = Fulfillment.objects.select_for_update().get(order=locked_order)
        allowed = FULFILLMENT_TRANSITIONS.get(fulfillment.status, set())
        if target_status not in allowed:
            raise CheckoutStateError(f"Cannot transition fulfillment from {fulfillment.status} to {target_status}.")
        previous = fulfillment.status
        fulfillment.status = target_status
        if carrier:
            fulfillment.carrier = carrier
        if tracking_number:
            fulfillment.tracking_number = tracking_number
        if target_status == Fulfillment.Status.SHIPPED:
            fulfillment.shipped_at = timezone.now()
        if target_status == Fulfillment.Status.DELIVERED:
            fulfillment.delivered_at = timezone.now()
        fulfillment.save()
        OrderStatusEvent.objects.create(
            order=locked_order,
            event_type="fulfillment.transitioned",
            from_status=previous,
            to_status=target_status,
            actor=actor,
            note=note,
        )
        AuditLog.objects.create(
            actor=actor,
            action="fulfillment.transitioned",
            object_type="Order",
            object_id=str(locked_order.pk),
            metadata={"from": previous, "to": target_status},
        )
        if target_status == Fulfillment.Status.SHIPPED:
            OutboxEvent.objects.create(
                event_type="order.shipped_email",
                aggregate_type="Order",
                aggregate_id=str(locked_order.pk),
                payload={"order_number": locked_order.order_number, "tracking": fulfillment.tracking_number},
            )
        if target_status == Fulfillment.Status.DELIVERED:
            OutboxEvent.objects.create(
                event_type="order.delivered_email",
                aggregate_type="Order",
                aggregate_id=str(locked_order.pk),
                payload={"order_number": locked_order.order_number},
            )
        return fulfillment


def cancel_order(order: Order, *, actor=None, note: str = "", restock: bool | None = None) -> Order:
    from .refunds import create_refund

    order_pk = order.pk
    if restock is None:
        fulfillment = Fulfillment.objects.filter(order_id=order_pk).first()
        restock = fulfillment is not None and fulfillment.status == Fulfillment.Status.UNFULFILLED

    refund_plan = None
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order_pk)
        fulfillment = Fulfillment.objects.select_for_update().get(order=locked_order)
        if fulfillment.status in {Fulfillment.Status.SHIPPED, Fulfillment.Status.DELIVERED}:
            raise CheckoutStateError("Shipped orders cannot be cancelled here; create a refund instead.")
        if locked_order.status in {Order.Status.CANCELLED, Order.Status.REFUNDED}:
            return locked_order

        refundable = clamp_money(locked_order.total - locked_order.refund_total)
        has_payment = locked_order.payments.filter(
            status__in=[Payment.Status.CONFIRMED, Payment.Status.PARTIALLY_REFUNDED]
        ).exists()
        if has_payment and refundable > 0:
            refund_plan = {
                "order": locked_order,
                "amount": refundable,
                "idempotency_key": f"{locked_order.order_number}-cancel",
                "restock": restock,
                "reason": note or "Order cancelled",
            }

        previous = locked_order.status
        locked_order.status = Order.Status.CANCELLED
        locked_order.save(update_fields=["status", "updated_at"])
        OrderStatusEvent.objects.create(
            order=locked_order,
            event_type="order.cancelled",
            from_status=previous,
            to_status=locked_order.status,
            actor=actor,
            note=note,
        )
        AuditLog.objects.create(
            actor=actor,
            action="order.cancelled",
            object_type="Order",
            object_id=str(locked_order.pk),
            metadata={"note": note, "restocked": restock},
        )
        OutboxEvent.objects.create(
            event_type="order.cancelled_email",
            aggregate_type="Order",
            aggregate_id=str(locked_order.pk),
            payload={"order_number": locked_order.order_number},
        )
        cancelled_order = locked_order

    if refund_plan:
        create_refund(
            refund_plan["order"],
            amount=refund_plan["amount"],
            idempotency_key=refund_plan["idempotency_key"],
            restock=refund_plan["restock"],
            actor=actor,
            reason=refund_plan["reason"],
        )
        # Cancellation is the terminal state; do not leave the order as refunded.
        Order.objects.filter(pk=cancelled_order.pk).update(status=Order.Status.CANCELLED)
        cancelled_order.refresh_from_db()
    return cancelled_order
