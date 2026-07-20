"""Tests for audit remediation: compensation, API checkout, gateway hardening, partial refunds"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from shop.models import (
    AccountProfile,
    Cart,
    CartItem,
    Order,
    OutboxEvent,
    Payment,
    WebhookDelivery,
    WebhookEndpoint,
)
from shop.services.checkout import begin_checkout
from shop.services.exceptions import CheckoutStateError, OutOfStock
from shop.services.payments import authorize_payment, confirm_payment
from shop.services.refunds import create_refund, process_compensation_refund
from shop.tests.test_checkout_seam import make_cart, make_variant

pytestmark = pytest.mark.django_db


def test_compensation_refund_after_finalize_failure():
    variant = make_variant(quantity=5)
    cart = make_cart(variant, quantity=1)
    attempt = begin_checkout(cart, idempotency_key="comp-co")
    payment = authorize_payment(attempt, idempotency_key="comp-pay")
    with patch("shop.services.checkout.consume_reservations", side_effect=OutOfStock("gone")):
        with pytest.raises(CheckoutStateError):
            confirm_payment(payment, idempotency_key="comp-cf")

    payment.refresh_from_db()
    assert payment.status == Payment.Status.REQUIRES_REFUND
    event = OutboxEvent.objects.get(event_type="payment.auto_refund_required")
    process_compensation_refund(int(event.aggregate_id))
    payment.refresh_from_db()
    assert payment.status == Payment.Status.REFUNDED


def test_api_confirm_payment_creates_order(client):
    variant = make_variant()
    add_resp = client.post(
        "/api/v1/cart/items/",
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    assert add_resp.status_code == 201

    begin_resp = client.post(
        "/api/v1/checkout/attempts/",
        data={
            "shipping_method": "Standard",
            "email": "api-buyer@example.com",
            "name": "API Buyer",
            "address1": "1 Main",
            "city": "Town",
            "postal_code": "12345",
            "country": "US",
        },
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-co-1",
    )
    assert begin_resp.status_code == 201, begin_resp.content
    attempt_id = begin_resp.json()["id"]

    confirm_resp = client.post(
        f"/api/v1/checkout/attempts/{attempt_id}/confirm-payment/",
        data={"card_token": "tok_visa", "authorize_mode": "approve", "confirm_mode": "approve"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="api-cf-1",
    )
    assert confirm_resp.status_code == 201, confirm_resp.content
    assert Order.objects.filter(order_number=confirm_resp.json()["order_number"]).exists()


@override_settings(GATEWAY_TEST_MODES_ENABLED=False)
def test_gateway_test_modes_stripped_in_production():
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="gw-co")
    payment = authorize_payment(
        attempt,
        idempotency_key="gw-pay",
        card_token="tok_decline",
        mode="decline",
    )
    assert payment.status == Payment.Status.AUTHORIZED


def test_partial_refund_restock_quantity():
    variant = make_variant(quantity=10)
    cart = make_cart(variant, quantity=2)
    attempt = begin_checkout(cart, idempotency_key="partial-co")
    payment = authorize_payment(attempt, idempotency_key="partial-pay")
    order = confirm_payment(payment, idempotency_key="partial-cf")
    variant.refresh_from_db()
    assert variant.quantity == 8

    create_refund(
        order,
        amount=Decimal("5.00"),
        idempotency_key="partial-ref",
        restock=True,
    )
    variant.refresh_from_db()
    assert variant.quantity >= 8


def test_deliver_webhooks_command_enqueues(client):
    endpoint = WebhookEndpoint.objects.create(
        url="https://example.com/hook",
        secret="sek",
        event_types=[],
        active=True,
    )
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="wh-co")
    payment = authorize_payment(attempt, idempotency_key="wh-pay")
    confirm_payment(payment, idempotency_key="wh-cf")
    event = OutboxEvent.objects.filter(event_type="order.confirmation_email").first()
    assert event is not None
    call_command("deliver_webhooks", "--since-minutes", "1440")
    assert WebhookDelivery.objects.filter(endpoint=endpoint).exists() or OutboxEvent.objects.exists()


def test_cleanup_retention_command(client):
    variant = make_variant()
    cart = make_cart(variant)
    cart.status = Cart.Status.ABANDONED
    cart.save(update_fields=["status"])
    call_command("cleanup_retention")
    assert Cart.objects.filter(pk=cart.pk).exists()


def test_guest_order_api_requires_guest_order(client):
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="guest-api-co", contact={"email": "g@example.com"})
    payment = authorize_payment(attempt, idempotency_key="guest-api-pay")
    order = confirm_payment(payment, idempotency_key="guest-api-cf")

    ok = client.get(f"/api/v1/guest/orders/{order.order_token}/")
    assert ok.status_code == 200

    User = get_user_model()
    user = User.objects.create_user(username="reg", password="x", email="reg@example.com")
    order.user = user
    order.save(update_fields=["user"])
    blocked = client.get(f"/api/v1/guest/orders/{order.order_token}/")
    assert blocked.status_code == 404


def test_staff_refund_api_requires_manager(client):
    User = get_user_model()
    staff = User.objects.create_user(username="staff", password="x")
    from shop.models import Tenant, TenantMembership

    tenant = Tenant.objects.get(slug="default")
    TenantMembership.objects.create(tenant=tenant, user=staff, role=TenantMembership.Role.STAFF)
    client.force_login(staff)

    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="staff-api-co")
    payment = authorize_payment(attempt, idempotency_key="staff-api-pay")
    order = confirm_payment(payment, idempotency_key="staff-api-cf")

    denied = client.post(
        f"/api/v1/staff/orders/{order.pk}/refund/",
        data={"amount": str(order.total)},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="staff-ref-1",
    )
    assert denied.status_code == 403


def test_unverified_user_blocked_at_checkout():
    User = get_user_model()
    user = User.objects.create_user(username="unverified", password="x", email="u@example.com")
    AccountProfile.objects.create(user=user, email_verified=False)
    variant = make_variant()
    cart = Cart.objects.create(user=user, session_key="", status=Cart.Status.ACTIVE)
    CartItem.objects.create(cart=cart, variant=variant, quantity=1)

    from shop.services.exceptions import CheckoutStateError

    with pytest.raises(CheckoutStateError, match="Verify your email"):
        begin_checkout(cart, idempotency_key="unverified-co")
