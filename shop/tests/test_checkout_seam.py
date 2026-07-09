# Tests checkout seam: payment finalize, stock, coupons, refunds, and reconciliation
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from shop.models import (
    Cart,
    CartItem,
    Category,
    CheckoutAttempt,
    CouponCode,
    Order,
    Payment,
    Product,
    ProductVariant,
    Promotion,
    PromotionRedemption,
    Reservation,
)
from shop.services.cart import apply_coupon
from shop.services.checkout import begin_checkout
from shop.services.exceptions import CheckoutStateError, OutOfStock
from shop.services.inventory import expire_reservations
from shop.services.payments import authorize_payment, confirm_payment, reconcile_stranded_payments
from shop.services.refunds import create_refund

pytestmark = pytest.mark.django_db


def test_confirmed_payment_finalizes_order_and_consumes_stock():
    variant = make_variant(quantity=5, price="20.00")
    cart = make_cart(variant, quantity=2)

    attempt = begin_checkout(cart, idempotency_key="checkout-happy")
    assert attempt.status == CheckoutAttempt.Status.RESERVED
    assert attempt.reservations.filter(status=Reservation.Status.ACTIVE).count() == 1

    payment = authorize_payment(attempt, idempotency_key="payment-happy")
    order = confirm_payment(payment, idempotency_key="confirm-happy")

    variant.refresh_from_db()
    cart.refresh_from_db()
    attempt.refresh_from_db()
    payment.refresh_from_db()

    assert order.items.count() == 1
    assert variant.quantity == 3
    assert cart.status == Cart.Status.ORDERED
    assert attempt.status == CheckoutAttempt.Status.FINALIZED
    assert payment.status == Payment.Status.CONFIRMED
    assert Reservation.objects.filter(checkout_attempt=attempt, status=Reservation.Status.CONSUMED).count() == 1


def test_double_confirmation_replay_returns_single_order():
    variant = make_variant(quantity=3)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="checkout-replay")
    same_attempt = begin_checkout(cart, idempotency_key="checkout-replay")
    assert same_attempt.pk == attempt.pk

    payment = authorize_payment(attempt, idempotency_key="payment-replay")
    first_order = confirm_payment(payment, idempotency_key="confirm-replay")
    second_order = confirm_payment(payment, idempotency_key="confirm-replay")

    assert first_order.pk == second_order.pk
    assert Order.objects.count() == 1
    assert Payment.objects.count() == 1


def test_last_stock_checkout_is_reserved_once():
    variant = make_variant(quantity=1)
    first_cart = make_cart(variant)
    second_cart = make_cart(variant)

    begin_checkout(first_cart, idempotency_key="checkout-first")
    with pytest.raises(OutOfStock):
        begin_checkout(second_cart, idempotency_key="checkout-second")

    assert Reservation.objects.filter(status=Reservation.Status.ACTIVE).count() == 1
    variant.refresh_from_db()
    assert variant.quantity == 1


def test_failed_payment_releases_reservations():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="checkout-fail")

    payment = authorize_payment(
        attempt,
        idempotency_key="payment-fail",
        card_token="tok_decline",
    )
    attempt.refresh_from_db()

    assert payment.status == Payment.Status.FAILED
    assert attempt.status == CheckoutAttempt.Status.FAILED
    assert Reservation.objects.filter(checkout_attempt=attempt, status=Reservation.Status.ACTIVE).count() == 0
    assert Reservation.objects.filter(checkout_attempt=attempt, status=Reservation.Status.RELEASED).count() == 1


def test_expiry_sweep_does_not_release_payment_pending_reservation():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="checkout-expiry")
    authorize_payment(attempt, idempotency_key="payment-expiry")
    attempt.refresh_from_db()
    past = timezone.now() - timedelta(minutes=5)
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(expires_at=past)
    Reservation.objects.filter(checkout_attempt=attempt).update(expires_at=past)

    expired = expire_reservations(now=timezone.now())

    assert expired == 0
    assert Reservation.objects.get(checkout_attempt=attempt).status == Reservation.Status.ACTIVE


