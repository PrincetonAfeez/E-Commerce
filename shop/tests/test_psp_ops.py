"""PSP integration seam and operational hardening tests"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import override_settings
from django.urls import reverse

from shop.services.gateway import SimulatedPaymentGateway, get_payment_gateway
from shop.services.ops_metrics import collect_ops_metrics
from shop.tests.test_checkout_seam import make_variant

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]


def test_get_payment_gateway_returns_simulated_by_default():
    gw = get_payment_gateway()
    assert gw.provider == "simulated"
    assert isinstance(gw, SimulatedPaymentGateway)


@override_settings(PAYMENT_WEBHOOK_SECRET="whsec-test")
def test_simulated_webhook_signature_round_trip():
    gw = SimulatedPaymentGateway()
    body = json.dumps({"event_id": "evt_1", "event_type": "payment.confirmed"}).encode()
    sig = gw.sign_webhook_body(body)
    assert gw.verify_webhook_signature(body=body, signature=sig)


def test_readyz_returns_ready(client):
    assert client.get("/readyz/").json() == {"status": "ready"}


@override_settings(OPS_METRICS_SECRET="metrics-secret")
def test_internal_metrics_requires_secret(client):
    assert client.get("/internal/metrics/").status_code == 403
    resp = client.get("/internal/metrics/", HTTP_X_OPS_METRICS_SECRET="metrics-secret")
    assert resp.status_code == 200
    assert "outbox_pending" in resp.json()


def test_collect_ops_metrics_shape():
    metrics = collect_ops_metrics()
    assert "outbox_pending" in metrics
    assert "checkout_attempts_by_status" in metrics


@override_settings(PAYMENT_WEBHOOK_SECRET="whsec-test")
def test_payment_webhook_rejects_bad_signature(client):
    url = reverse("api-payment-webhook", args=["simulated"])
    resp = client.post(
        url,
        data=b"{}",
        content_type="application/json",
        HTTP_X_PAYMENT_SIGNATURE="bad",
    )
    assert resp.status_code == 403


def test_authorize_payment_api_endpoint(client):
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    checkout = client.post(
        reverse("api-checkout-attempts"),
        {
            "shipping_method": "Standard",
            "email": "guest@example.com",
            "address1": "1 Main",
            "city": "Town",
            "postal_code": "12345",
        },
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="psp-auth-co",
    )
    attempt_id = checkout.json()["id"]
    resp = client.post(
        reverse("api-checkout-authorize-payment", args=[attempt_id]),
        {"card_token": "tok_visa"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="psp-auth-pay",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "authorized"
    assert resp.json()["provider"] == "simulated"


def test_production_requires_ops_metrics_secret():
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
            "TLS_CHECK_SECRET": "tls-secret",
            "MEDIA_PERSIST_LOCAL": "1",
            "DATABASE_URL": "postgres://commerce:commerce@localhost:5432/commerce",
            "PYTHONPATH": str(ROOT),
        }
    )
    env.pop("OPS_METRICS_SECRET", None)
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
    assert "OPS_METRICS_SECRET" in result.stderr


def test_worker_loop_script_exists():
    script = ROOT / "scripts" / "worker-loop.sh"
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert "reconcile_payments 60" in content
    assert "run_billing 1440" in content
