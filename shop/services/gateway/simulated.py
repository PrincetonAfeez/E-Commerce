"""Simulated payment gateway — reference implementation of PaymentGateway."""
from __future__ import annotations

import hashlib
import hmac
import uuid
from decimal import Decimal

from django.conf import settings

from shop.models import SimulatedGatewayIntent

from ..exceptions import PaymentGatewayError
from .protocol import AUTHORIZED, CONFIRMED, FAILED, GatewayResult


class SimulatedPaymentGateway:
    provider = "simulated"

    def authorize(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        card_token: str = "tok_visa",
        mode: str = "approve",
        tenant_scope: str = "",
    ) -> GatewayResult:
        scope_key = f"{tenant_scope}:{idempotency_key}" if tenant_scope else idempotency_key
        reference = f"sim_{uuid.uuid5(uuid.NAMESPACE_URL, scope_key).hex[:24]}"
        if card_token in {"tok_decline", "decline"} or mode == "decline":
            self._record_intent(reference, FAILED, amount, currency)
            return GatewayResult(
                gateway_reference=reference,
                status=FAILED,
                amount=amount,
                currency=currency,
                provider_status=FAILED,
                failure_code="card_declined",
                payload={"mode": mode},
                provider_event_id=f"{reference}:authorized",
            )
        provider_status = CONFIRMED if mode == "dropped_confirmation" else AUTHORIZED
        self._record_intent(reference, provider_status, amount, currency)
        return GatewayResult(
            gateway_reference=reference,
            status=AUTHORIZED,
            amount=amount,
            currency=currency,
            provider_status=provider_status,
            payload={"mode": mode},
            provider_event_id=f"{reference}:authorized",
        )

    def confirm(
        self,
        *,
        gateway_reference: str,
        idempotency_key: str,
        mode: str = "approve",
    ) -> GatewayResult:
        intent = SimulatedGatewayIntent.objects.filter(gateway_reference=gateway_reference).first()
        if not intent:
            raise PaymentGatewayError("Gateway reference was not found.")
        if mode == "decline" or intent.status == FAILED:
            self._record_intent(gateway_reference, FAILED, intent.amount, intent.currency)
            return GatewayResult(
                gateway_reference=gateway_reference,
                status=FAILED,
                amount=intent.amount,
                currency=intent.currency,
                provider_status=FAILED,
                failure_code="confirmation_failed",
                payload={"mode": mode},
                provider_event_id=f"{gateway_reference}:confirmed:{idempotency_key}",
            )
        self._record_intent(gateway_reference, CONFIRMED, intent.amount, intent.currency)
        return GatewayResult(
            gateway_reference=gateway_reference,
            status=CONFIRMED,
            amount=intent.amount,
            currency=intent.currency,
            provider_status=CONFIRMED,
            payload={"mode": mode, "confirmation_idempotency_key": idempotency_key},
            provider_event_id=f"{gateway_reference}:confirmed:{idempotency_key}",
        )

    def refund(
        self,
        *,
        gateway_reference: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        tenant_scope: str = "",
    ) -> GatewayResult:
        dedupe_ref = f"refund_{tenant_scope}:{idempotency_key}" if tenant_scope else f"refund_{idempotency_key}"
        existing = SimulatedGatewayIntent.objects.filter(gateway_reference=dedupe_ref).first()
        if existing:
            return GatewayResult(
                gateway_reference=dedupe_ref,
                status=CONFIRMED,
                amount=existing.amount,
                currency=existing.currency,
                provider_status=CONFIRMED,
                safe_display="Simulated refund",
                payload={"payment_reference": gateway_reference, "replay": True},
                provider_event_id=f"{dedupe_ref}:refund",
            )
        self._record_intent(dedupe_ref, CONFIRMED, amount, currency)
        return GatewayResult(
            gateway_reference=dedupe_ref,
            status=CONFIRMED,
            amount=amount,
            currency=currency,
            provider_status=CONFIRMED,
            safe_display="Simulated refund",
            payload={"payment_reference": gateway_reference},
            provider_event_id=f"{dedupe_ref}:refund",
        )

    def get_payment_status(self, gateway_reference: str) -> str:
        intent = SimulatedGatewayIntent.objects.filter(gateway_reference=gateway_reference).first()
        if not intent:
            raise PaymentGatewayError("Gateway reference was not found.")
        return intent.status

    def verify_webhook_signature(self, *, body: bytes, signature: str, tenant_id: int | None = None) -> bool:
        secret = getattr(settings, "PAYMENT_WEBHOOK_SECRET", "")
        if not secret:
            return bool(getattr(settings, "DEBUG", False) and not getattr(settings, "IS_PRODUCTION", False))
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(self, payload: dict) -> dict:
        return {
            "event_id": payload.get("event_id", ""),
            "event_type": payload.get("event_type", ""),
            "gateway_reference": payload.get("gateway_reference", ""),
            "status": payload.get("status", ""),
            "tenant_id": payload.get("tenant_id"),
        }

    def build_webhook_payload(
        self,
        *,
        gateway_reference: str,
        event_type: str,
        tenant_id: int,
        status: str = CONFIRMED,
    ) -> dict:
        event_id = f"{gateway_reference}:{event_type}:{uuid.uuid4().hex[:8]}"
        return {
            "event_id": event_id,
            "event_type": event_type,
            "gateway_reference": gateway_reference,
            "status": status,
            "tenant_id": tenant_id,
            "provider": self.provider,
        }

    def sign_webhook_body(self, body: bytes) -> str:
        secret = getattr(settings, "PAYMENT_WEBHOOK_SECRET", "")
        if not secret:
            return ""
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    @staticmethod
    def _record_intent(reference: str, status: str, amount: Decimal, currency: str) -> None:
        SimulatedGatewayIntent.objects.update_or_create(
            gateway_reference=reference,
            defaults={"status": status, "amount": amount, "currency": currency},
        )