def test_reconciliation_finalizes_lost_confirmation():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="checkout-lost")
    payment = authorize_payment(
        attempt,
        idempotency_key="payment-lost",
        mode="dropped_confirmation",
    )
    payment.refresh_from_db()
    assert payment.status == Payment.Status.AUTHORIZED
    assert payment.raw_status == "confirmed"

    resolved = reconcile_stranded_payments(older_than=timedelta(seconds=0))

    assert resolved == 1
    assert Order.objects.count() == 1
    payment.refresh_from_db()
    assert payment.status == Payment.Status.CONFIRMED


def test_last_use_coupon_cannot_over_redeem():
    variant = make_variant(quantity=3, price="60.00")
    coupon = make_coupon(usage_limit=1)
    first_cart = make_cart(variant)
    second_cart = make_cart(variant)
    apply_coupon(first_cart, coupon.code)
    apply_coupon(second_cart, coupon.code)
    first_attempt = begin_checkout(first_cart, idempotency_key="coupon-first")
    second_attempt = begin_checkout(second_cart, idempotency_key="coupon-second")

    first_payment = authorize_payment(first_attempt, idempotency_key="payment-coupon-first")
    confirm_payment(first_payment, idempotency_key="confirm-coupon-first")
    second_payment = authorize_payment(second_attempt, idempotency_key="payment-coupon-second")
    with pytest.raises(CheckoutStateError):
        confirm_payment(second_payment, idempotency_key="confirm-coupon-second")

    coupon.promotion.refresh_from_db()
    second_payment.refresh_from_db()
    second_attempt.refresh_from_db()
    assert coupon.promotion.used_count == 1
    assert PromotionRedemption.objects.count() == 1
    assert Order.objects.count() == 1
    assert second_payment.status == Payment.Status.REQUIRES_REFUND
    assert second_attempt.status == CheckoutAttempt.Status.FAILED


def test_full_refund_can_restock_and_is_idempotent():
    variant = make_variant(quantity=4, price="40.00")
    cart = make_cart(variant, quantity=2)
    attempt = begin_checkout(cart, idempotency_key="checkout-refund")
    payment = authorize_payment(attempt, idempotency_key="payment-refund")
    order = confirm_payment(payment, idempotency_key="confirm-refund")
    variant.refresh_from_db()
    assert variant.quantity == 2

    refund = create_refund(
        order,
        amount=order.total,
        idempotency_key="refund-once",
        restock=True,
        reason="Customer return",
    )
    replay = create_refund(
        order,
        amount=order.total,
        idempotency_key="refund-once",
        restock=True,
        reason="Customer return",
    )

    variant.refresh_from_db()
    order.refresh_from_db()
    assert refund.pk == replay.pk
    assert variant.quantity == 4
    assert order.status == Order.Status.REFUNDED


def make_variant(*, quantity=10, price="25.00") -> ProductVariant:
    category, _ = Category.objects.get_or_create(name="Test", slug=f"test-{uuid.uuid4().hex[:8]}")
    product = Product.objects.create(
        category=category,
        name=f"Test Product {uuid.uuid4().hex[:6]}",
        slug=f"test-product-{uuid.uuid4().hex[:8]}",
        description="Test product",
        status=Product.Status.ACTIVE,
    )
    return ProductVariant.objects.create(
        product=product,
        sku=f"SKU-{uuid.uuid4().hex[:10]}",
        title="Default",
        price=Decimal(price),
        quantity=quantity,
        active=True,
    )


def make_cart(variant: ProductVariant, *, quantity=1) -> Cart:
    cart = Cart.objects.create(session_key=f"s-{uuid.uuid4().hex}")
    CartItem.objects.create(cart=cart, variant=variant, quantity=quantity)
    return cart


def make_coupon(*, usage_limit=1) -> CouponCode:
    promotion = Promotion.objects.create(
        name=f"Coupon {uuid.uuid4().hex[:6]}",
        type=Promotion.Type.PERCENTAGE,
        active=True,
        discount_percent=Decimal("10.00"),
        min_subtotal=Decimal("1.00"),
        usage_limit=usage_limit,
    )
    return CouponCode.objects.create(
        promotion=promotion,
        code=f"SAVE{uuid.uuid4().hex[:6]}",
    )
