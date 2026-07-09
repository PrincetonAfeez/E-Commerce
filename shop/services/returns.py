from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import F

from shop.models import (
    AuditLog,
    InventoryMovement,
    Order,
    OrderItem,
    OutboxEvent,
    ProductVariant,
    ReturnLine,
    ReturnRequest,
)

from .exceptions import CheckoutStateError
from .money import quantize_money
from .refunds import create_refund


def request_return(order: Order, *, user=None, lines: list[tuple[int, int]], reason: str = "") -> ReturnRequest:
    """Create a customer return request for specific order lines/quantities."""
    if order.status == Order.Status.CANCELLED:
        raise CheckoutStateError("This order was cancelled and cannot be returned.")
    if not order.refundable:
        raise CheckoutStateError("This order has nothing left to refund.")

    items = {item.pk: item for item in order.items.all()}
    validated: list[tuple[OrderItem, int]] = []
    for item_id, qty in lines:
        item = items.get(int(item_id))
        if item and qty and 0 < int(qty) <= item.quantity:
            validated.append((item, int(qty)))
    if not validated:
        raise CheckoutStateError("Select at least one item and quantity to return.")

    with transaction.atomic():
        rr = ReturnRequest.objects.create(order=order, user=user, reason=reason)
        for item, qty in validated:
            ReturnLine.objects.create(return_request=rr, order_item=item, quantity=qty)
        AuditLog.objects.create(
            actor=user,
            action="return.requested",
            object_type="ReturnRequest",
            object_id=str(rr.pk),
            metadata={"order": order.order_number, "lines": len(validated)},
        )
    return rr


def _return_amount(rr: ReturnRequest) -> Decimal:
    total = Decimal("0.00")
    for line in rr.lines.select_related("order_item"):
        item = line.order_item
        if item.quantity:
            unit_value = item.line_total / item.quantity
            total += quantize_money(unit_value * line.quantity)
    return quantize_money(total)


def approve_return(rr: ReturnRequest, *, actor=None, restock: bool = True, note: str = "") -> ReturnRequest:
    """Approve a return: refund the returned value and (optionally) restock those lines."""
    if rr.status not in {ReturnRequest.Status.REQUESTED, ReturnRequest.Status.RECEIVED}:
        raise CheckoutStateError("This return is not in an approvable state.")
    amount = _return_amount(rr)
    # Refund money only; restock the *specific* returned lines below (create_refund's
    # generic allocation would restock the wrong units).
    refund = create_refund(
        rr.order,
        amount=amount,
        idempotency_key=f"return-{rr.pk}",
        restock=False,
        actor=actor,
        reason=note or f"Return #{rr.pk}",
    )
    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
        if locked.status == ReturnRequest.Status.REFUNDED:
            return locked
        if restock:
            for line in locked.lines.select_related("order_item"):
                variant_id = line.order_item.variant_id
                if not variant_id:
                    continue
                ProductVariant.objects.filter(pk=variant_id).update(
                    quantity=F("quantity") + line.quantity
                )
                InventoryMovement.objects.create(
                    variant_id=variant_id,
                    quantity_delta=line.quantity,
                    reason=InventoryMovement.Reason.RETURN,
                    order=locked.order,
                    staff_user=actor,
                    note=f"Return #{locked.pk} restock",
                )
        locked.status = ReturnRequest.Status.REFUNDED
        locked.refund = refund
        locked.staff_note = note
        locked.save(update_fields=["status", "refund", "staff_note", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action="return.approved",
            object_type="ReturnRequest",
            object_id=str(locked.pk),
            metadata={"amount": str(amount), "restock": restock},
        )
        OutboxEvent.objects.create(
            event_type="order.refund_email",
            aggregate_type="Order",
            aggregate_id=str(locked.order_id),
            payload={"order_number": locked.order.order_number, "amount": str(amount)},
        )
    return rr


def reject_return(rr: ReturnRequest, *, actor=None, note: str = "") -> ReturnRequest:
    if rr.status != ReturnRequest.Status.REQUESTED:
        raise CheckoutStateError("Only requested returns can be rejected.")
    rr.status = ReturnRequest.Status.REJECTED
    rr.staff_note = note
    rr.save(update_fields=["status", "staff_note", "updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action="return.rejected",
        object_type="ReturnRequest",
        object_id=str(rr.pk),
        metadata={"note": note},
    )
    return rr
