"""Distributed single-instance locks for background management commands"""
from __future__ import annotations

import contextlib
import time
import uuid

from django.core.cache import cache

# Per-command TTL overrides for long-running jobs (seconds).
COMMAND_LOCK_TTLS: dict[str, int] = {
    "backup_db": 7200,
    "process_outbox": 900,
    "deliver_webhooks": 900,
    "reconcile_payments": 900,
    "cleanup_retention": 1800,
    "run_billing": 600,
    "run_subscription_billing": 600,
}


@contextlib.contextmanager
def single_instance(lock_name: str, *, ttl_seconds: int | None = None, wait_seconds: float = 0):
    """Acquire a cache-backed lock so only one worker runs a job at a time."""
    if ttl_seconds is None:
        ttl_seconds = COMMAND_LOCK_TTLS.get(lock_name, 300)
    token = uuid.uuid4().hex
    key = f"mgmt-lock:{lock_name}"
    deadline = time.monotonic() + wait_seconds
    acquired = False
    while True:
        if cache.add(key, token, ttl_seconds):
            acquired = True
            break
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(0.25)
    try:
        yield True
    finally:
        if acquired and cache.get(key) == token:
            cache.delete(key)
