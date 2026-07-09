from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shop.models import CheckoutAttempt, OutboxEvent, Payment, PaymentEvent, SimulatedGatewayIntent
from shop.tenancy import clear_current_tenant, set_current_tenant

from .credit import release_hold
from .exceptions import CheckoutStateError, PaymentGatewayError
from .inventory import release_reservations


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


class SimulatedPaymentGateway:
    CONFIRMED = "confirmed"
    AUTHORIZED = "authorized"
    FAILED = "failed"

    def authorize(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        card_token: str = "tok_visa",
        mode: str = "approve",
    ) -> GatewayResult:
        reference = f"sim_{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex[:24]}"
        if card_token in {"tok_decline", "decline"} or mode == "decline":
            self._record_intent(reference, self.FAILED, amount, currency)
            return GatewayResult(
                gateway_reference=reference,
                status=self.FAILED,
                amount=amount,
                currency=currency,
                provider_status=self.FAILED,
                failure_code="card_declined",
                payload={"mode": mode},
            )
        provider_status = self.CONFIRMED if mode == "dropped_confirmation" else self.AUTHORIZED
        self._record_intent(reference, provider_status, amount, currency)
        return GatewayResult(
            gateway_reference=reference,
            status=self.AUTHORIZED,
            amount=amount,
            currency=currency,
            provider_status=provider_status,
            payload={"mode": mode},
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
        if mode == "decline" or intent.status == self.FAILED:
            self._record_intent(gateway_reference, self.FAILED, intent.amount, intent.currency)
            return GatewayResult(
                gateway_reference=gateway_reference,
                status=self.FAILED,
                amount=intent.amount,
                currency=intent.currency,
                provider_status=self.FAILED,
                failure_code="confirmation_failed",
                payload={"mode": mode},
            )
        self._record_intent(gateway_reference, self.CONFIRMED, intent.amount, intent.currency)
        return GatewayResult(
            gateway_reference=gateway_reference,
            status=self.CONFIRMED,
            amount=intent.amount,
            currency=intent.currency,
            provider_status=self.CONFIRMED,
            payload={"mode": mode, "confirmation_idempotency_key": idempotency_key},
        )

    def refund(
        self,
        *,
        gateway_reference: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> GatewayResult:
        return GatewayResult(
            gateway_reference=f"re_{uuid.uuid5(uuid.NAMESPACE_URL, gateway_reference + idempotency_key).hex[:24]}",
            status=self.CONFIRMED,
            amount=amount,
            currency=currency,
            provider_status=self.CONFIRMED,
            safe_display="Simulated refund",
            payload={"payment_reference": gateway_reference},
        )

    def get_payment_status(self, gateway_reference: str) -> str:
        intent = SimulatedGatewayIntent.objects.filter(gateway_reference=gateway_reference).first()
        if not intent:
            raise PaymentGatewayError("Gateway reference was not found.")
        return intent.status

    @staticmethod
    def _record_intent(reference: str, status: str, amount: Decimal, currency: str) -> None:
        SimulatedGatewayIntent.objects.update_or_create(
            gateway_reference=reference,
            defaults={"status": status, "amount": amount, "currency": currency},
        )


gateway = SimulatedPaymentGateway()


def _safe_mode(mode: str) -> str:
    """Ignore gateway test modes (decline/dropped_confirmation/...) in production so
    untrusted clients cannot drive simulator behaviour (spec §19.3)."""
    if mode != "approve" and not getattr(settings, "GATEWAY_TEST_MODES_ENABLED", False):
        return "approve"
    return mode


def authorize_payment(
    attempt: CheckoutAttempt,
    *,
    idempotency_key: str,
    card_token: str = "tok_visa",
    mode: str = "approve",
) -> Payment:
    existing = Payment.objects.filter(checkout_attempt=attempt, idempotency_key=idempotency_key).first()
    if existing:
        return existing
    if attempt.status not in {CheckoutAttempt.Status.RESERVED, CheckoutAttempt.Status.PAYMENT_PENDING}:
        raise CheckoutStateError("Checkout attempt is not ready for payment.")

    mode = _safe_mode(mode)
    # In production, ignore the simulator's decline test card so untrusted clients can't
    # drive gateway behaviour via card_token (mirrors _safe_mode for `mode`).
    if card_token != "tok_visa" and not getattr(settings, "GATEWAY_TEST_MODES_ENABLED", False):
        card_token = "tok_visa"
    result = gateway.authorize(
        amount=attempt.amount_due,
        currency=attempt.currency,
        idempotency_key=idempotency_key,
        card_token=card_token,
        mode=mode,
    )

    with transaction.atomic():
        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
        payment, created = Payment.objects.get_or_create(
            checkout_attempt=locked_attempt,
            idempotency_key=idempotency_key,
            defaults={
                # Charge only the amount due after held store credit.
                "amount": locked_attempt.amount_due,
                "currency": locked_attempt.currency,
                "gateway_reference": result.gateway_reference,
                "safe_display": result.safe_display,
            },
        )
        if not created:
            return payment

        payment.gateway_reference = result.gateway_reference
        payment.safe_display = result.safe_display
        payment.failure_code = result.failure_code
        payment.raw_status = result.provider_status
        if result.status == gateway.FAILED:
            payment.status = Payment.Status.FAILED
            locked_attempt.status = CheckoutAttempt.Status.FAILED
        else:
            payment.status = Payment.Status.AUTHORIZED
            locked_attempt.status = CheckoutAttempt.Status.PAYMENT_PENDING
            locked_attempt.gateway_reference = result.gateway_reference
            locked_attempt.payment_started_at = timezone.now()
        payment.save()
        locked_attempt.save(
            update_fields=["status", "gateway_reference", "payment_started_at", "updated_at"]
        )
        PaymentEvent.objects.create(
            payment=payment,
            checkout_attempt=locked_attempt,
            gateway_reference=result.gateway_reference,
            event_type="payment.authorized",
            payload=result.payload or {},
            status=result.status,
            processing_result="authorized" if result.status != gateway.FAILED else "failed",
        )

    if result.status == gateway.FAILED:
        release_reservations(attempt)
        release_hold(attempt)
        _queue_payment_failed_email(attempt, failure_code=result.failure_code)
    return payment


def confirm_payment(
    payment: Payment,
    *,
    idempotency_key: str,
    mode: str = "approve",
):
    payment.refresh_from_db()
    if payment.order_id and payment.checkout_attempt.status == CheckoutAttempt.Status.FINALIZED:
        return payment.order

    mode = _safe_mode(mode)
    result = gateway.confirm(
        gateway_reference=payment.gateway_reference,
        idempotency_key=idempotency_key,
        mode=mode,
    )
    failed = result.status == gateway.FAILED
    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=locked_payment.checkout_attempt_id)
        PaymentEvent.objects.create(
            payment=locked_payment,
            checkout_attempt=locked_attempt,
            gateway_reference=result.gateway_reference,
            event_type="payment.confirmed" if not failed else "payment.failed",
            payload=result.payload or {},
            status=result.status,
            processing_result="recorded",
        )
        locked_payment.raw_status = result.provider_status
        locked_payment.failure_code = result.failure_code
        if failed:
            locked_payment.status = Payment.Status.FAILED
            locked_attempt.status = CheckoutAttempt.Status.FAILED
        else:
            locked_payment.status = Payment.Status.CONFIRMED
            locked_attempt.status = CheckoutAttempt.Status.PAYMENT_CONFIRMED
        locked_payment.save(update_fields=["status", "raw_status", "failure_code", "updated_at"])
        locked_attempt.save(update_fields=["status", "updated_at"])

    if failed:
        release_reservations(payment.checkout_attempt)
        release_hold(payment.checkout_attempt)
        _queue_payment_failed_email(payment.checkout_attempt, failure_code=result.failure_code)
        raise PaymentGatewayError("Payment confirmation failed.", code="payment_failed")

    from .checkout import finalize_confirmed_payment

    return finalize_confirmed_payment(payment)


def _queue_payment_failed_email(attempt: CheckoutAttempt, *, failure_code: str = "") -> None:
    OutboxEvent.objects.create(
        event_type="payment.failed_email",
        aggregate_type="CheckoutAttempt",
        aggregate_id=str(attempt.pk),
        payload={"email": attempt.guest_email, "failure_code": failure_code},
    )


def replay_confirmation(gateway_reference: str):
    payment = Payment.objects.get(gateway_reference=gateway_reference)
    if payment.status != Payment.Status.CONFIRMED:
        with transaction.atomic():
            locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
            locked_attempt = CheckoutAttempt.objects.select_for_update().get(
                pk=locked_payment.checkout_attempt_id
            )
            locked_payment.status = Payment.Status.CONFIRMED
            locked_payment.raw_status = gateway.CONFIRMED
            locked_payment.save(update_fields=["status", "raw_status", "updated_at"])
            locked_attempt.status = CheckoutAttempt.Status.PAYMENT_CONFIRMED
            locked_attempt.save(update_fields=["status", "updated_at"])
            PaymentEvent.objects.create(
                payment=locked_payment,
                checkout_attempt=locked_attempt,
                gateway_reference=gateway_reference,
                event_type="payment.confirmed.replay",
                status=gateway.CONFIRMED,
                processing_result="recorded",
            )
    from .checkout import finalize_confirmed_payment

    payment.refresh_from_db()
    return finalize_confirmed_payment(payment)


def reconcile_stranded_payments(
    *,
    older_than: timedelta = timedelta(seconds=0),
    abandon_authorized_after: timedelta = timedelta(hours=1),
) -> int:
    now = timezone.now()
    cutoff = now - older_than
    abandon_cutoff = now - abandon_authorized_after
    attempts = CheckoutAttempt.objects.filter(
        status=CheckoutAttempt.Status.PAYMENT_PENDING,
        payment_started_at__lte=cutoff,
    )
    resolved = 0
    for attempt in attempts:
        # Scope finalize/coupon/inventory work to the attempt's own store.
        set_current_tenant(attempt.tenant_id)
        payment = attempt.payments.order_by("-created_at").first()
        if not payment or not payment.gateway_reference:
            continue
        provider_status = gateway.get_payment_status(payment.gateway_reference)
        if provider_status == gateway.CONFIRMED:
            with transaction.atomic():
                locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
                locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
                locked_payment.status = Payment.Status.CONFIRMED
                locked_payment.raw_status = gateway.CONFIRMED
                locked_payment.save(update_fields=["status", "raw_status", "updated_at"])
                locked_attempt.status = CheckoutAttempt.Status.PAYMENT_CONFIRMED
                locked_attempt.save(update_fields=["status", "updated_at"])
                PaymentEvent.objects.create(
                    payment=locked_payment,
                    checkout_attempt=locked_attempt,
                    gateway_reference=payment.gateway_reference,
                    event_type="payment.reconciled",
                    status=gateway.CONFIRMED,
                    processing_result="confirmed-by-poll",
                )
            from .checkout import finalize_confirmed_payment

            finalize_confirmed_payment(payment)
            resolved += 1
        elif provider_status == gateway.FAILED:
            with transaction.atomic():
                locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
                locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
                locked_payment.status = Payment.Status.FAILED
                locked_payment.raw_status = gateway.FAILED
                locked_payment.save(update_fields=["status", "raw_status", "updated_at"])
                locked_attempt.status = CheckoutAttempt.Status.FAILED
                locked_attempt.save(update_fields=["status", "updated_at"])
            release_reservations(attempt)
            release_hold(attempt)
            resolved += 1
        elif provider_status == gateway.AUTHORIZED and (
            attempt.payment_started_at is not None and attempt.payment_started_at <= abandon_cutoff
        ):
            # Authorized but never confirmed and abandoned past the threshold: cancel the
            # authorization and release its reservations so stock cannot be held forever.
            with transaction.atomic():
                locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
                locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
                locked_payment.status = Payment.Status.FAILED
                locked_payment.failure_code = "authorization_abandoned"
                locked_payment.save(update_fields=["status", "failure_code", "updated_at"])
                locked_attempt.status = CheckoutAttempt.Status.FAILED
                locked_attempt.save(update_fields=["status", "updated_at"])
                PaymentEvent.objects.create(
                    payment=locked_payment,
                    checkout_attempt=locked_attempt,
                    gateway_reference=payment.gateway_reference,
                    event_type="payment.authorization_abandoned",
                    status=gateway.FAILED,
                    processing_result="released-by-poll",
                )
            release_reservations(attempt)
            release_hold(attempt)
            resolved += 1
    clear_current_tenant()
    return resolved
