from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import F

from shop.models import (
    AuditLog,
    InventoryMovement,
    Order,
    OrderItem,
    OrderStatusEvent,
    OutboxEvent,
    Payment,
    ProductVariant,
    Refund,
    RefundLine,
)

from .exceptions import CheckoutStateError
from .money import allocate_amount, clamp_money, quantize_money
from .payments import gateway
from .promotions import release_redemptions_for_order


def create_refund(
    order: Order,
    *,
    amount: Decimal,
    idempotency_key: str,
    restock: bool = False,
    actor=None,
    reason: str = "",
) -> Refund:
    amount = quantize_money(amount)
    # Replay check first, independent of current payment status (a fully refunded
    # payment is no longer CONFIRMED but its refund must still replay idempotently).
    existing = Refund.objects.filter(order=order, idempotency_key=idempotency_key).first()
    if existing:
        if existing.status in {Refund.Status.SUCCEEDED, Refund.Status.FAILED}:
            return existing
        # PENDING: a prior attempt died between the gateway call and finalization.
        # Resume it (the gateway call is idempotent by key; finalize is guarded).
        return _run_gateway_and_finalize(existing, actor=actor)

    payment = order.payments.filter(
        status__in=[Payment.Status.CONFIRMED, Payment.Status.PARTIALLY_REFUNDED]
    ).order_by("-created_at").first()
    if not payment:
        raise CheckoutStateError("Order has no refundable payment.")

    refundable = clamp_money(order.total - order.refund_total)
    if amount <= 0 or amount > refundable:
        raise CheckoutStateError("Refund amount is not refundable.")

    # Reserve the idempotency slot BEFORE touching the gateway so a concurrent
    # same-key request cannot double-refund at the provider (spec §19a.4).
    try:
        refund = Refund.objects.create(
            order=order,
            payment=payment,
            idempotency_key=idempotency_key,
            amount=amount,
            status=Refund.Status.PENDING,
            restock=restock,
            reason=reason,
        )
    except IntegrityError:
        existing = Refund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
        if existing and existing.status in {Refund.Status.SUCCEEDED, Refund.Status.FAILED}:
            return existing
        if existing:
            return _run_gateway_and_finalize(existing, actor=actor)
        raise

    return _run_gateway_and_finalize(refund, actor=actor)


def _run_gateway_and_finalize(refund: Refund, *, actor=None) -> Refund:
    payment = refund.payment
    order = refund.order
    order.refresh_from_db()
    # Split the refund: the gateway can only return what was actually charged
    # (total minus store credit applied); any remainder goes back to store credit.
    amount_charged = clamp_money(order.total - order.credit_applied)
    gateway_already = min(order.refund_total, amount_charged)
    gateway_room = clamp_money(amount_charged - gateway_already)
    gateway_amount = min(refund.amount, gateway_room)
    credit_amount = quantize_money(refund.amount - gateway_amount)

    # On a gateway error, leave the refund PENDING (not FAILED) so a same-key retry can
    # resume it — the gateway refund is idempotent by key, so no double-refund. This
    # avoids permanently bricking a refund on a transient provider blip.
    if gateway_amount > 0:
        gateway.refund(
            gateway_reference=payment.gateway_reference,
            amount=gateway_amount,
            currency=payment.currency,
            idempotency_key=refund.idempotency_key,
        )

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=refund.order_id)
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
        refund = Refund.objects.select_for_update().get(pk=refund.pk)
        # Idempotent guard: if a concurrent/previous run already finalized this refund,
        # do not restock or bump refund_total a second time.
        if refund.status == Refund.Status.SUCCEEDED:
            return refund
        amount = refund.amount
        restock = refund.restock
        reason = refund.reason
        refund.status = Refund.Status.SUCCEEDED
        refund.save(update_fields=["status", "updated_at"])

        order_items = list(locked_order.items.select_related("variant").order_by("id"))
        allocations = allocate_amount(amount, [item.line_total for item in order_items])
        payload_lines = []
        for item, line_amount in zip(order_items, allocations, strict=True):
            if line_amount <= 0:
                continue
            quantity = _quantity_for_refund_line(item, line_amount)
            RefundLine.objects.create(
                refund=refund,
                order_item=item,
                quantity=quantity,
                amount=line_amount,
                restocked=restock and bool(item.variant_id),
            )
            payload_lines.append(
                {
                    "sku": item.sku,
                    "quantity": quantity,
                    "amount": str(line_amount),
                    "discount": str(item.discount_total),
                    "tax": str(item.tax_total),
                    "shipping": str(item.shipping_total),
                }
            )
            if restock and item.variant_id:
                ProductVariant.objects.filter(pk=item.variant_id).update(quantity=F("quantity") + quantity)
                InventoryMovement.objects.create(
                    variant=item.variant,
                    quantity_delta=quantity,
                    reason=InventoryMovement.Reason.RETURN,
                    order=locked_order,
                    staff_user=actor,
                    note=f"Refund {refund.pk} restock",
                )

        refund.allocation_payload = {"lines": payload_lines}
        refund.save(update_fields=["allocation_payload", "updated_at"])

        locked_order.refund_total = quantize_money(locked_order.refund_total + amount)
        full_refund = locked_order.refund_total >= locked_order.total
        if full_refund:
            previous = locked_order.status
            locked_order.status = Order.Status.REFUNDED
            locked_payment.status = Payment.Status.REFUNDED
            release_redemptions_for_order(locked_order)
            OrderStatusEvent.objects.create(
                order=locked_order,
                event_type="order.refunded",
                from_status=previous,
                to_status=locked_order.status,
                actor=actor,
                note=reason,
            )
        else:
            locked_payment.status = Payment.Status.PARTIALLY_REFUNDED

        locked_order.save(update_fields=["refund_total", "status", "updated_at"])
        locked_payment.save(update_fields=["status", "updated_at"])
        # The portion beyond what the gateway was charged is returned as store credit.
        if credit_amount > 0 and locked_order.user_id:
            from .credit import refund_to_credit

            refund_to_credit(locked_order.user_id, credit_amount, locked_order)
        AuditLog.objects.create(
            actor=actor,
            action="refund.created",
            object_type="Refund",
            object_id=str(refund.pk),
            metadata={
                "order": locked_order.order_number,
                "amount": str(amount),
                "restock": restock,
                "to_credit": str(credit_amount),
            },
        )
        OutboxEvent.objects.create(
            event_type="order.refund_email",
            aggregate_type="Order",
            aggregate_id=str(locked_order.pk),
            payload={"order_number": locked_order.order_number, "amount": str(amount)},
        )
        return refund


def _quantity_for_refund_line(item: OrderItem, line_amount: Decimal) -> int:
    if line_amount >= item.line_total:
        return item.quantity
    unit_share = item.line_total / item.quantity
    quantity = int((line_amount / unit_share).to_integral_value(rounding=ROUND_HALF_UP))
    return max(min(quantity, item.quantity), 1)
