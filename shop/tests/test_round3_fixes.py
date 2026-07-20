"""Round 3 audit remediation tests (TEST3-001 through TEST3-011)"""
from __future__ import annotations

import urllib.error
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from shop.models import (
    CheckoutAttempt,
    CustomerSubscription,
    EmailDelivery,
    Order,
    OutboxEvent,
    Refund,
    Tenant,
    TenantMembership,
    WebhookDelivery,
    WebhookEndpoint,
)
from shop.services import webhooks as webhook_service
from shop.services.checkout import begin_checkout
from shop.services.exceptions import CheckoutStateError, CommerceError, OutOfStock
from shop.services.inventory import consume_reservations
from shop.services.notifications import deliver_outbox_event
from shop.services.orders import cancel_order
from shop.services.payments import authorize_payment, confirm_payment
from shop.services.refunds import create_refund
from shop.services.subscriptions import generate_due_renewals
from shop.tenancy import clear_current_tenant, set_current_tenant
from shop.tests.conftest import ensure_verified_profile
from shop.tests.test_checkout_seam import make_cart, make_variant
from shop.tests.test_tenancy import _product, _tenant

pytestmark = pytest.mark.django_db


def _place_order(idem="r3", *, user=None, quantity=5, cart_qty=1, price="20.00"):
    variant = make_variant(quantity=quantity, price=price)
    cart = make_cart(variant, quantity=cart_qty)
    if user is not None:
        cart.user = user
        cart.save(update_fields=["user"])
        ensure_verified_profile(user)
    attempt = begin_checkout(cart, idempotency_key=f"co-{idem}")
    payment = authorize_payment(attempt, idempotency_key=f"pay-{idem}")
    order = confirm_payment(payment, idempotency_key=f"cf-{idem}")
    return order, variant


def _subscription(django_user_model):
    user = django_user_model.objects.create_user(
        username=f"u{uuid.uuid4().hex[:6]}", password="x", email=f"{uuid.uuid4().hex[:6]}@sub.test"
    )
    ensure_verified_profile(user)
    variant = make_variant(quantity=10, price="20.00")
    variant.subscription_interval = "monthly"
    variant.save(update_fields=["subscription_interval"])
    cart = make_cart(variant, quantity=1)
    cart.user = user
    cart.save(update_fields=["user"])
    attempt = begin_checkout(cart, idempotency_key=f"sub-{uuid.uuid4().hex[:6]}")
    payment = authorize_payment(attempt, idempotency_key=f"sub-pay-{uuid.uuid4().hex[:6]}")
    confirm_payment(payment, idempotency_key=f"sub-cf-{uuid.uuid4().hex[:6]}")
    sub = CustomerSubscription.objects.get(user=user, variant=variant)
    return user, variant, sub


# TEST3-001: subscription renewal recovery when finalized order exists
def test_test3_001_renewal_recovery_when_finalized_order_exists(django_user_model):
    user, _variant, sub = _subscription(django_user_model)
    due_at = timezone.now() - timedelta(days=1)
    CustomerSubscription.objects.filter(pk=sub.pk).update(next_renewal_at=due_at)
    sub.refresh_from_db()

    key = f"renewal-{sub.pk}-{sub.next_renewal_at.date().isoformat()}"
    existing_order, _ = _place_order("renewal-existing", user=user)
    CheckoutAttempt.objects.filter(pk=existing_order.checkout_attempt_id).update(
        idempotency_key=key,
        status=CheckoutAttempt.Status.FINALIZED,
    )

    orders_before = Order.objects.count()
    assert generate_due_renewals() == 1
    assert Order.objects.count() == orders_before

    sub.refresh_from_db()
    assert sub.status == CustomerSubscription.Status.ACTIVE
    assert sub.next_renewal_at > timezone.now()
    assert sub.last_order_id == existing_order.pk


# TEST3-002: renewal CommerceError marks PAST_DUE
def test_test3_002_renewal_commerce_error_marks_past_due(django_user_model, monkeypatch):
    _user, _variant, sub = _subscription(django_user_model)
    CustomerSubscription.objects.filter(pk=sub.pk).update(next_renewal_at=timezone.now() - timedelta(days=1))

    def boom(*_args, **_kwargs):
        raise CommerceError("renewal blocked", code="checkout_state_error")

    monkeypatch.setattr("shop.services.checkout.begin_checkout", boom)
    assert generate_due_renewals() == 0

    sub.refresh_from_db()
    assert sub.status == CustomerSubscription.Status.PAST_DUE


