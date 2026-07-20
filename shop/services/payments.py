"""Payment orchestration: authorize, confirm, reconcile, and stranded recovery"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from shop.models import CheckoutAttempt, OutboxEvent, Payment, PaymentEvent
from shop.tenancy import clear_current_tenant, get_current_tenant_id, set_current_tenant, tenant_context

from .credit import release_hold
from .exceptions import CheckoutStateError, PaymentGatewayError
from .gateway import AUTHORIZED, CONFIRMED, FAILED, GatewayResult, get_payment_gateway
from .inventory import release_reservations

logger = logging.getLogger("shop.payments")

# Module-level default gateway instance (backward compatible; prefer get_payment_gateway()).
gateway = get_payment_gateway()


def _safe_mode(mode: str) -> str:
    """Ignore gateway test modes (decline/dropped_confirmation/...) in production so
    untrusted clients cannot drive simulator behaviour (spec §19.3)."""
    if mode != "approve" and not getattr(settings, "GATEWAY_TEST_MODES_ENABLED", False):
        return "approve"
    return mode


def _record_payment_event(
    *,
    payment: Payment,
    attempt: CheckoutAttempt,
    event_type: str,
    result: GatewayResult,
    processing_result: str,
    provider: str,
) -> PaymentEvent | None:
    provider_event_id = result.provider_event_id or ""
    if provider_event_id:
        existing = PaymentEvent.objects.filter(
            tenant_id=payment.tenant_id,
            provider=provider,
            provider_event_id=provider_event_id,
        ).first()
        if existing:
            return existing
    try:
        return PaymentEvent.objects.create(
            payment=payment,
            checkout_attempt=attempt,
            gateway_reference=result.gateway_reference,
            event_type=event_type,
            payload=result.payload or {},
            status=result.status,
            processing_result=processing_result,
            provider=provider,
            provider_event_id=provider_event_id,
        )
    except IntegrityError:
        if provider_event_id:
            return PaymentEvent.objects.filter(
                tenant_id=payment.tenant_id,
                provider=provider,
                provider_event_id=provider_event_id,
            ).first()
        raise


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
    if card_token != "tok_visa" and not getattr(settings, "GATEWAY_TEST_MODES_ENABLED", False):
        card_token = "tok_visa"

    with transaction.atomic():
        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked_attempt.status not in {
            CheckoutAttempt.Status.RESERVED,
            CheckoutAttempt.Status.PAYMENT_PENDING,
        }:
            raise CheckoutStateError("Checkout attempt is not ready for payment.")
        if Payment.objects.filter(
            checkout_attempt=locked_attempt,
            status__in=[
                Payment.Status.PENDING,
                Payment.Status.AUTHORIZED,
                Payment.Status.CONFIRMED,
            ],
        ).exists():
            existing = Payment.objects.filter(checkout_attempt=locked_attempt, idempotency_key=idempotency_key).first()
            if existing:
                return existing
            raise CheckoutStateError("Checkout attempt already has an active payment.")
        amount = locked_attempt.amount_due
        currency = locked_attempt.currency
        payment = Payment.objects.create(
            checkout_attempt=locked_attempt,
            idempotency_key=idempotency_key,
            amount=amount,
            currency=currency,
            status=Payment.Status.PENDING,
            tenant_id=locked_attempt.tenant_id,
            provider=get_payment_gateway().provider,
        )

    gw = get_payment_gateway()
    result = gw.authorize(
        amount=amount,
        currency=currency,
        idempotency_key=idempotency_key,
        card_token=card_token,
        mode=mode,
        tenant_scope=str(locked_attempt.tenant_id),
    )

    with transaction.atomic():
        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
        try:
            payment = Payment.objects.select_for_update().get(
                checkout_attempt=locked_attempt,
                idempotency_key=idempotency_key,
            )
        except Payment.DoesNotExist:
            payment = Payment.objects.filter(checkout_attempt=locked_attempt, idempotency_key=idempotency_key).first()
            if payment is None:
                raise

        payment.gateway_reference = result.gateway_reference
        payment.safe_display = result.safe_display
        payment.failure_code = result.failure_code
        payment.raw_status = result.provider_status
        if result.status == FAILED:
            payment.status = Payment.Status.FAILED
            locked_attempt.status = CheckoutAttempt.Status.FAILED
        else:
            payment.status = Payment.Status.AUTHORIZED
            locked_attempt.status = CheckoutAttempt.Status.PAYMENT_PENDING
            locked_attempt.gateway_reference = result.gateway_reference
            locked_attempt.payment_started_at = timezone.now()
        payment.save()
        locked_attempt.save(update_fields=["status", "gateway_reference", "payment_started_at", "updated_at"])
        _record_payment_event(
            payment=payment,
            attempt=locked_attempt,
            event_type="payment.authorized",
            result=result,
            processing_result="authorized" if result.status != FAILED else "failed",
            provider=gw.provider,
        )

    if result.status == FAILED:
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
    from .checkout import _order_from_attempt, finalize_confirmed_payment

    payment.refresh_from_db()
    if payment.order_id and payment.checkout_attempt.status == CheckoutAttempt.Status.FINALIZED:
        return payment.order

    mode = _safe_mode(mode)
    if payment.status == Payment.Status.CONFIRMED:
        order = _order_from_attempt(payment.checkout_attempt)
        if order:
            return order

    gw = get_payment_gateway()
    result = gw.confirm(
        gateway_reference=payment.gateway_reference,
        idempotency_key=idempotency_key,
        mode=mode,
    )
    failed = result.status == FAILED
    replay_order = None
    try:
        with transaction.atomic():
            locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
            locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=locked_payment.checkout_attempt_id)
            if locked_payment.status == Payment.Status.CONFIRMED:
                replay_order = _order_from_attempt(locked_attempt)
            elif locked_payment.status != Payment.Status.AUTHORIZED:
                raise CheckoutStateError("Payment is not authorized for confirmation.")
            else:
                _record_payment_event(
                    payment=locked_payment,
                    attempt=locked_attempt,
                    event_type="payment.confirmed" if not failed else "payment.failed",
                    result=result,
                    processing_result="recorded",
                    provider=gw.provider,
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
    except IntegrityError:
        payment.refresh_from_db()
        order = _order_from_attempt(payment.checkout_attempt)
        if order:
            return order
        raise

    if replay_order:
        return replay_order

    if failed:
        release_reservations(payment.checkout_attempt)
        release_hold(payment.checkout_attempt)
        _queue_payment_failed_email(payment.checkout_attempt, failure_code=result.failure_code)
        raise PaymentGatewayError("Payment confirmation failed.", code="payment_failed")

    return finalize_confirmed_payment(payment)


def _queue_payment_failed_email(attempt: CheckoutAttempt, *, failure_code: str = "") -> None:
    OutboxEvent.objects.create(
        event_type="payment.failed_email",
        aggregate_type="CheckoutAttempt",
        aggregate_id=str(attempt.pk),
        payload={"email": attempt.guest_email, "failure_code": failure_code},
    )


def replay_confirmation(gateway_reference: str):
    from shop.tenancy import get_current_tenant_id

    gw = get_payment_gateway()
    tid = get_current_tenant_id()
    qs = Payment.objects.filter(gateway_reference=gateway_reference)
    if tid is not None:
        qs = qs.filter(tenant_id=tid)
    payment = qs.get()
    if payment.status != Payment.Status.CONFIRMED:
        with transaction.atomic():
            locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
            locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=locked_payment.checkout_attempt_id)
            locked_payment.status = Payment.Status.CONFIRMED
            locked_payment.raw_status = CONFIRMED
            locked_payment.save(update_fields=["status", "raw_status", "updated_at"])
            locked_attempt.status = CheckoutAttempt.Status.PAYMENT_CONFIRMED
            locked_attempt.save(update_fields=["status", "updated_at"])
            _record_payment_event(
                payment=locked_payment,
                attempt=locked_attempt,
                event_type="payment.confirmed.replay",
                result=GatewayResult(
                    gateway_reference=gateway_reference,
                    status=CONFIRMED,
                    amount=locked_payment.amount,
                    currency=locked_payment.currency,
                    provider_status=CONFIRMED,
                    provider_event_id=f"{gateway_reference}:replay",
                ),
                processing_result="recorded",
                provider=gw.provider,
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
    prior_tenant = get_current_tenant_id()
    gw = get_payment_gateway()
    resolved = 0
    try:
        for attempt in attempts:
            with tenant_context(attempt.tenant_id):
                payment = attempt.payments.order_by("-created_at").first()
                if not payment or not payment.gateway_reference:
                    continue
                provider_status = gw.get_payment_status(payment.gateway_reference)
                if provider_status == CONFIRMED:
                    with transaction.atomic():
                        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
                        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
                        locked_payment.status = Payment.Status.CONFIRMED
                        locked_payment.raw_status = CONFIRMED
                        locked_payment.save(update_fields=["status", "raw_status", "updated_at"])
                        locked_attempt.status = CheckoutAttempt.Status.PAYMENT_CONFIRMED
                        locked_attempt.save(update_fields=["status", "updated_at"])
                        _record_payment_event(
                            payment=locked_payment,
                            attempt=locked_attempt,
                            event_type="payment.reconciled",
                            result=GatewayResult(
                                gateway_reference=payment.gateway_reference,
                                status=CONFIRMED,
                                amount=locked_payment.amount,
                                currency=locked_payment.currency,
                                provider_status=CONFIRMED,
                                provider_event_id=f"{payment.gateway_reference}:reconciled",
                            ),
                            processing_result="confirmed-by-poll",
                            provider=gw.provider,
                        )
                    from .checkout import finalize_confirmed_payment
                    from .refunds import process_compensation_refund

                    try:
                        finalize_confirmed_payment(payment)
                    except Exception:
                        payment.refresh_from_db()
                        if payment.status in {
                            Payment.Status.REQUIRES_REFUND,
                            Payment.Status.REFUNDED,
                        }:
                            resolved += 1
                            continue
                        logger.exception(
                            "Finalize failed for stranded payment %s; running compensation",
                            payment.pk,
                        )
                        process_compensation_refund(payment.pk)
                    resolved += 1
                elif provider_status == FAILED:
                    with transaction.atomic():
                        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
                        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
                        locked_payment.status = Payment.Status.FAILED
                        locked_payment.raw_status = FAILED
                        locked_payment.save(update_fields=["status", "raw_status", "updated_at"])
                        locked_attempt.status = CheckoutAttempt.Status.FAILED
                        locked_attempt.save(update_fields=["status", "updated_at"])
                    release_reservations(attempt)
                    release_hold(attempt)
                    resolved += 1
                elif provider_status == AUTHORIZED and (
                    attempt.payment_started_at is not None and attempt.payment_started_at <= abandon_cutoff
                ):
                    with transaction.atomic():
                        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
                        locked_attempt = CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
                        locked_payment.status = Payment.Status.FAILED
                        locked_payment.failure_code = "authorization_abandoned"
                        locked_payment.save(update_fields=["status", "failure_code", "updated_at"])
                        locked_attempt.status = CheckoutAttempt.Status.FAILED
                        locked_attempt.save(update_fields=["status", "updated_at"])
                        _record_payment_event(
                            payment=locked_payment,
                            attempt=locked_attempt,
                            event_type="payment.authorization_abandoned",
                            result=GatewayResult(
                                gateway_reference=payment.gateway_reference,
                                status=FAILED,
                                amount=locked_payment.amount,
                                currency=locked_payment.currency,
                                provider_status=FAILED,
                                failure_code="authorization_abandoned",
                                provider_event_id=f"{payment.gateway_reference}:abandoned",
                            ),
                            processing_result="released-by-poll",
                            provider=gw.provider,
                        )
                    release_reservations(attempt)
                    release_hold(attempt)
                    resolved += 1
    finally:
        if prior_tenant is None:
            clear_current_tenant()
        else:
            set_current_tenant(prior_tenant)
    return resolved
