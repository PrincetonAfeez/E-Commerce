"""Inbound PSP webhook ingestion and async-style payment confirmation."""
from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction

from shop.models import InboundGatewayEvent, Payment
from shop.tenancy import tenant_context

from .exceptions import PaymentGatewayError
from .gateway import get_payment_gateway
from .payments import confirm_payment

logger = logging.getLogger("shop.psp_webhooks")


def receive_gateway_webhook(
    *,
    provider: str,
    body: bytes,
    signature: str,
    tenant_id: int | None = None,
) -> InboundGatewayEvent:
    gw = get_payment_gateway(provider=provider)
    if not gw.verify_webhook_signature(body=body, signature=signature, tenant_id=tenant_id):
        raise PaymentGatewayError("Invalid webhook signature.", code="invalid_signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaymentGatewayError("Invalid webhook JSON.", code="invalid_payload") from exc

    normalized = gw.parse_webhook_event(payload)
    event_id = normalized.get("event_id", "")
    event_type = normalized.get("event_type", "")
    gateway_reference = normalized.get("gateway_reference", "")
    resolved_tenant = tenant_id or normalized.get("tenant_id")

    try:
        with transaction.atomic():
            record = InboundGatewayEvent.objects.create(
                tenant_id=resolved_tenant,
                provider=provider,
                provider_event_id=event_id,
                event_type=event_type,
                gateway_reference=gateway_reference,
                payload=payload,
                signature=signature[:256],
                status=InboundGatewayEvent.Status.RECEIVED,
            )
    except IntegrityError:
        existing = InboundGatewayEvent.objects.filter(provider=provider, provider_event_id=event_id).first()
        if existing:
            return existing
        raise

    try:
        process_inbound_gateway_event(record.pk)
    except Exception as exc:
        logger.exception("Failed to process inbound gateway event %s", record.pk)
        InboundGatewayEvent.objects.filter(pk=record.pk).update(
            status=InboundGatewayEvent.Status.FAILED,
            processing_result=str(exc)[:500],
        )
        record.refresh_from_db()
    return record


def process_inbound_gateway_event(event_id: int) -> InboundGatewayEvent:
    record = InboundGatewayEvent.objects.select_related("tenant").get(pk=event_id)
    if record.status == InboundGatewayEvent.Status.PROCESSED:
        return record

    payment = Payment.objects.filter(gateway_reference=record.gateway_reference).first()
    if payment is None:
        InboundGatewayEvent.objects.filter(pk=record.pk).update(
            status=InboundGatewayEvent.Status.IGNORED,
            processing_result="payment-not-found",
        )
        record.refresh_from_db()
        return record

    tenant_id = record.tenant_id or payment.tenant_id
    with tenant_context(tenant_id):
        if record.event_type in {"payment.confirmed", "payment.succeeded"}:
            confirm_payment(
                payment,
                idempotency_key=f"webhook-{record.provider_event_id or record.pk}",
            )
            InboundGatewayEvent.objects.filter(pk=record.pk).update(
                status=InboundGatewayEvent.Status.PROCESSED,
                processing_result="confirmed-via-webhook",
            )
        else:
            InboundGatewayEvent.objects.filter(pk=record.pk).update(
                status=InboundGatewayEvent.Status.IGNORED,
                processing_result=f"unhandled-event:{record.event_type}",
            )
    record.refresh_from_db()
    return record
