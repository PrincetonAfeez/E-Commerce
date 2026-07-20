"""Idempotency record locking and replay for safe retried API mutations"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from shop.models import IdempotencyRecord
from shop.tenancy import get_current_tenant_id

from .exceptions import IdempotencyInProgress, IdempotencyKeyReuseMismatch


def _tenant_id_for_record() -> int:
    tid = get_current_tenant_id()
    if tid is None:
        raise RuntimeError("Tenant context is required for idempotency operations.")
    return tid


def actor_hash(*, user=None, session_key: str = "", tenant_id: int | None = None) -> str:
    if tenant_id is None:
        tenant_id = _tenant_id_for_record()
    raw = f"tenant:{tenant_id}:user:{getattr(user, 'pk', '')}:session:{session_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


RECORD_TTL = timedelta(hours=24)
LOCK_TTL = timedelta(seconds=30)


def _hash_matches(record: IdempotencyRecord, payload: str) -> bool:
    if not record.request_hash:
        return False
    return record.request_hash == request_hash(payload)


def begin(scope: str, key: str, *, user=None, session_key: str = "", payload: str = "") -> IdempotencyRecord:
    now = timezone.now()
    tenant_id = _tenant_id_for_record()
    owner = actor_hash(user=user, session_key=session_key, tenant_id=tenant_id)
    incoming_hash = request_hash(payload)
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(
                scope=scope,
                key=key,
                actor_hash=owner,
                tenant_id=tenant_id,
                request_hash=incoming_hash,
                expires_at=now + RECORD_TTL,
                locked_until=now + LOCK_TTL,
            )
    except IntegrityError:
        pass

    with transaction.atomic():
        record = IdempotencyRecord.objects.select_for_update().get(
            scope=scope, key=key, actor_hash=owner, tenant_id=tenant_id
        )
        expired = record.expires_at is not None and record.expires_at <= now
        terminal = record.status in {IdempotencyRecord.Status.COMPLETED, IdempotencyRecord.Status.FAILED}

        if terminal and not expired:
            if not _hash_matches(record, payload):
                raise IdempotencyKeyReuseMismatch("Idempotency-Key was reused with a different request body.")
            return record

        lock_live = record.locked_until is not None and record.locked_until > now
        if not terminal and lock_live:
            raise IdempotencyInProgress("An operation with this idempotency key is already in progress.")

        if record.request_hash and not _hash_matches(record, payload):
            raise IdempotencyKeyReuseMismatch("Idempotency-Key was reused with a different request body.")

        record.status = IdempotencyRecord.Status.IN_PROGRESS
        record.request_hash = incoming_hash
        record.response_status = None
        record.response_body = {}
        record.expires_at = now + RECORD_TTL
        record.locked_until = now + LOCK_TTL
        record.save(
            update_fields=[
                "status",
                "request_hash",
                "response_status",
                "response_body",
                "expires_at",
                "locked_until",
                "updated_at",
            ]
        )
        return record


def complete(record: IdempotencyRecord, *, status: int, body: dict) -> IdempotencyRecord:
    with transaction.atomic():
        locked = IdempotencyRecord.objects.select_for_update().get(pk=record.pk)
        locked.status = IdempotencyRecord.Status.COMPLETED
        locked.response_status = status
        locked.response_body = body
        locked.save(update_fields=["status", "response_status", "response_body", "updated_at"])
        return locked


def fail(record: IdempotencyRecord, *, status: int, body: dict) -> IdempotencyRecord:
    with transaction.atomic():
        locked = IdempotencyRecord.objects.select_for_update().get(pk=record.pk)
        locked.status = IdempotencyRecord.Status.FAILED
        locked.response_status = status
        locked.response_body = body
        locked.save(update_fields=["status", "response_status", "response_body", "updated_at"])
        return locked


def abandon(record: IdempotencyRecord) -> IdempotencyRecord:
    """Release the lock without caching a result (used for unexpected failures)."""
    with transaction.atomic():
        locked = IdempotencyRecord.objects.select_for_update().get(pk=record.pk)
        locked.locked_until = timezone.now()
        locked.save(update_fields=["locked_until", "updated_at"])
        return locked
