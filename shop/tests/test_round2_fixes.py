"""Round 2 audit remediation tests"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from django.test import override_settings
from django.urls import reverse

from shop.models import AccountProfile, Tenant, TenantMembership
from shop.services import idempotency
from shop.services.exceptions import IdempotencyKeyReuseMismatch
from shop.tests.test_checkout_seam import make_cart, make_variant

pytestmark = pytest.mark.django_db


def test_production_settings_import():
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    env.update(
        {
            "DJANGO_ENV": "production",
            "DJANGO_SECRET_KEY": "test-secret",
            "DJANGO_ALLOWED_HOSTS": "example.com",
            "DJANGO_SITE_URL": "https://example.com",
            "CACHE_URL": "redis://127.0.0.1:6379/0",
            "DJANGO_EMAIL_HOST": "smtp.example.com",
            "DATABASE_URL": "postgres://commerce:commerce@localhost:5432/commerce",
            "MEDIA_PERSIST_LOCAL": "1",
            "TLS_CHECK_SECRET": "test-tls-secret",
            "OPS_METRICS_SECRET": "test-metrics-secret",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup(); from django.conf import settings; assert settings.SECURE_SSL_REDIRECT is not None",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, result.stderr


@override_settings(DEBUG=False, RUNNING_TESTS=False)
def test_healthz_accessible_without_tenant_host(client):
    resp = client.get("/healthz/")
    assert resp.status_code == 200


_CHECKOUT_PAYLOAD = {
    "shipping_method": "Standard",
    "address1": "1 Main St",
    "city": "Springfield",
    "postal_code": "12345",
}


def test_api_checkout_blocks_user_without_profile(client, django_user_model):
    user = django_user_model.objects.create_user(username="noprofile", password="x", email="n@example.com")
    client.force_login(user)
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    resp = client.post(
        reverse("api-checkout-attempts"),
        _CHECKOUT_PAYLOAD,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="no-profile-co",
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "email_not_verified"


def test_api_checkout_blocks_unverified_user(client, django_user_model):
    user = django_user_model.objects.create_user(username="unv", password="x", email="u@example.com")
    AccountProfile.objects.create(user=user, email_verified=False)
    client.force_login(user)
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    resp = client.post(
        reverse("api-checkout-attempts"),
        _CHECKOUT_PAYLOAD,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="unverified-co",
    )
    assert resp.status_code == 403


def test_idempotency_rejects_body_mismatch():
    from shop.tenancy import set_current_tenant

    tenant = Tenant.objects.get(slug="default")
    set_current_tenant(tenant)
    record = idempotency.begin("test", "key-1", payload='{"a":1}')
    idempotency.complete(record, status=200, body={"ok": True})
    with pytest.raises(IdempotencyKeyReuseMismatch):
        idempotency.begin("test", "key-1", payload='{"a":2}')


def test_order_detail_api_token_access(client):
    variant = make_variant()
    cart = make_cart(variant)
    from shop.services.checkout import begin_checkout
    from shop.services.payments import authorize_payment, confirm_payment

    attempt = begin_checkout(cart, idempotency_key="od-co", contact={"email": "g@example.com"})
    payment = authorize_payment(attempt, idempotency_key="od-pay")
    order = confirm_payment(payment, idempotency_key="od-cf")

    denied = client.get(reverse("api-order-detail", args=[order.order_number]))
    assert denied.status_code == 403

    ok = client.get(
        reverse("api-order-detail", args=[order.order_number]),
        {"token": str(order.order_token)},
    )
    assert ok.status_code == 200


def test_staff_membership_refund_denied(client, django_user_model):
    staff = django_user_model.objects.create_user(username="staff2", password="x")
    tenant = Tenant.objects.get(slug="default")
    TenantMembership.objects.create(tenant=tenant, user=staff, role=TenantMembership.Role.STAFF)
    client.force_login(staff)

    variant = make_variant()
    cart = make_cart(variant)
    from shop.services.checkout import begin_checkout
    from shop.services.payments import authorize_payment, confirm_payment

    attempt = begin_checkout(cart, idempotency_key=f"st-{uuid.uuid4().hex[:6]}")
    payment = authorize_payment(attempt, idempotency_key="st-pay")
    order = confirm_payment(payment, idempotency_key="st-cf")

    resp = client.post(
        reverse("api-staff-order-refund", args=[order.pk]),
        {"amount": str(order.total)},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="st-ref",
    )
    assert resp.status_code == 403
