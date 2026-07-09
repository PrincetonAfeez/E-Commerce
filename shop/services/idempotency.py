# Idempotency record locking and replay for safe retried API mutations
from __future__ import annotations

import hashlib
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from shop.models import IdempotencyRecord

from .exceptions import IdempotencyInProgress


def actor_hash(*, user=None, session_key: str = "") -> str:
    raw = f"user:{getattr(user, 'pk', '')}:session:{session_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


RECORD_TTL = timedelta(hours=24)
LOCK_TTL = timedelta(seconds=30)


def begin(scope: str, key: str, *, user=None, session_key: str = "", payload: str = "") -> IdempotencyRecord:
    now = timezone.now()
    owner = actor_hash(user=user, session_key=session_key)
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(
                scope=scope,
                key=key,
                actor_hash=owner,
                request_hash=request_hash(payload),
                expires_at=now + RECORD_TTL,
                locked_until=now + LOCK_TTL,
            )
    except IntegrityError:
        pass

    # A record already exists. Resolve replay / in-progress / reclaim under a row lock.
    with transaction.atomic():
        record = IdempotencyRecord.objects.select_for_update().get(scope=scope, key=key, actor_hash=owner)
        expired = record.expires_at is not None and record.expires_at <= now
        terminal = record.status in {IdempotencyRecord.Status.COMPLETED, IdempotencyRecord.Status.FAILED}

        # Terminal + within TTL: replay the winner's stored result (spec §21 / ADR-0017).
        if terminal and not expired:
            return record

        # In-progress with a live lock: a twin is genuinely running -> clean 409.
        lock_live = record.locked_until is not None and record.locked_until > now
        if not terminal and lock_live:
            raise IdempotencyInProgress(
                "An operation with this idempotency key is already in progress."
            )

        # Otherwise the record is stale (expired TTL, or an in-progress lock whose holder
        # died). Reclaim it so this caller can retry instead of being blocked forever.
        record.status = IdempotencyRecord.Status.IN_PROGRESS
        record.request_hash = request_hash(payload)
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
    record.status = IdempotencyRecord.Status.COMPLETED
    record.response_status = status
    record.response_body = body
    record.save(update_fields=["status", "response_status", "response_body", "updated_at"])
    return record


def fail(record: IdempotencyRecord, *, status: int, body: dict) -> IdempotencyRecord:
    record.status = IdempotencyRecord.Status.FAILED
    record.response_status = status
    record.response_body = body
    record.save(update_fields=["status", "response_status", "response_body", "updated_at"])
    return record


def abandon(record: IdempotencyRecord) -> IdempotencyRecord:
    """Release the lock without caching a result (used for unexpected failures).

    Keeps the record IN_PROGRESS but expires its lock so the next request reclaims it
    immediately instead of waiting out the lock TTL or being cached as a failure.
    """
    record.locked_until = timezone.now()
    record.save(update_fields=["locked_until", "updated_at"])
    return record
