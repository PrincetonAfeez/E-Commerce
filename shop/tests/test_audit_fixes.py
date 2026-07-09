# Tests audit fixes: reservation expiry, idempotency locks, rate limits, and price drift
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from shop.models import (
    AccountProfile,
    CheckoutAttempt,
    Fulfillment,
    IdempotencyRecord,
    Order,
    OutboxEvent,
    Payment,
    PromotionRedemption,
    Reservation,
)
from shop.services import idempotency
from shop.services.checkout import begin_checkout
from shop.services.exceptions import CheckoutStateError, IdempotencyInProgress, InvalidCoupon
from shop.services.inventory import expire_reservations, variants_with_availability
from shop.services.money import allocate_amount
from shop.services.orders import cancel_order, transition_fulfillment
from shop.services.payments import authorize_payment, confirm_payment, reconcile_stranded_payments
from shop.services.promotions import redeem_coupon_for_order

from .test_checkout_seam import make_cart, make_coupon, make_variant

pytestmark = pytest.mark.django_db


# --- #17/#5: the sweep DOES release an abandoned reserved attempt ---
def test_expiry_sweep_releases_abandoned_reservation():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="abandon")
    past = timezone.now() - timedelta(minutes=1)
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(expires_at=past)
    Reservation.objects.filter(checkout_attempt=attempt).update(expires_at=past)

    released = expire_reservations(now=timezone.now())

    attempt.refresh_from_db()
    assert released == 1
    assert attempt.status == CheckoutAttempt.Status.EXPIRED
    assert Reservation.objects.get(checkout_attempt=attempt).status == Reservation.Status.EXPIRED


# --- #5: an authorized-but-never-confirmed abandoned attempt is reconciled + stock freed ---
def test_reconcile_releases_abandoned_authorization():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="auth-abandon")
    authorize_payment(attempt, idempotency_key="auth-abandon-pay")  # approve -> intent authorized
    attempt.refresh_from_db()
    assert attempt.status == CheckoutAttempt.Status.PAYMENT_PENDING
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(
        payment_started_at=timezone.now() - timedelta(hours=2)
    )

    resolved = reconcile_stranded_payments(abandon_authorized_after=timedelta(hours=1))

    attempt.refresh_from_db()
    assert resolved == 1
    assert attempt.status == CheckoutAttempt.Status.FAILED
    assert Reservation.objects.filter(
        checkout_attempt=attempt, status=Reservation.Status.ACTIVE
    ).count() == 0


# --- #4: idempotency lock reclaim / in-progress semantics ---
def test_idempotency_live_lock_returns_in_progress():
    idempotency.begin("scope-a", "k1", session_key="s")
    with pytest.raises(IdempotencyInProgress):
        idempotency.begin("scope-a", "k1", session_key="s")


def test_idempotency_expired_lock_is_reclaimed():
    record = idempotency.begin("scope-b", "k2", session_key="s")
    IdempotencyRecord.objects.filter(pk=record.pk).update(
        locked_until=timezone.now() - timedelta(minutes=1)
    )
    reclaimed = idempotency.begin("scope-b", "k2", session_key="s")
    assert reclaimed.pk == record.pk
    assert reclaimed.status == IdempotencyRecord.Status.IN_PROGRESS


def _place_order(idem="fix", *, quantity=5, cart_qty=1, price="20.00"):
    variant = make_variant(quantity=quantity, price=price)
    cart = make_cart(variant, quantity=cart_qty)
    attempt = begin_checkout(cart, idempotency_key=f"co-{idem}")
    payment = authorize_payment(attempt, idempotency_key=f"pay-{idem}")
    order = confirm_payment(payment, idempotency_key=f"cf-{idem}")
    return order, variant


# --- #2: cancelling a paid order refunds the money and (optionally) restocks ---
def test_cancel_paid_order_refunds_and_restocks():
    order, variant = _place_order("cancel")
    variant.refresh_from_db()
    assert variant.quantity == 4  # one unit consumed at finalization

    cancel_order(order, note="changed mind", restock=True)

    order.refresh_from_db()
    variant.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    assert order.refund_total == order.total  # money returned
    assert variant.quantity == 5  # restocked
    assert order.payments.first().status == Payment.Status.REFUNDED
    assert OutboxEvent.objects.filter(
        event_type="order.cancelled_email", aggregate_id=str(order.pk)
    ).exists()