# TEST3-003: webhook retry after transient failure
def test_test3_003_webhook_retry_after_transient_failure(monkeypatch):
    endpoint = WebhookEndpoint.objects.create(url="https://example.test/hook", secret="s3cr3t", event_types=[])
    event = OutboxEvent.objects.create(
        event_type="order.confirmation_email",
        aggregate_type="Order",
        aggregate_id="1",
        payload={"order_number": "EC-1"},
    )
    webhook_service.enqueue_deliveries(event)
    delivery = WebhookDelivery.objects.get(endpoint=endpoint, outbox_event=event)

    def fail_post(_url, _body, _headers):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(webhook_service, "_post", fail_post)
    sent, failed = webhook_service.deliver_pending()

    delivery.refresh_from_db()
    assert (sent, failed) == (0, 0)
    assert delivery.status == WebhookDelivery.Status.PENDING
    assert delivery.attempts == 1
    assert delivery.last_error


# TEST3-004: webhook FAILED after MAX_ATTEMPTS
def test_test3_004_webhook_failed_after_max_attempts(monkeypatch):
    endpoint = WebhookEndpoint.objects.create(url="https://example.test/hook", secret="s3cr3t", event_types=[])
    event = OutboxEvent.objects.create(
        event_type="order.confirmation_email",
        aggregate_type="Order",
        aggregate_id="2",
        payload={"order_number": "EC-2"},
    )
    webhook_service.enqueue_deliveries(event)
    delivery = WebhookDelivery.objects.get(endpoint=endpoint, outbox_event=event)

    def fail_post(_url, _body, _headers):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(webhook_service, "_post", fail_post)
    for _ in range(webhook_service.MAX_ATTEMPTS):
        webhook_service.deliver_pending()

    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.FAILED
    assert delivery.attempts == webhook_service.MAX_ATTEMPTS


# TEST3-005: concurrent deliver_pending skip_locked
def test_test3_005_concurrent_deliver_pending_skip_locked():
    endpoint = WebhookEndpoint.objects.create(url="https://example.test/hook", secret="s3cr3t", event_types=[])
    event = OutboxEvent.objects.create(
        event_type="order.confirmation_email",
        aggregate_type="Order",
        aggregate_id="3",
        payload={"order_number": "EC-3"},
    )
    webhook_service.enqueue_deliveries(event)
    delivery = WebhookDelivery.objects.get(endpoint=endpoint, outbox_event=event)

    seen_kwargs: dict = {}
    real_sfu = WebhookDelivery.objects.select_for_update

    def tracking_sfu(**kwargs):
        seen_kwargs.update(kwargs)
        return real_sfu(**kwargs)

    with patch.object(WebhookDelivery.objects, "select_for_update", side_effect=tracking_sfu):
        webhook_service._claim_delivery(delivery.pk)
    assert seen_kwargs.get("skip_locked") is True

    locked_qs = MagicMock()
    locked_qs.select_related.return_value = locked_qs
    locked_qs.filter.return_value = locked_qs
    locked_qs.first.return_value = None
    with patch.object(WebhookDelivery.objects, "select_for_update", return_value=locked_qs):
        assert webhook_service._claim_delivery(delivery.pk) is None


# TEST3-006: StaffCancelView API idempotency replay
def test_test3_006_staff_cancel_api_idempotency_replay(client, django_user_model, settings):
    settings.ALLOWED_HOSTS = ["*"]
    tenant = Tenant.objects.get(slug="default")
    manager = django_user_model.objects.create_user(username=f"mgr-{uuid.uuid4().hex[:6]}", password="x")
    TenantMembership.objects.create(tenant=tenant, user=manager, role=TenantMembership.Role.MANAGER)
    client.force_login(manager)

    order, _ = _place_order("staff-cancel-api")
    url = reverse("api-staff-order-cancel", args=[order.pk])
    payload = {"note": "customer request", "restock": False}
    headers = {"HTTP_IDEMPOTENCY_KEY": "cancel-replay-key", "HTTP_HOST": "testserver"}

    first = client.post(url, payload, content_type="application/json", **headers)
    second = client.post(url, payload, content_type="application/json", **headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] in {Order.Status.CANCELLED, Order.Status.REFUNDED}
    assert second.json() == first.json()
    order.refresh_from_db()
    assert order.status in {Order.Status.CANCELLED, Order.Status.REFUNDED}
    assert Refund.objects.filter(order=order).count() == 1


