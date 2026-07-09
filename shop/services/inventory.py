# Stock availability, reservations, consumption, expiry, and manual adjustments
from __future__ import annotations

from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from shop.models import CheckoutAttempt, InventoryMovement, ProductVariant, Reservation

from .exceptions import OutOfStock

PAYMENT_PROTECTED_STATUSES = {
    CheckoutAttempt.Status.PAYMENT_PENDING,
    CheckoutAttempt.Status.PAYMENT_CONFIRMED,
    CheckoutAttempt.Status.FINALIZED,
}


def variants_with_availability(queryset=None):
    """Annotate variants with ``active_reserved`` so availability needs no per-row query.

    Use this in list/detail prefetches to keep catalog rendering off the N+1 path (§31).
    """
    qs = queryset if queryset is not None else ProductVariant.objects.all()
    return qs.annotate(
        active_reserved=Coalesce(
            Sum("reservations__quantity", filter=Q(reservations__status=Reservation.Status.ACTIVE)),
            0,
        )
    )


def active_reserved_quantity(variant: ProductVariant, *, exclude_attempt: CheckoutAttempt | None = None) -> int:
    reservations = Reservation.objects.filter(variant=variant, status=Reservation.Status.ACTIVE)
    if exclude_attempt:
        reservations = reservations.exclude(checkout_attempt=exclude_attempt)
    return reservations.aggregate(total=Sum("quantity"))["total"] or 0


def available_to_sell(variant: ProductVariant, *, exclude_attempt: CheckoutAttempt | None = None) -> int:
    # Prefer a pre-annotated value when no attempt-exclusion is needed (avoids N+1).
    annotated = getattr(variant, "active_reserved", None)
    if exclude_attempt is None and annotated is not None:
        return max(variant.quantity - annotated, 0)
    reserved = active_reserved_quantity(variant, exclude_attempt=exclude_attempt)
    return max(variant.quantity - reserved, 0)


def reserve_for_attempt(attempt: CheckoutAttempt) -> list[Reservation]:
    if attempt.reservations.filter(status=Reservation.Status.ACTIVE).exists():
        return list(attempt.reservations.filter(status=Reservation.Status.ACTIVE))

    reservations: list[Reservation] = []
    lines = attempt.line_snapshots.select_related("variant").order_by("variant_id")
    for line in lines:
        variant = ProductVariant.objects.select_for_update().get(pk=line.variant_id)
        available = available_to_sell(variant)
        if line.quantity > available:
            raise OutOfStock(
                f"{variant.sku} has only {available} available.",
                field_errors={"variant": variant.sku, "available": available},
            )
        reservations.append(
            Reservation.objects.create(
                variant=variant,
                cart=attempt.cart,
                checkout_attempt=attempt,
                quantity=line.quantity,
                expires_at=attempt.expires_at,
            )
        )
    return reservations


def release_reservations(attempt: CheckoutAttempt, *, status: str = Reservation.Status.RELEASED) -> int:
    now = timezone.now()
    with transaction.atomic():
        reservations = Reservation.objects.select_for_update().filter(
            checkout_attempt=attempt,
            status=Reservation.Status.ACTIVE,
        )
        count = reservations.update(status=status, released_at=now)
        if status == Reservation.Status.EXPIRED:
            CheckoutAttempt.objects.filter(
                pk=attempt.pk,
                status__in=[CheckoutAttempt.Status.STARTED, CheckoutAttempt.Status.RESERVED],
            ).update(status=CheckoutAttempt.Status.EXPIRED)
        return count


def consume_reservations(attempt: CheckoutAttempt, order) -> None:
    reservations = (
        Reservation.objects.select_for_update()
        .select_related("variant")
        .filter(checkout_attempt=attempt, status=Reservation.Status.ACTIVE)
        .order_by("variant_id")
    )
    if not reservations.exists():
        raise OutOfStock("No active reservations exist for this checkout attempt.")

    for reservation in reservations:
        variant = ProductVariant.objects.select_for_update().get(pk=reservation.variant_id)
        if variant.quantity < reservation.quantity:
            raise OutOfStock(
                f"{variant.sku} has insufficient physical stock for finalization.",
                field_errors={"variant": variant.sku, "available": variant.quantity},
            )
        ProductVariant.objects.filter(pk=variant.pk, quantity__gte=reservation.quantity).update(
            quantity=F("quantity") - reservation.quantity
        )
        reservation.status = Reservation.Status.CONSUMED
        reservation.released_at = timezone.now()
        reservation.save(update_fields=["status", "released_at", "updated_at"])
        InventoryMovement.objects.create(
            variant=variant,
            quantity_delta=-reservation.quantity,
            reason=InventoryMovement.Reason.RESERVATION_CONSUMED,
            reservation=reservation,
            order=order,
            note=f"Checkout attempt {attempt.pk} finalized",
        )


def expire_reservations(*, now=None) -> int:
    now = now or timezone.now()
    with transaction.atomic():
        reservations = (
            Reservation.objects.select_for_update()
            .select_related("checkout_attempt")
            .filter(status=Reservation.Status.ACTIVE, expires_at__lte=now)
            .exclude(checkout_attempt__status__in=PAYMENT_PROTECTED_STATUSES)
        )
        attempt_ids = set(reservations.values_list("checkout_attempt_id", flat=True))
        count = reservations.update(status=Reservation.Status.EXPIRED, released_at=now)
        expired_ids = list(
            CheckoutAttempt.objects.filter(
                id__in=attempt_ids,
                status__in=[CheckoutAttempt.Status.STARTED, CheckoutAttempt.Status.RESERVED],
                credit_applied__gt=0,
            ).values_list("id", flat=True)
        )
        CheckoutAttempt.objects.filter(
            id__in=attempt_ids,
            status__in=[CheckoutAttempt.Status.STARTED, CheckoutAttempt.Status.RESERVED],
        ).update(status=CheckoutAttempt.Status.EXPIRED)
    # Return any held store credit for expired attempts (outside the sweep transaction).
    if expired_ids:
        from .credit import release_hold

        for attempt in CheckoutAttempt.objects.filter(id__in=expired_ids):
            release_hold(attempt)
    return count


def adjust_stock(variant: ProductVariant, delta: int, *, actor=None, reason=None, note: str = "") -> InventoryMovement:
    reason = reason or InventoryMovement.Reason.MANUAL_ADJUSTMENT
    with transaction.atomic():
        locked = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        new_quantity = locked.quantity + delta
        if new_quantity < 0:
            raise OutOfStock("Stock adjustment would make quantity negative.")
        locked.quantity = new_quantity
        locked.save(update_fields=["quantity", "updated_at"])
        return InventoryMovement.objects.create(
            variant=locked,
            quantity_delta=delta,
            reason=reason,
            staff_user=actor,
            note=note,
        )
