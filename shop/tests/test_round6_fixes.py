"""Round 6 remediation tests"""
from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from shop.models import (
    CheckoutAttempt,
    OrderItem,
    Payment,
    ProductVariant,
    ReturnLine,
    ReturnRequest,
    Tenant,
)
from shop.services.checkout import begin_checkout
from shop.services.payments import authorize_payment
from shop.services.returns import _returnable_quantity
from shop.tests.test_checkout_seam import make_cart, make_variant

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]


def _prod_subprocess_env(**overrides):
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
            "TLS_CHECK_SECRET": "test-tls-secret",
            "OPS_METRICS_SECRET": "test-metrics-secret",
            "MEDIA_PERSIST_LOCAL": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    env.update(overrides)
    return env


def test_returnable_quantity_includes_approved_status(django_user_model):
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="r6-return-qty")
    from shop.services.payments import confirm_payment

    payment = authorize_payment(attempt, idempotency_key="r6-return-pay")
    order = confirm_payment(payment, idempotency_key="r6-return-confirm")
    item = order.items.first()
    rr = ReturnRequest.objects.create(order=order, reason="test", tenant=order.tenant)
    ReturnLine.objects.create(return_request=rr, order_item=item, quantity=1)
    rr.status = ReturnRequest.Status.APPROVED
    rr.save(update_fields=["status", "updated_at"])
    assert _returnable_quantity(item) == 0


def test_expire_other_attempts_rechecks_status_under_lock():
    variant = make_variant(quantity=4)
    cart = make_cart(variant)
    first = begin_checkout(cart, idempotency_key="r6-toctou-first")
    second = begin_checkout(cart, idempotency_key="r6-toctou-second")
    first.refresh_from_db()
    assert first.status == CheckoutAttempt.Status.EXPIRED
    assert second.status in {
        CheckoutAttempt.Status.STARTED,
        CheckoutAttempt.Status.RESERVED,
    }


def test_api_cart_delete_accepts_query_param(client):
    variant = make_variant()
    add_url = reverse("api-cart-items")
    client.post(add_url, {"variant_id": variant.pk, "quantity": 1}, content_type="application/json")
    delete_url = f"{add_url}?variant_id={variant.pk}"
    resp = client.delete(delete_url)
    assert resp.status_code == 200
    assert resp.json()["item_count"] == 0


def test_api_begin_checkout_requires_shipping_fields(client):
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    resp = client.post(
        reverse("api-checkout-attempts"),
        {"shipping_method": "Standard", "email": "guest@example.com"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="r6-ship-req",
    )
    assert resp.status_code == 400
    assert "address1" in resp.json()["field_errors"]


def test_web_attempt_mismatch_returns_404(client):
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="r6-web-404", contact={"email": "a@test.com"})
    other = client.get(reverse("checkout:payment", args=[attempt.pk]))
    assert other.status_code == 404


@override_settings(ALLOWED_HOSTS=["*"], TENANT_PLATFORM_DOMAINS=["platform.test"])
def test_subdomain_slug_fallback_does_not_match_nested_host(client):
    Tenant.objects.create(slug="acme", name="Acme", active=True)
    resp = client.get("/", HTTP_HOST="evil.acme.platform.test")
    assert resp.status_code == 404


@override_settings(TLS_CHECK_SECRET="secret-token")
def test_tls_check_requires_shared_secret(client):
    Tenant.objects.create(slug="tls", name="TLS", active=True, primary_domain="shop.tls.test")
    assert client.get("/internal/tls-check/", {"domain": "shop.tls.test"}).status_code == 403
    assert (
        client.get(
            "/internal/tls-check/",
            {"domain": "shop.tls.test", "secret": "secret-token"},
        ).status_code
        == 200
    )


def test_payment_rejects_cross_tenant_checkout_attempt():
    variant = make_variant()
    attempt = begin_checkout(make_cart(variant), idempotency_key="r6-pay-tenant")
    other = Tenant.objects.create(slug="pay-other", name="Pay Other", active=True)
    payment = Payment(
        checkout_attempt=attempt,
        amount=Decimal("10.00"),
        idempotency_key="r6-cross",
        tenant=other,
    )
    with pytest.raises(ValidationError, match="Checkout attempt tenant"):
        payment.save()


