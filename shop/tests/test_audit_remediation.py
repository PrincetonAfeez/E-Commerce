"""Tests for SaaS audit remediation: backup restore, DLQ, feature flags, load smoke, orphan media"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from shop.feature_flags import enabled_flags, is_enabled
from shop.models import OutboxEvent, ProductImage, WebhookDelivery, WebhookEndpoint
from shop.services.dead_letters import requeue_failed_outbox, requeue_failed_webhooks
from shop.services.ops_metrics import collect_ops_metrics
from shop.tests.test_checkout_seam import make_variant

pytestmark = pytest.mark.django_db


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
@patch("shop.management.commands.verify_backup_restore.pg_dump")
@patch("shop.management.commands.verify_backup_restore.create_database")
@patch("shop.management.commands.verify_backup_restore.drop_database")
@patch("shop.management.commands.verify_backup_restore.pg_restore")
@patch("shop.management.commands.verify_backup_restore.verify_restored_schema")
def test_verify_backup_restore_round_trip(
    mock_verify,
    mock_restore,
    mock_drop,
    mock_create,
    mock_dump,
    tmp_path,
):
    mock_dump.side_effect = lambda target, connection=None: target.write_bytes(b"dump") or target
    mock_verify.return_value = 26

    call_command("verify_backup_restore", out_dir=str(tmp_path))

    mock_dump.assert_called_once()
    mock_create.assert_called_once()
    mock_restore.assert_called_once()
    mock_verify.assert_called_once()
    assert mock_drop.call_count >= 1


@patch("shop.management.commands.verify_backup_restore.resolve_pg_connection")
def test_verify_backup_restore_rejects_non_postgres(mock_resolve):
    mock_resolve.side_effect = CommandError("requires PostgreSQL")
    with pytest.raises(CommandError, match="PostgreSQL"):
        call_command("verify_backup_restore")


def test_dead_letter_requeue_outbox_and_webhooks():
    event = OutboxEvent.objects.create(
        event_type="order.placed",
        aggregate_type="Order",
        aggregate_id="1",
        status=OutboxEvent.Status.FAILED,
        attempts=5,
        last_error="smtp down",
    )
    endpoint = WebhookEndpoint.objects.create(url="https://example.com/hook", secret="sec", active=True)
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint,
        event_type="order.placed",
        status=WebhookDelivery.Status.FAILED,
        attempts=5,
        last_error="timeout",
    )

    assert requeue_failed_outbox(limit=10) == 1
    assert requeue_failed_webhooks(limit=10) == 1

    event.refresh_from_db()
    delivery.refresh_from_db()
    assert event.status == OutboxEvent.Status.PENDING
    assert event.attempts == 0
    assert delivery.status == WebhookDelivery.Status.PENDING
    assert delivery.attempts == 0


def test_reprocess_dead_letters_command_dry_run():
    OutboxEvent.objects.create(
        event_type="x",
        aggregate_type="Order",
        aggregate_id="1",
        status=OutboxEvent.Status.FAILED,
    )
    call_command("reprocess_dead_letters", "--dry-run")


def test_ops_metrics_includes_dead_letter_counts():
    metrics = collect_ops_metrics()
    assert "outbox_failed" in metrics
    assert "webhook_deliveries_failed" in metrics
    assert "feature_flags" in metrics


def test_feature_flags_default_and_override():
    assert is_enabled("SELF_SERVE_SIGNUP") is True
    with patch.dict(os.environ, {"FF_SELF_SERVE_SIGNUP": "0"}):
        assert is_enabled("SELF_SERVE_SIGNUP") is False
    assert "SELF_SERVE_SIGNUP" in enabled_flags()


@override_settings(MEDIA_ROOT="/tmp/test-media")
def test_cleanup_orphan_media_dry_run(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    media_dir = tmp_path / "product-images"
    media_dir.mkdir(parents=True)
    orphan = media_dir / "orphan.png"
    orphan.write_bytes(b"png")

    variant = make_variant()
    referenced = media_dir / "kept.png"
    referenced.write_bytes(b"png")
    ProductImage.objects.create(product=variant.product, image="product-images/kept.png", alt_text="")

    call_command("cleanup_orphan_media", "--dry-run")
    assert orphan.exists()
    assert referenced.exists()


@override_settings(MEDIA_ROOT="/tmp/test-media")
def test_cleanup_orphan_media_deletes_orphans(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    media_dir = tmp_path / "branding"
    media_dir.mkdir(parents=True)
    orphan = media_dir / "old-logo.png"
    orphan.write_bytes(b"png")

    call_command("cleanup_orphan_media")
    assert not orphan.exists()


def test_store_signup_respects_feature_flag(client):
    with patch.dict(os.environ, {"FF_SELF_SERVE_SIGNUP": "0"}):
        resp = client.get(reverse("store_signup"))
        assert resp.status_code == 403


def _load_smoke_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "load_smoke.py"
    spec = importlib.util.spec_from_file_location("load_smoke", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_smoke_script_success(monkeypatch):
    load_smoke = _load_smoke_module()
    monkeypatch.setattr(load_smoke, "_hit", lambda url, timeout=5.0: (200, 0.01))
    assert load_smoke.main(["http://127.0.0.1:8000", "6", "2"]) == 0


def test_load_smoke_script_counts_errors(monkeypatch):
    load_smoke = _load_smoke_module()

    def fake_hit(url, timeout=5.0):
        return (503, 0.01) if "healthz" in url else (200, 0.01)

    monkeypatch.setattr(load_smoke, "_hit", fake_hit)
    assert load_smoke.main(["http://127.0.0.1:8000", "3", "1"]) == 1


def test_settings_exposes_staging_env():
    from django.conf import settings

    assert hasattr(settings, "IS_STAGING")
    assert hasattr(settings, "IS_DEPLOYED")