# --- #3: delivered + failed-payment notifications are queued ---
def test_delivered_transition_queues_email():
    order, _ = _place_order("deliver")
    transition_fulfillment(order, target_status=Fulfillment.Status.PROCESSING)
    transition_fulfillment(order, target_status=Fulfillment.Status.SHIPPED)
    transition_fulfillment(order, target_status=Fulfillment.Status.DELIVERED)
    assert OutboxEvent.objects.filter(
        event_type="order.delivered_email", aggregate_id=str(order.pk)
    ).exists()


def test_failed_payment_queues_email():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="pf")
    authorize_payment(attempt, idempotency_key="pf-pay", card_token="tok_decline")
    assert OutboxEvent.objects.filter(
        event_type="payment.failed_email", aggregate_id=str(attempt.pk)
    ).exists()


# --- #1 (R4): a cancelled order cannot be fulfilled ---
def test_cannot_fulfill_cancelled_order():
    order, _ = _place_order("nofulfill")
    cancel_order(order, note="cancelled")
    order.refresh_from_db()
    with pytest.raises(CheckoutStateError):
        transition_fulfillment(order, target_status=Fulfillment.Status.PROCESSING)


# --- #3 (R4): email verification token flow works ---
def test_email_verification_marks_profile_verified(client):
    user = get_user_model().objects.create_user(
        username=f"v{uuid.uuid4().hex[:6]}", email="v@example.com", password="x"
    )
    profile = AccountProfile.objects.create(user=user)
    assert profile.email_verified is False
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    resp = client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))
    assert resp.status_code == 302
    profile.refresh_from_db()
    assert profile.email_verified is True


def test_email_verification_rejects_bad_token(client):
    user = get_user_model().objects.create_user(username=f"v{uuid.uuid4().hex[:6]}", password="x")
    AccountProfile.objects.create(user=user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": "bad-token"}))
    assert AccountProfile.objects.get(user=user).email_verified is False


# --- #2 (R4): auth endpoints are rate limited ---
def test_register_is_rate_limited(client):
    cache.clear()
    statuses = [client.post(reverse("register"), {}).status_code for _ in range(12)]
    assert 429 in statuses  # the throttle trips within the window


# --- DRF API layer is throttled (returns 429 past the anon rate) ---
def test_api_throttle_returns_429(client, monkeypatch):
    from rest_framework.throttling import AnonRateThrottle

    cache.clear()
    # DRF binds THROTTLE_RATES as a class attribute at import, so tighten it directly.
    monkeypatch.setattr(AnonRateThrottle, "THROTTLE_RATES", {"anon": "3/min"}, raising=False)
    url = reverse("api-products-list")
    statuses = [client.get(url).status_code for _ in range(6)]
    assert 429 in statuses  # the DRF throttle trips within the window


# --- #5: availability annotation avoids the per-variant query ---
def test_variants_with_availability_annotation():
    variant = make_variant(quantity=10)
    cart = make_cart(variant)
    begin_checkout(cart, idempotency_key="annot")  # reserves 1
    annotated = variants_with_availability().get(pk=variant.pk)
    assert annotated.active_reserved == 1
    assert annotated.available_to_sell() == 9


# --- #14: money allocation never drops cents, even with all-zero weights ---
def test_allocate_amount_zero_weights_reconciles():
    parts = allocate_amount(Decimal("10.00"), [Decimal("0.00"), Decimal("0.00")])
    assert sum(parts) == Decimal("10.00")
    assert all(p >= 0 for p in parts)


def test_allocate_amount_normal_reconciles():
    parts = allocate_amount(Decimal("10.00"), [Decimal("1.00"), Decimal("3.00")])
    assert sum(parts) == Decimal("10.00")


# --- #17: a terminal checkout attempt is not silently reused ---
def test_terminal_attempt_is_not_reused():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="term")
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(status=CheckoutAttempt.Status.EXPIRED)
    with pytest.raises(CheckoutStateError):
        begin_checkout(cart, idempotency_key="term")


