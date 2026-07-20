"""Customer return requests with staff approve/reject and linked refund creation"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum

from shop.models import (
    AuditLog,
    InventoryMovement,
    Order,
    OrderItem,
    ProductVariant,
    ReturnLine,
    ReturnRequest,
)

from .exceptions import CheckoutStateError, PermissionDenied
from .money import clamp_money, quantize_money
from .refunds import create_refund


def _aggregate_return_lines(lines: list[tuple[int, int]]) -> dict[int, int]:
    totals: dict[int, int] = defaultdict(int)
    for item_id, qty in lines:
        if qty and int(qty) > 0:
            totals[int(item_id)] += int(qty)
    return dict(totals)


def _returnable_quantity(order_item: OrderItem) -> int:
    already = (
        ReturnLine.objects.filter(
            order_item=order_item,
            return_request__status__in=[
                ReturnRequest.Status.REQUESTED,
                ReturnRequest.Status.RECEIVED,
                ReturnRequest.Status.APPROVED,
                ReturnRequest.Status.REFUNDED,
            ],
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    return max(order_item.quantity - already, 0)


def request_return(order: Order, *, user=None, lines: list[tuple[int, int]], reason: str = "") -> ReturnRequest:
    """Create a customer return request for specific order lines/quantities."""
    if order.status == Order.Status.CANCELLED:
        raise CheckoutStateError("This order was cancelled and cannot be returned.")
    if not order.refundable:
        raise CheckoutStateError("This order has nothing left to refund.")

    aggregated = _aggregate_return_lines(lines)
    if not aggregated:
        raise CheckoutStateError("Select at least one item and quantity to return.")

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        if user is not None and locked_order.user_id != user.pk:
            raise PermissionDenied("You do not have access to this order.")
        if locked_order.status == Order.Status.CANCELLED:
            raise CheckoutStateError("This order was cancelled and cannot be returned.")
        if not locked_order.refundable:
            raise CheckoutStateError("This order has nothing left to refund.")

        items = {item.pk: item for item in locked_order.items.all()}
        validated: list[tuple[OrderItem, int]] = []
        for item_id, qty in aggregated.items():
            item = items.get(item_id)
            if not item:
                raise CheckoutStateError(f"Order item {item_id} was not found on this order.")
            returnable = _returnable_quantity(item)
            if qty > returnable:
                raise CheckoutStateError(
                    f"Cannot return {qty} of item {item.sku}; only {returnable} remain returnable."
                )
            if qty > 0:
                validated.append((item, qty))
        if not validated:
            raise CheckoutStateError("Select at least one item and quantity to return.")

        rr = ReturnRequest.objects.create(order=locked_order, user=user, reason=reason, tenant=locked_order.tenant)
        for item, qty in validated:
            ReturnLine.objects.create(return_request=rr, order_item=item, quantity=qty)
        AuditLog.objects.create(
            actor=user,
            action="return.requested",
            object_type="ReturnRequest",
            object_id=str(rr.pk),
            metadata={"order": locked_order.order_number, "lines": len(validated)},
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


def _capped_return_amount(rr: ReturnRequest) -> Decimal:
    amount = _return_amount(rr)
    order = rr.order
    remaining = clamp_money(order.total - order.refund_total)
    return min(amount, remaining)


def _restock_return_lines(rr: ReturnRequest, *, actor=None) -> None:
    for line in rr.lines.select_related("order_item"):
        variant_id = line.order_item.variant_id
        if not variant_id:
            continue
        note = f"Return #{rr.pk} restock"
        if InventoryMovement.objects.filter(order=rr.order, note=note, variant_id=variant_id).exists():
            continue
        ProductVariant.objects.filter(pk=variant_id).update(quantity=F("quantity") + line.quantity)
        InventoryMovement.objects.create(
            variant_id=variant_id,
            quantity_delta=line.quantity,
            reason=InventoryMovement.Reason.RETURN,
            order=rr.order,
            staff_user=actor,
            note=note,
        )


def approve_return(rr: ReturnRequest, *, actor=None, restock: bool = True, note: str = "") -> ReturnRequest:
    """Approve a return: refund the returned value and (optionally) restock those lines."""
    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
        if locked.status == ReturnRequest.Status.REFUNDED:
            return locked
        if locked.status not in {ReturnRequest.Status.REQUESTED, ReturnRequest.Status.RECEIVED}:
            raise CheckoutStateError("This return is not in an approvable state.")
        amount = _capped_return_amount(locked)
        if amount <= 0:
            raise CheckoutStateError("This return has no refundable value remaining.")
        locked.status = ReturnRequest.Status.APPROVED
        locked.staff_note = note
        locked.save(update_fields=["status", "staff_note", "updated_at"])
        return_pk = locked.pk
        order = locked.order

    try:
        refund = create_refund(
            order,
            amount=amount,
            idempotency_key=f"return-{return_pk}",
            restock=False,
            actor=actor,
            reason=note or f"Return #{return_pk}",
            notify=True,
        )
    except Exception as exc:
        with transaction.atomic():
            locked = ReturnRequest.objects.select_for_update().get(pk=return_pk)
            if locked.status == ReturnRequest.Status.APPROVED:
                locked.status = ReturnRequest.Status.REQUESTED
                error_note = f"Refund failed: {exc}"
                locked.staff_note = f"{note}\n{error_note}".strip() if note else error_note
                locked.save(update_fields=["status", "staff_note", "updated_at"])
        raise

    try:
        with transaction.atomic():
            locked = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
            if locked.status == ReturnRequest.Status.REFUNDED:
                return locked
            locked.status = ReturnRequest.Status.REFUNDED
            locked.refund = refund
            locked.save(update_fields=["status", "refund", "updated_at"])
            if restock:
                _restock_return_lines(locked, actor=actor)
            AuditLog.objects.create(
                actor=actor,
                action="return.approved",
                object_type="ReturnRequest",
                object_id=str(locked.pk),
                metadata={"amount": str(amount), "restock": restock},
            )
    except Exception as exc:
        with transaction.atomic():
            locked = ReturnRequest.objects.select_for_update().get(pk=return_pk)
            if locked.status == ReturnRequest.Status.APPROVED:
                locked.status = ReturnRequest.Status.REQUESTED
                error_note = f"Post-refund update failed: {exc}"
                locked.staff_note = f"{note}\n{error_note}".strip() if note else error_note
                locked.save(update_fields=["status", "staff_note", "updated_at"])
        raise
    return locked


def reject_return(rr: ReturnRequest, *, actor=None, note: str = "") -> ReturnRequest:
    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=rr.pk)
        if locked.status != ReturnRequest.Status.REQUESTED:
            raise CheckoutStateError("Only requested returns can be rejected.")
        locked.status = ReturnRequest.Status.REJECTED
        locked.staff_note = note
        locked.save(update_fields=["status", "staff_note", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action="return.rejected",
            object_type="ReturnRequest",
            object_id=str(locked.pk),
            metadata={"note": note},
        )
    return locked