def test_order_item_rejects_cross_tenant_variant():
    from shop.models import Product

    variant = make_variant()
    attempt = begin_checkout(make_cart(variant), idempotency_key="r6-item-tenant")
    from shop.services.payments import confirm_payment

    payment = authorize_payment(attempt, idempotency_key="r6-item-pay")
    order = confirm_payment(payment, idempotency_key="r6-item-confirm")
    other = Tenant.objects.create(slug="item-other", name="Item Other", active=True)
    foreign_product = Product.objects.create(tenant=other, name="Foreign", slug="foreign-prod")
    foreign_variant = ProductVariant.objects.create(
        tenant=other,
        product=foreign_product,
        sku="FOREIGN-SKU",
        title="Foreign",
        price=Decimal("1.00"),
        quantity=1,
    )
    item = OrderItem(
        order=order,
        variant=foreign_variant,
        sku=foreign_variant.sku,
        product_name="X",
        quantity=1,
        unit_price=Decimal("1.00"),
        line_total=Decimal("1.00"),
    )
    with pytest.raises(ValidationError, match="Product variant tenant"):
        item.save()


def test_production_rejects_gateway_test_modes_enabled():
    env = _prod_subprocess_env(
        DATABASE_URL="postgres://commerce:commerce@localhost:5432/commerce",
        GATEWAY_TEST_MODES_ENABLED="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    assert "GATEWAY_TEST_MODES_ENABLED" in result.stderr


def test_production_rejects_console_email_backend():
    env = _prod_subprocess_env(
        DATABASE_URL="postgres://commerce:commerce@localhost:5432/commerce",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    assert "Console email" in result.stderr


@override_settings(
    IS_PRODUCTION=True,
    SECURE_SSL_REDIRECT=True,
    ALLOWED_HOSTS=["example.com"],
    CSRF_TRUSTED_ORIGINS=[],
)
def test_csrf_origins_include_tenant_primary_domain():
    from shop.csrf_origins import extend_csrf_trusted_origins

    Tenant.objects.create(
        slug="csrf-store",
        name="CSRF Store",
        active=True,
        primary_domain="shop.csrf-store.test",
    )
    extend_csrf_trusted_origins()
    from django.conf import settings

    assert "https://shop.csrf-store.test" in settings.CSRF_TRUSTED_ORIGINS


def test_backup_db_lock_uses_extended_ttl():
    from shop.locks import COMMAND_LOCK_TTLS

    assert COMMAND_LOCK_TTLS["backup_db"] >= 3600


def test_seed_demo_rejected_in_production_subprocess():
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    env.update(
        {
            "DJANGO_ENV": "production",
            "DJANGO_SECRET_KEY": "test-secret",
            "DJANGO_ALLOWED_HOSTS": "example.com",
            "DJANGO_SITE_URL": "https://example.com",
            "DATABASE_URL": f"sqlite:///{ROOT / 'tmp-seed-demo-test.db'}",
            "CACHE_URL": "redis://127.0.0.1:6379/0",
            "DJANGO_EMAIL_HOST": "smtp.example.com",
            "TLS_CHECK_SECRET": "seed-secret",
            "MEDIA_PERSIST_LOCAL": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "seed_demo"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    assert "production" in (result.stderr + result.stdout).lower()


def test_seed_demo_rejected_when_is_production():
    from django.conf import settings

    with patch.object(settings, "IS_PRODUCTION", True):
        with pytest.raises(CommandError, match="production"):
            call_command("seed_demo")


@override_settings(GATEWAY_TEST_MODES_ENABLED=False)
def test_confirm_payment_serializer_omits_test_modes():
    from shop.serializers import ConfirmPaymentSerializer

    serializer = ConfirmPaymentSerializer()
    assert "authorize_mode" not in serializer.fields
    assert "confirm_mode" not in serializer.fields