# --- #11: price drift is detected and surfaced ---
def test_price_drift_message_set_when_expected_subtotal_differs():
    variant = make_variant(quantity=3, price="20.00")
    cart = make_cart(variant, quantity=1)
    attempt = begin_checkout(cart, idempotency_key="drift", expected_subtotal=Decimal("999.00"))
    assert "changed" in attempt.price_drift_message.lower()


def test_no_price_drift_when_shipping_method_changes():
    # Subtotal-based drift check must not fire just because Express shipping changed the total.
    variant = make_variant(quantity=3, price="20.00")
    cart = make_cart(variant, quantity=1)
    attempt = begin_checkout(
        cart, idempotency_key="drift-ship", shipping_method="Express", expected_subtotal=Decimal("20.00")
    )
    assert attempt.price_drift_message == ""


def test_no_price_drift_when_totals_match():
    variant = make_variant(quantity=3, price="20.00")
    cart = make_cart(variant, quantity=1)
    totals_attempt = begin_checkout(cart, idempotency_key="nodrift")
    assert totals_attempt.price_drift_message == ""


# --- #2: guest order is not viewable without the session or token ---
def test_checkout_complete_forbidden_without_token(client):
    order, _ = _place_order("acl")
    assert order.user_id is None
    url = reverse("checkout:complete", args=[order.order_number])
    assert client.get(url).status_code == 403
    assert client.get(url, {"token": str(order.order_token)}).status_code == 200


# --- #3: HTTP-level idempotency replays the winner's stored result ---
def test_api_begin_checkout_idempotency_replay(client):
    variant = make_variant(quantity=5, price="15.00")
    add_url = reverse("api-cart-items")
    resp = client.post(add_url, {"variant_id": variant.pk, "quantity": 1}, content_type="application/json")
    assert resp.status_code == 201

    url = reverse("api-checkout-attempts")
    headers = {"HTTP_IDEMPOTENCY_KEY": "api-idem-1"}
    first = client.post(url, {"shipping_method": "Standard"}, content_type="application/json", **headers)
    second = client.post(url, {"shipping_method": "Standard"}, content_type="application/json", **headers)

    assert first.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert CheckoutAttempt.objects.count() == 1


def test_api_begin_checkout_requires_idempotency_key(client):
    variant = make_variant(quantity=5, price="15.00")
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    resp = client.post(reverse("api-checkout-attempts"), {}, content_type="application/json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "idempotency_key_required"


# --- #6: per-customer usage limit > 1 is honoured (not blocked at 1) ---
def test_per_customer_limit_allows_up_to_limit(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    coupon = make_coupon(usage_limit=None)
    coupon.promotion.per_customer_usage_limit = 2
    coupon.promotion.save(update_fields=["per_customer_usage_limit"])

    for i in range(2):
        order = Order.objects.create(
            user=user,
            checkout_attempt=begin_checkout(
                make_cart(make_variant(quantity=3, price="60.00"), quantity=1),
                idempotency_key=f"pcl-{i}",
            ),
            subtotal=Decimal("60.00"),
            discount_total=Decimal("6.00"),
            total=Decimal("54.00"),
            coupon_code=coupon,
        )
        redeem_coupon_for_order(order)

    assert PromotionRedemption.objects.filter(promotion=coupon.promotion, user=user).count() == 2

    # A third redemption exceeds the per-customer limit.
    order3 = Order.objects.create(
        user=user,
        checkout_attempt=begin_checkout(
            make_cart(make_variant(quantity=3, price="60.00"), quantity=1),
            idempotency_key="pcl-3",
        ),
        subtotal=Decimal("60.00"),
        discount_total=Decimal("6.00"),
        total=Decimal("54.00"),
        coupon_code=coupon,
    )
    with pytest.raises(InvalidCoupon):
        redeem_coupon_for_order(order3)