# TEST3-007: cancel_order twice is no-op
def test_test3_007_cancel_order_twice_is_noop():
    order, _ = _place_order("dbl-cancel")
    first = cancel_order(order, note="once")
    refunds_after_first = Refund.objects.filter(order=order).count()

    second = cancel_order(order, note="again")

    terminal = {Order.Status.CANCELLED, Order.Status.REFUNDED}
    assert first.status in terminal
    assert second.status == first.status
    assert second.pk == first.pk
    assert Refund.objects.filter(order=order).count() == refunds_after_first


# TEST3-008: pending refund blocks second refund over ceiling
def test_test3_008_pending_refund_blocks_second_refund_over_ceiling():
    order, _ = _place_order("pending-ceiling", price="50.00")
    payment = order.payments.first()
    pending_amount = order.total - Decimal("5.00")
    Refund.objects.create(
        order=order,
        payment=payment,
        idempotency_key="pending-hold",
        amount=pending_amount,
        status=Refund.Status.PENDING,
    )

    with pytest.raises(CheckoutStateError, match="not refundable"):
        create_refund(
            order,
            amount=Decimal("10.00"),
            idempotency_key="over-ceiling",
        )


# TEST3-009: consume_reservations rows_updated TOCTOU
def test_test3_009_consume_reservations_rows_updated_toctou():
    variant = make_variant(quantity=2)
    cart = make_cart(variant, quantity=1)
    attempt = begin_checkout(cart, idempotency_key="toctou-co")
    order, _ = _place_order("toctou-order")

    real_filter = type(variant).objects.filter

    def selective_filter(*args, **kwargs):
        qs = real_filter(*args, **kwargs)
        if kwargs.get("quantity__gte") is not None:

            def lose_race(**_upd):
                return 0

            qs.update = lose_race
            return qs
        return qs

    with patch("shop.services.inventory.ProductVariant.objects.filter", side_effect=selective_filter):
        with pytest.raises(OutOfStock, match="insufficient physical stock"):
            consume_reservations(attempt, order)


# TEST3-010: SENDING email delivery retry
def test_test3_010_sending_email_delivery_retry(monkeypatch):
    variant = make_variant(quantity=5, price="20.00")
    cart = make_cart(variant, quantity=1)
    attempt = begin_checkout(
        cart,
        idempotency_key="email-retry-co",
        contact={"email": "buyer@example.com"},
    )
    payment = authorize_payment(attempt, idempotency_key="email-retry-pay")
    order = confirm_payment(payment, idempotency_key="email-retry-cf")
    event = OutboxEvent.objects.get(event_type="order.confirmation_email", aggregate_id=str(order.pk))

    calls = {"n": 0}

    class FlakyMessage:
        def attach_alternative(self, *_args, **_kwargs):
            return None

        def send(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("smtp unavailable")

    monkeypatch.setattr(
        "shop.services.notifications.EmailMultiAlternatives",
        lambda *_args, **_kwargs: FlakyMessage(),
    )

    with pytest.raises(OSError):
        deliver_outbox_event(event)

    delivery = EmailDelivery.objects.get(outbox_event=event)
    assert delivery.status == EmailDelivery.Status.QUEUED

    deliver_outbox_event(event)
    delivery.refresh_from_db()
    assert delivery.status == EmailDelivery.Status.SENT


# TEST3-011: TENANT_PLATFORM_DOMAINS multi-label subdomain routing
@override_settings(ALLOWED_HOSTS=["*"], TENANT_PLATFORM_DOMAINS=["platform.test"])
def test_test3_011_tenant_platform_domains_multi_label_subdomain(client):
    tenant = _tenant("widgetco")
    set_current_tenant(tenant)
    _product("Widget Product", "widget-prod")
    clear_current_tenant()

    resp = client.get("/", HTTP_HOST="widgetco.platform.test")
    assert resp.status_code == 200
    assert b"Widget Product" in resp.content


# TEST3-012: docker-compose file validates and defines expected services
def test_test3_012_docker_compose_smoke():
    import os
    import subprocess
    from pathlib import Path

    if os.environ.get("DOCKER_COMPOSE_TEST") != "1":
        pytest.skip("Set DOCKER_COMPOSE_TEST=1 to run docker compose validation")

    compose_file = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    for service in ("db", "redis", "web", "worker", "backup"):
        assert f"{service}:" in rendered
