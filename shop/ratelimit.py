# Cache-backed per-IP rate limit decorator for Django views outside DRF throttling
from __future__ import annotations

import functools

from django.core.cache import cache
from django.http import HttpResponse

_PERIODS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400}


def parse_rate(rate: str) -> tuple[int, int]:
    """Parse a DRF-style rate string like ``"10/min"`` into ``(limit, window_seconds)``."""
    count, _, period = rate.partition("/")
    return int(count), _PERIODS.get(period.strip().lower(), 60)


def _client_ip(request) -> str:
    # REMOTE_ADDR only: X-Forwarded-For is client-controlled and must not be trusted
    # for a security control unless a vetted proxy sets it.
    return request.META.get("REMOTE_ADDR", "unknown")


def ratelimit(scope: str, rate: str = "10/min", methods=("POST",), field: str | None = None):
    """Fixed-window per-IP (optionally per-identifier) throttle backed by the cache.

    Applied to Django auth views, which DRF throttles do not cover (spec §9/§27.1).
    """
    limit, window = parse_rate(rate)

    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods:
                ident = _client_ip(request)
                if field:
                    ident = f"{ident}:{request.POST.get(field, '')[:150]}"
                key = f"ratelimit:{scope}:{ident}"
                # add() is a no-op if the key exists, so the window starts on first hit.
                cache.add(key, 0, window)
                try:
                    current = cache.incr(key)
                except ValueError:
                    cache.set(key, 1, window)
                    current = 1
                if current > limit:
                    return HttpResponse(
                        "Too many attempts. Please wait a moment and try again.",
                        status=429,
                    )
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
