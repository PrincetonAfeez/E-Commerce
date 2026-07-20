"""Partial or full refunds with idempotency, optional restock, and gateway reversal"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Sum

from shop.models import (
    AuditLog,
    InventoryMovement,
    Order,
    OrderItem,
    OrderStatusEvent,
    OutboxEvent,
    Payment,
    PaymentEvent,
    ProductVariant,
    Refund,
    RefundLine,
)

from .exceptions import CheckoutStateError
from .gateway import CONFIRMED, get_payment_gateway
from .money import allocate_amount, clamp_money, quantize_money
from .promotions import release_redemptions_for_order


def process_compensation_refund(payment_id: int) -> Refund | None:
    """Refund a confirmed payment after checkout finalization failed (auto-compensation)."""
    payment = Payment.objects.select_related("checkout_attempt").get(pk=payment_id)
    idempotency_key = f"compensation-{payment_id}"
    order = Order.objects.filter(checkout_attempt=payment.checkout_attempt).first()
    if order:
        return create_refund(
            order,
            amount=payment.amount,
            idempotency_key=idempotency_key,
            reason="Automatic compensation refund",
        )
    return _compensation_refund_without_order(payment, idempotency_key=idempotency_key)


def _compensation_refund_without_order(payment: Payment, *, idempotency_key: str) -> None:
    """Gateway-only compensation when finalization never created an order."""
    payment.refresh_from_db()
    if payment.status == Payment.Status.REFUNDED:
        return None
    if not payment.gateway_reference:
        raise CheckoutStateError("Payment has no gateway reference for compensation refund.")

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == Payment.Status.REFUNDED:
            return None
        existing_event = PaymentEvent.objects.filter(
            payment=locked,
            event_type="payment.compensation_refund",
            payload__idempotency_key=idempotency_key,
        ).first()
        if existing_event and existing_event.processing_result == "compensation-refunded":
            return None
        if not existing_event:
            PaymentEvent.objects.create(
                payment=locked,
                checkout_attempt=locked.checkout_attempt,
                gateway_reference=locked.gateway_reference or "",
                event_type="payment.compensation_refund",
                payload={"idempotency_key": idempotency_key},
                status="processing",
                processing_result="processing",
            )

    try:
        gw = get_payment_gateway()
        gw.refund(
            gateway_reference=payment.gateway_reference,
            amount=payment.amount,
            currency=payment.currency,
            idempotency_key=idempotency_key,
            tenant_scope=str(payment.tenant_id),
        )
    except Exception:
        PaymentEvent.objects.filter(
            payment_id=payment.pk,
            event_type="payment.compensation_refund",
            payload__idempotency_key=idempotency_key,
        ).update(processing_result="gateway-failed")
        raise

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == Payment.Status.REFUNDED:
            return None
        locked.status = Payment.Status.REFUNDED
        locked.save(update_fields=["status", "updated_at"])
        PaymentEvent.objects.filter(
            payment=locked,
            event_type="payment.compensation_refund",
            payload__idempotency_key=idempotency_key,
        ).update(
            status=CONFIRMED,
            processing_result="compensation-refunded",
        )
    return None


def create_refund(
    order: Order,
    *,
    amount: Decimal,
    idempotency_key: str,
    restock: bool = False,
    actor=None,
    reason: str = "",
    notify: bool = True,
) -> Refund:
    amount = quantize_money(amount)
    payment_for_lookup = order.payments.order_by("-created_at").first()
    if payment_for_lookup:
        existing = Refund.objects.filter(payment=payment_for_lookup, idempotency_key=idempotency_key).first()
        if existing:
            if existing.status in {Refund.Status.SUCCEEDED, Refund.Status.FAILED}:
                return existing
            return _run_gateway_and_finalize(existing, actor=actor, notify=notify)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        payment = (
            locked_order.payments.filter(status__in=[Payment.Status.CONFIRMED, Payment.Status.PARTIALLY_REFUNDED])
            .order_by("-created_at")
            .first()
        )
        if payment:
            existing = Refund.objects.filter(payment=payment, idempotency_key=idempotency_key).first()
            if existing:
                if existing.status in {Refund.Status.SUCCEEDED, Refund.Status.FAILED}:
                    return existing
                refund = existing
            else:
                pending_total = Refund.objects.filter(order=locked_order, status=Refund.Status.PENDING).aggregate(
                    total=Sum("amount")
                )["total"] or Decimal("0.00")
                refundable = clamp_money(locked_order.total - locked_order.refund_total - pending_total)
                if amount <= 0 or amount > refundable:
                    raise CheckoutStateError("Refund amount is not refundable.")
                try:
                    refund = Refund.objects.create(
                        order=locked_order,
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
                        refund = existing
                    else:
                        raise
        else:
            raise CheckoutStateError("Order has no refundable payment.")

    return _run_gateway_and_finalize(refund, actor=actor, notify=notify)


_CLAIM_SUCCEEDED = "SUCCEEDED"


def _claim_refund_for_gateway(refund_id: int) -> tuple[int, Decimal, Decimal, Decimal] | str | None:
    """Lock a pending refund and compute gateway/credit split (no HTTP in this transaction)."""
    with transaction.atomic():
        refund = (
            Refund.objects.select_for_update()
            .select_related("order", "payment")
            .filter(pk=refund_id, status=Refund.Status.PENDING)
            .first()
        )
        if refund is None:
            if Refund.objects.filter(pk=refund_id, status=Refund.Status.SUCCEEDED).exists():
                return _CLAIM_SUCCEEDED
            return None
        locked_order = Order.objects.select_for_update().get(pk=refund.order_id)
        Payment.objects.select_for_update().get(pk=refund.payment_id)

        pending_others = Refund.objects.filter(order=locked_order, status=Refund.Status.PENDING).exclude(
            pk=refund.pk
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        refundable = clamp_money(locked_order.total - locked_order.refund_total - pending_others)
        if refund.amount > refundable:
            raise CheckoutStateError("Refund amount is not refundable.")

        amount_charged = clamp_money(locked_order.total - locked_order.credit_applied)
        gateway_already = min(locked_order.refund_total, amount_charged)
        gateway_room = clamp_money(amount_charged - gateway_already)
        gateway_amount = min(refund.amount, gateway_room)
        credit_amount = quantize_money(refund.amount - gateway_amount)
        return refund.pk, gateway_amount, credit_amount, refund.amount


def _run_gateway_and_finalize(refund: Refund, *, actor=None, notify: bool = True) -> Refund:
    if refund.status == Refund.Status.SUCCEEDED:
        return refund

    claimed = _claim_refund_for_gateway(refund.pk)
    if claimed == _CLAIM_SUCCEEDED:
        refund.refresh_from_db()
        return refund
    if claimed is None:
        refund.refresh_from_db()
        if refund.status == Refund.Status.SUCCEEDED:
            return refund
        if refund.status == Refund.Status.FAILED:
            raise CheckoutStateError("Refund has failed and cannot be retried without a new idempotency key.")
        raise CheckoutStateError("Refund is not in a processable state.")

    refund_id, gateway_amount, credit_amount, _amount = claimed
    refund = Refund.objects.select_related("order", "payment").get(pk=refund_id)
    payment = refund.payment

    if gateway_amount > 0:
        gw = get_payment_gateway()
        try:
            result = gw.refund(
                gateway_reference=payment.gateway_reference,
                amount=gateway_amount,
                currency=payment.currency,
                idempotency_key=refund.idempotency_key,
                tenant_scope=str(payment.tenant_id),
            )
            Refund.objects.filter(pk=refund.pk).update(gateway_reference=result.gateway_reference)
        except Exception as exc:
            Refund.objects.filter(pk=refund.pk).update(
                status=Refund.Status.FAILED,
                reason=(refund.reason or "")[:200] + f" [gateway: {exc}]"[:200],
            )
            raise

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=refund.order_id)
        locked_payment = Payment.objects.select_for_update().get(pk=refund.payment_id)
        refund = Refund.objects.select_for_update().get(pk=refund.pk)
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
            if quantity <= 0:
                continue
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
        if notify:
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
    return max(min(quantity, item.quantity), 0)
