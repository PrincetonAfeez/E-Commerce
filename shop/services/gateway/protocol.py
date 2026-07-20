"""Payment gateway contract — swappable PSP adapters implement this protocol."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

# Normalized provider status values shared across adapters.
CONFIRMED = "confirmed"
AUTHORIZED = "authorized"
FAILED = "failed"
PENDING = "pending"


@dataclass(frozen=True)
class GatewayResult:
    gateway_reference: str
    status: str
    amount: Decimal
    currency: str
    provider_status: str
    safe_display: str = "Simulated card **** 4242"
    failure_code: str = ""
    payload: dict | None = None
    provider_event_id: str = ""


@runtime_checkable
class PaymentGateway(Protocol):
    """Outbound PSP contract: authorize, confirm, refund, and status polling."""

    provider: str

    def authorize(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        card_token: str = "tok_visa",
        mode: str = "approve",
        tenant_scope: str = "",
    ) -> GatewayResult: ...

    def confirm(
        self,
        *,
        gateway_reference: str,
        idempotency_key: str,
        mode: str = "approve",
    ) -> GatewayResult: ...

    def refund(
        self,
        *,
        gateway_reference: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        tenant_scope: str = "",
    ) -> GatewayResult: ...

    def get_payment_status(self, gateway_reference: str) -> str: ...

    def verify_webhook_signature(self, *, body: bytes, signature: str, tenant_id: int | None = None) -> bool:
        """Return True when the inbound webhook signature is valid for this provider."""
        ...

    def parse_webhook_event(self, payload: dict) -> dict:
        """Normalize a provider webhook body to {event_id, event_type, gateway_reference, status}."""
        ...
