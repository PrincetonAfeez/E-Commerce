"""Environment-backed feature flags for safe rollouts (GROWTH: feature-flag mechanism)."""
from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Documented flags — override via FF_<NAME>=1 in the environment.
_DEFAULTS: dict[str, bool] = {
    "SELF_SERVE_SIGNUP": True,
    "ABANDONED_CART_RECOVERY": True,
    "SUBSCRIPTION_BILLING": True,
}


def is_enabled(name: str, *, default: bool | None = None) -> bool:
    """Return whether an env-backed feature flag is enabled."""
    key = f"FF_{name.upper()}"
    raw = os.environ.get(key)
    if raw is None:
        if default is not None:
            return default
        return _DEFAULTS.get(name.upper(), False)
    return raw.strip().lower() in _TRUTHY


def enabled_flags() -> dict[str, bool]:
    """Snapshot of all known flags and their effective values."""
    known = set(_DEFAULTS) | {key.removeprefix("FF_") for key in os.environ if key.startswith("FF_")}
    return {name: is_enabled(name) for name in sorted(known)}
