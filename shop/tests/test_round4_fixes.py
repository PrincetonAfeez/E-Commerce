"""Round 4 config/infra/command/doc remediation tests"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from shop.locks import single_instance
from shop.models import EmailDelivery, OutboxEvent, WebhookDelivery, WebhookEndpoint
from shop.services.checkout import begin_checkout
from shop.services.payments import authorize_payment, confirm_payment
from shop.tests.test_checkout_seam import make_cart, make_variant

pytestmark = pytest.mark.django_db


def test_single_instance_lock_prevents_overlap():
    cache.clear()
    with single_instance("test-lock") as first:
        assert first is True
        with single_instance("test-lock") as second:
            assert second is False


def test_production_settings_requires_aws_bucket_or_local_override():
    base_env = {
        "DJANGO_ENV": "production",
        "DJANGO_SECRET_KEY": "test-secret",
        "DJANGO_ALLOWED_HOSTS": "example.com",
        "DJANGO_SITE_URL": "https://example.com",
        "CACHE_URL": "redis://127.0.0.1:6379/0",
        "DJANGO_EMAIL_HOST": "smtp.example.com",
        "TLS_CHECK_SECRET": "test-tls-secret",
        "OPS_METRICS_SECRET": "test-metrics-secret",
        "DATABASE_URL": "postgres://commerce:commerce@localhost:5432/commerce",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    env = os.environ.copy()
    env.update(base_env)
    env.pop("AWS_STORAGE_BUCKET_NAME", None)
    env.pop("MEDIA_PERSIST_LOCAL", None)
    env.pop("PYTEST_CURRENT_TEST", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
            "django.setup(); from django.conf import settings; assert settings.IS_PRODUCTION",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode != 0, result.stderr

    env["MEDIA_PERSIST_LOCAL"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
            "django.setup(); from django.conf import settings; assert settings.IS_PRODUCTION",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, result.stderr


@override_settings(IS_PRODUCTION=True, RUNNING_TESTS=False)
def test_healthz_returns_503_when_cache_down_in_production(client, monkeypatch):
    monkeypatch.setattr(
        "django.core.cache.cache.set",
        MagicMock(side_effect=ConnectionError("redis down")),
    )
    resp = client.get("/healthz/")
    assert resp.status_code == 503
    assert resp.json()["cache"] == "down"


@override_settings(IS_PRODUCTION=False)
def test_healthz_ok_when_cache_up(client):
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    assert resp.json()["cache"] == "ok"


@override_settings(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "commerce",
            "HOST": "localhost",
            "PORT": "5432",
            "USER": "",
            "PASSWORD": "",
        }
    }
)
@patch("shop.services.db_backup.subprocess.run")
def test_backup_db_writes_dump(mock_run, tmp_path):

    def _write_dump(argv, env=None, check=None):
        out_file = Path(argv[argv.index("--file") + 1])
        out_file.write_bytes(b"dump")

    mock_run.side_effect = _write_dump
    call_command("backup_db", out_dir=str(tmp_path), retention_days=30)
    dumps = list(tmp_path.glob("*.dump"))
    assert len(dumps) == 1
    mock_run.assert_called_once()


def test_backup_db_prunes_old_dumps(tmp_path):
    from datetime import datetime, timedelta, timezone

    from shop.management.commands.backup_db import Command

    old_dump = tmp_path / "commerce-old.dump"
    new_dump = tmp_path / "commerce-new.dump"
    old_dump.write_bytes(b"old")
    new_dump.write_bytes(b"new")

    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    os.utime(old_dump, (old_ts, old_ts))

    cmd = Command()
    pruned = cmd._prune_old_dumps(tmp_path, retention_days=30)
    assert pruned == 1
    assert not old_dump.exists()
    assert new_dump.exists()


def test_cleanup_retention_prunes_delivery_logs():
    cutoff = timezone.now() - timedelta(days=31)
    event = OutboxEvent.objects.create(
        event_type="order.confirmation_email",
        aggregate_type="Order",
        aggregate_id="1",
        status=OutboxEvent.Status.SENT,
    )
    OutboxEvent.objects.filter(pk=event.pk).update(created_at=cutoff)
    email = EmailDelivery.objects.create(
        template="order.confirmation",
        status=EmailDelivery.Status.SENT,
    )
    EmailDelivery.objects.filter(pk=email.pk).update(created_at=cutoff)
    endpoint = WebhookEndpoint.objects.create(url="https://example.com/hook", secret="sek")
    webhook = WebhookDelivery.objects.create(
        endpoint=endpoint,
        event_type="order.placed",
        status=WebhookDelivery.Status.SUCCESS,
    )
    WebhookDelivery.objects.filter(pk=webhook.pk).update(created_at=cutoff)

    call_command("cleanup_retention", days=30)

    assert not OutboxEvent.objects.filter(pk=event.pk).exists()
    assert not EmailDelivery.objects.filter(pk=email.pk).exists()
    assert not WebhookDelivery.objects.filter(pk=webhook.pk).exists()


def test_guest_order_lookup_api_anti_enumeration(client):
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key=f"r4-guest-{uuid.uuid4().hex[:8]}")
    attempt.guest_email = "guest@example.com"
    attempt.save(update_fields=["guest_email"])
    payment = authorize_payment(attempt, idempotency_key=f"r4-guest-pay-{uuid.uuid4().hex[:8]}")
    order = confirm_payment(payment, idempotency_key=f"r4-guest-cf-{uuid.uuid4().hex[:8]}")
    order.guest_email = "guest@example.com"
    order.user = None
    order.save(update_fields=["guest_email", "user"])

    valid = client.post(
        reverse("api-guest-order-lookup"),
        {"email": "guest@example.com", "order_number": order.order_number},
        content_type="application/json",
    )
    invalid = client.post(
        reverse("api-guest-order-lookup"),
        {"email": "wrong@example.com", "order_number": order.order_number},
        content_type="application/json",
    )
    missing = client.post(
        reverse("api-guest-order-lookup"),
        {"email": "nobody@example.com", "order_number": "NO-SUCH-ORDER"},
        content_type="application/json",
    )

    assert valid.status_code == 200
    assert invalid.status_code == 200
    assert missing.status_code == 200
    assert valid.json()["message"] == invalid.json()["message"] == missing.json()["message"]


def test_guest_order_lookup_throttle_configured():
    from django.conf import settings

    assert "guest_order_lookup" in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]


def test_guest_order_lookup_api_throttled(client, monkeypatch):
    from rest_framework.throttling import ScopedRateThrottle

    cache.clear()
    monkeypatch.setattr(
        ScopedRateThrottle,
        "THROTTLE_RATES",
        {"guest_order_lookup": "2/min"},
        raising=False,
    )
    url = reverse("api-guest-order-lookup")
    payload = {"email": f"{uuid.uuid4().hex}@example.com", "order_number": "NONE"}
    statuses = [client.post(url, payload, content_type="application/json").status_code for _ in range(5)]
    assert 429 in statuses
