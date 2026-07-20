"""Store credit and gift card balance, holds at checkout, and redemption spending"""
from __future__ import annotations

import contextlib
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from shop.models import (
    CheckoutAttempt,
    GiftCard,
    Order,
    StoreCredit,
    StoreCreditTransaction,
    normalize_coupon_code,
)
from shop.tenancy import get_current_tenant_id, tenant_context

from .exceptions import GiftCardError
from .money import quantize_money

_ZERO = Decimal("0.00")


def _credit_tenant_id(*, attempt: CheckoutAttempt | None = None) -> int:
    if attempt is not None:
        return attempt.tenant_id
    tid = get_current_tenant_id()
    if tid is None:
        raise RuntimeError("Tenant context is required for store credit operations.")
    return tid


def _store_credit_queryset(*, user_id=None, attempt: CheckoutAttempt | None = None):
    tid = _credit_tenant_id(attempt=attempt)
    qs = StoreCredit.objects.filter(tenant_id=tid)
    if user_id is not None:
        qs = qs.filter(user_id=user_id)
    return qs


@contextlib.contextmanager
def _credit_tenant_scope(*, attempt: CheckoutAttempt | None = None):
    tid = _credit_tenant_id(attempt=attempt)
    with tenant_context(tid):
        yield


def get_balance(user) -> Decimal:
    if not getattr(user, "is_authenticated", False):
        return _ZERO
    with _credit_tenant_scope():
        sc = _store_credit_queryset(user_id=user.pk).first()
    return sc.balance if sc else _ZERO


def redeem_gift_card(code: str, user) -> Decimal:
    """Move a gift card's balance into the user's store credit (single use)."""
    normalized = normalize_coupon_code(code)
    if not normalized:
        raise GiftCardError("Enter a gift card code.")
    with _credit_tenant_scope():
        with transaction.atomic():
            card = GiftCard.objects.select_for_update().filter(code=normalized, active=True).first()
            if not card or card.balance <= 0:
                raise GiftCardError("Gift card is invalid or has no balance.")
            if card.expires_at and card.expires_at <= timezone.now():
                raise GiftCardError("Gift card has expired.")
            amount = card.balance
            card.balance = _ZERO
            card.active = False
            card.save(update_fields=["balance", "active", "updated_at"])
            sc = _store_credit_queryset(user_id=user.pk).first()
            if sc is None:
                sc = StoreCredit.objects.create(user=user, tenant_id=_credit_tenant_id())
            StoreCredit.objects.filter(pk=sc.pk).update(balance=F("balance") + amount)
            StoreCreditTransaction.objects.create(
                user=user,
                store_credit=sc,
                delta=amount,
                reason=StoreCreditTransaction.Reason.GIFT_CARD,
                note=f"Gift card {normalized}",
            )
    return amount


def hold_credit(attempt: CheckoutAttempt, max_amount: Decimal) -> Decimal:
    """Debit and hold store credit for an attempt (mirrors stock reservation)."""
    if not attempt.user_id:
        return _ZERO
    if attempt.credit_applied > 0:
        return attempt.credit_applied
    max_amount = quantize_money(max_amount)
    with _credit_tenant_scope(attempt=attempt):
        with transaction.atomic():
            locked = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked.credit_applied > 0:
                attempt.credit_applied = locked.credit_applied
                return locked.credit_applied
            sc = _store_credit_queryset(user_id=locked.user_id, attempt=locked).select_for_update().first()
            if not sc or sc.balance <= 0 or max_amount <= 0:
                return _ZERO
            amount = min(sc.balance, max_amount)
            StoreCredit.objects.filter(pk=sc.pk).update(balance=F("balance") - amount)
            StoreCreditTransaction.objects.create(
                user_id=locked.user_id,
                store_credit=sc,
                delta=-amount,
                reason=StoreCreditTransaction.Reason.CHECKOUT_HOLD,
                checkout_attempt=locked,
                tenant_id=locked.tenant_id,
            )
            CheckoutAttempt.objects.filter(pk=locked.pk).update(credit_applied=amount)
            attempt.credit_applied = amount
    return amount


def release_hold(attempt: CheckoutAttempt) -> Decimal:
    """Return a held (unspent) credit to the user. Idempotent; no-op if none/finalized."""
    with _credit_tenant_scope(attempt=attempt):
        with transaction.atomic():
            locked = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked.credit_applied <= 0 or locked.status == CheckoutAttempt.Status.FINALIZED:
                return _ZERO
            amount = locked.credit_applied
            if locked.user_id:
                sc = _store_credit_queryset(user_id=locked.user_id, attempt=locked).first()
                if sc is None:
                    sc = StoreCredit.objects.create(user_id=locked.user_id, tenant_id=locked.tenant_id)
                StoreCredit.objects.filter(pk=sc.pk).update(balance=F("balance") + amount)
                StoreCreditTransaction.objects.create(
                    user_id=locked.user_id,
                    store_credit=sc,
                    delta=amount,
                    reason=StoreCreditTransaction.Reason.HOLD_RELEASE,
                    checkout_attempt=locked,
                )
            CheckoutAttempt.objects.filter(pk=locked.pk).update(credit_applied=_ZERO)
            attempt.credit_applied = _ZERO
    return amount


def spend_hold(attempt: CheckoutAttempt, order: Order) -> None:
    """Convert a held credit into a spend on the finalized order (no balance change)."""
    if attempt.credit_applied <= 0:
        return
    with _credit_tenant_scope(attempt=attempt):
        with transaction.atomic():
            StoreCreditTransaction.objects.filter(
                checkout_attempt=attempt, reason=StoreCreditTransaction.Reason.CHECKOUT_HOLD
            ).update(order=order)
            Order.objects.filter(pk=order.pk).update(credit_applied=attempt.credit_applied)
            order.credit_applied = attempt.credit_applied


def refund_to_credit(user_id, amount: Decimal, order: Order) -> None:
    amount = quantize_money(amount)
    if amount <= 0 or not user_id:
        return
    attempt = getattr(order, "checkout_attempt", None)
    with _credit_tenant_scope(attempt=attempt):
        with transaction.atomic():
            sc = _store_credit_queryset(user_id=user_id, attempt=attempt).select_for_update().first()
            if sc is None:
                tid = attempt.tenant_id if attempt is not None else _credit_tenant_id()
                sc = StoreCredit.objects.create(user_id=user_id, tenant_id=tid)
            StoreCredit.objects.filter(pk=sc.pk).update(balance=F("balance") + amount)
            StoreCreditTransaction.objects.create(
                user_id=user_id,
                store_credit=sc,
                delta=amount,
                reason=StoreCreditTransaction.Reason.REFUND_CREDIT,
                order=order,
            )
