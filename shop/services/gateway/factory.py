"""Gateway factory — selects the active PaymentGateway implementation."""
from __future__ import annotations

from django.conf import settings

from .protocol import PaymentGateway
from .simulated import SimulatedPaymentGateway

_REGISTRY: dict[str, type] = {
    "simulated": SimulatedPaymentGateway,
}


def get_payment_gateway(*, provider: str | None = None, tenant_id: int | None = None) -> PaymentGateway:
    """Return the configured gateway adapter (simulated by default)."""
    del tenant_id  # reserved for per-tenant PSP credentials
    name = (provider or getattr(settings, "PAYMENT_GATEWAY", "simulated")).strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown payment gateway provider: {name!r}")
    return cls()
