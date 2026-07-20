"""Round 5 config/infra/model remediation tests"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from shop.models import (
    Cart,
    CheckoutAttempt,
    Collection,
    Product,
    Reservation,
    Tenant,
)
from shop.services.checkout import begin_checkout
from shop.services.inventory import expire_reservations
from shop.services.payments import authorize_payment
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
            "MEDIA_PERSIST_LOCAL": "1",
            "TLS_CHECK_SECRET": "test-tls-secret",
            "OPS_METRICS_SECRET": "test-metrics-secret",
            "PYTHONPATH": str(ROOT),
        }
    )
    env.update(overrides)
    return env


def test_production_rejects_sqlite_database_url():
    env = _prod_subprocess_env(DATABASE_URL="sqlite:////tmp/prod-sqlite-test.db")
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
    assert "SQLite" in result.stderr or "SQLite" in result.stdout


def test_production_require_tenant_context_defaults_true():
    env = _prod_subprocess_env(
        DATABASE_URL="postgres://commerce:commerce@localhost:5432/commerce",
    )
    env.pop("REQUIRE_TENANT_CONTEXT", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
            "django.setup(); from django.conf import settings; assert settings.REQUIRE_TENANT_CONTEXT is True",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr


def test_production_rejects_missing_aws_credentials():
    env = _prod_subprocess_env(
        DATABASE_URL="postgres://commerce:commerce@localhost:5432/commerce",
        AWS_STORAGE_BUCKET_NAME="prod-media-bucket",
    )
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
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
    assert "AWS_ACCESS_KEY_ID" in result.stderr


@override_settings(ALLOWED_HOSTS=["*"], TENANT_PLATFORM_DOMAINS=["platform.test"])
def test_platform_host_root_returns_404(client):
    resp = client.get("/", HTTP_HOST="platform.test")
    assert resp.status_code == 404


def test_expire_other_attempts_skips_payment_pending():
    variant = make_variant(quantity=4)
    cart = make_cart(variant)
    first = begin_checkout(cart, idempotency_key="r5-first-co")
    authorize_payment(first, idempotency_key="r5-first-pay")
    first.refresh_from_db()
    assert first.status == CheckoutAttempt.Status.PAYMENT_PENDING

    second = begin_checkout(cart, idempotency_key="r5-second-co")
    first.refresh_from_db()
    assert first.status == CheckoutAttempt.Status.PAYMENT_PENDING
    assert Reservation.objects.filter(checkout_attempt=first, status=Reservation.Status.ACTIVE).exists()
    assert second.status in {
        CheckoutAttempt.Status.STARTED,
        CheckoutAttempt.Status.RESERVED,
    }


def test_expiry_sweep_skips_payment_pending_attempt():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="r5-expiry-co")
    authorize_payment(attempt, idempotency_key="r5-expiry-pay")
    past = timezone.now() - timedelta(minutes=5)
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(expires_at=past)
    Reservation.objects.filter(checkout_attempt=attempt).update(expires_at=past)

    expired = expire_reservations(now=timezone.now())

    assert expired == 0
    assert Reservation.objects.get(checkout_attempt=attempt).status == Reservation.Status.ACTIVE


def test_product_rejects_cross_tenant_collection():
    tenant_a = Tenant.objects.get(slug="default")
    tenant_b = Tenant.objects.create(slug="other-store", name="Other Store", active=True)
    product = Product.objects.create(tenant=tenant_a, name="A Product", slug="a-product")
    foreign = Collection.objects.create(tenant=tenant_b, name="Foreign", slug="foreign")

    with pytest.raises(ValidationError, match="Collection tenant"):
        product.collections.add(foreign)


def test_reservation_rejects_cross_tenant_cart():
    variant = make_variant()
    tenant_b = Tenant.objects.create(slug="other-cart", name="Other Cart Store", active=True)
    foreign_cart = Cart.objects.create(
        tenant=tenant_b,
        session_key=f"sess-{uuid.uuid4().hex[:8]}",
        status=Cart.Status.ACTIVE,
    )
    attempt = begin_checkout(make_cart(variant), idempotency_key="r5-resv-co")

    reservation = Reservation(
        variant=variant,
        cart=foreign_cart,
        checkout_attempt=attempt,
        quantity=1,
        expires_at=attempt.expires_at,
    )
    with pytest.raises(ValidationError, match="Cart tenant"):
        reservation.save()


def test_dockerfile_collectstatic_env_vars():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "MEDIA_PERSIST_LOCAL=1" in dockerfile
    assert "DJANGO_EMAIL_HOST=localhost" in dockerfile


def test_docker_compose_web_restart_and_worker_resilience():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    web_block = compose.split("web:")[1].split("worker:")[0]
    backup_block = compose.split("backup:")[1]
    assert "restart: unless-stopped" in web_block
    assert "|| true" not in compose
    assert "worker-heartbeat" in compose
    assert "CACHE_URL: redis://redis:6379/0" in backup_block
    assert "redis:" in backup_block.split("depends_on:")[1]
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "DJANGO_ENV: production" in prod


@pytest.mark.parametrize(
    "command,args",
    [
        ("expire_reservations", []),
        ("reconcile_payments", []),
        ("process_outbox", []),
        ("deliver_webhooks", []),
        ("recover_abandoned_carts", []),
        ("run_billing", []),
        ("run_subscription_billing", []),
        ("cleanup_retention", ["--days", "365"]),
    ],
)
def test_management_command_smoke(command, args):
    call_command(command, *args)


def test_single_instance_lock_uses_redis_cache():
    cache.clear()
    from shop.locks import single_instance

    with patch.object(cache, "add", return_value=True) as mock_add:
        with single_instance("r5-redis-lock") as acquired:
            assert acquired is True
        mock_add.assert_called_once()
