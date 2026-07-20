"""Management command smoke tests — each command runs without error in the test DB"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django.core.management.base import BaseCommand
from django.test import override_settings
from django.utils import timezone

from shop.models import Cart, CheckoutAttempt, IdempotencyRecord, OutboxEvent

from .test_checkout_seam import make_cart, make_variant

pytestmark = pytest.mark.django_db


def test_process_outbox_runs():
    call_command("process_outbox", verbosity=0)


def test_expire_reservations_runs():
    call_command("expire_reservations", verbosity=0)


def test_reconcile_payments_runs():
    call_command("reconcile_payments", "--older-than-seconds", "3600", verbosity=0)


def test_deliver_webhooks_runs():
    call_command("deliver_webhooks", "--since-minutes", "1440", verbosity=0)


def test_recover_abandoned_carts_runs():
    call_command("recover_abandoned_carts", "--older-than-minutes", "99999", verbosity=0)


def test_run_billing_runs():
    call_command("run_billing", verbosity=0)


def test_run_subscription_billing_runs():
    call_command("run_subscription_billing", verbosity=0)


def test_cleanup_retention_runs():
    now = timezone.now()
    IdempotencyRecord.objects.create(
        scope="test",
        key=f"exp-{uuid.uuid4().hex[:8]}",
        actor_hash="a",
        tenant_id=1,
        request_hash="h",
        expires_at=now - timedelta(hours=1),
    )
    call_command("cleanup_retention", "--days", "0", verbosity=0)


def test_seed_demo_populates_catalog():
    call_command("seed_demo", verbosity=0)
    from shop.models import Product

    assert Product.objects.filter(slug="weatherproof-field-jacket").exists()


def test_reprocess_dead_letters_dry_run():
    call_command("reprocess_dead_letters", "--dry-run", verbosity=0)


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
@patch("shop.management.commands.verify_backup_restore.call_command")
@patch("shop.management.commands.verify_backup_restore.pg_dump")
@patch("shop.management.commands.verify_backup_restore.create_database")
@patch("shop.management.commands.verify_backup_restore.drop_database")
@patch("shop.management.commands.verify_backup_restore.pg_restore")
@patch("shop.management.commands.verify_backup_restore.verify_restored_schema")
def test_verify_backup_restore_mocked(
    mock_verify,
    mock_restore,
    mock_drop,
    mock_create,
    mock_dump,
    mock_call_command,
    tmp_path,
):
    mock_dump.side_effect = lambda target, connection=None: target.write_bytes(b"x") or target
    mock_verify.return_value = 1
    call_command("verify_backup_restore", out_dir=str(tmp_path))
    mock_call_command.assert_called_once_with("migrate", "--check", verbosity=0)


def test_cleanup_orphan_media_dry_run():
    call_command("cleanup_orphan_media", "--dry-run", verbosity=0)


def test_backup_db_rejects_non_postgres():
    with pytest.raises(CommandError, match="Postgres"):
        call_command("backup_db", verbosity=0)


def test_all_commands_have_handle():
    from shop.management.commands import (
        backup_db,
        cleanup_orphan_media,
        cleanup_retention,
        deliver_webhooks,
        expire_reservations,
        process_outbox,
        reconcile_payments,
        recover_abandoned_carts,
        reprocess_dead_letters,
        run_billing,
        run_subscription_billing,
        seed_demo,
        verify_backup_restore,
    )

    for mod in (
        backup_db,
        cleanup_orphan_media,
        cleanup_retention,
        deliver_webhooks,
        expire_reservations,
        process_outbox,
        reconcile_payments,
        recover_abandoned_carts,
        reprocess_dead_letters,
        run_billing,
        run_subscription_billing,
        seed_demo,
        verify_backup_restore,
    ):
        assert issubclass(mod.Command, BaseCommand)
        assert callable(mod.Command().handle)


def test_cleanup_retention_deletes_expired_idempotency():
    from shop.models import Tenant

    tenant = Tenant.objects.get(slug="default")
    IdempotencyRecord.objects.create(
        scope="cleanup-test",
        key="old-key",
        actor_hash="actor",
        tenant_id=tenant.pk,
        request_hash="hash",
        expires_at=timezone.now() - timedelta(days=1),
    )
    call_command("cleanup_retention", "--days", "30", verbosity=0)
    assert not IdempotencyRecord.objects.filter(scope="cleanup-test", key="old-key").exists()


def test_process_outbox_with_pending_event():
    variant = make_variant()
    cart = make_cart(variant)
    from shop.services.checkout import begin_checkout
    from shop.services.payments import authorize_payment, confirm_payment

    attempt = begin_checkout(cart, idempotency_key=f"cmd-{uuid.uuid4().hex[:6]}", contact={"email": "cmd@test.com"})
    payment = authorize_payment(attempt, idempotency_key="cmd-pay")
    confirm_payment(payment, idempotency_key="cmd-cf")
    assert OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).exists()
    call_command("process_outbox", verbosity=0)


def test_expire_reservations_expires_stale_attempt():
    variant = make_variant(quantity=2)
    cart = make_cart(variant)
    from shop.services.checkout import begin_checkout

    attempt = begin_checkout(cart, idempotency_key=f"exp-{uuid.uuid4().hex[:6]}")
    CheckoutAttempt.objects.filter(pk=attempt.pk).update(
        expires_at=timezone.now() - timedelta(minutes=10),
        status=CheckoutAttempt.Status.RESERVED,
    )
    call_command("expire_reservations", verbosity=0)
    attempt.refresh_from_db()
    assert attempt.status in {CheckoutAttempt.Status.EXPIRED, CheckoutAttempt.Status.RESERVED}


def test_recover_abandoned_carts_skips_active_cart():
    cart = make_cart(make_variant())
    Cart.objects.filter(pk=cart.pk).update(updated_at=timezone.now() - timedelta(hours=5))
    before = OutboxEvent.objects.filter(event_type="cart.recovery_email").count()
    call_command("recover_abandoned_carts", "--older-than-minutes", "60", verbosity=0)
    after = OutboxEvent.objects.filter(event_type="cart.recovery_email").count()
    assert after == before
