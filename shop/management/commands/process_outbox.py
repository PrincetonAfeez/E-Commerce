"""Processes pending outbox events and sends transactional emails with retry backoff"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from shop.locks import single_instance
from shop.models import OutboxEvent
from shop.services.notifications import EMAIL_EVENTS, deliver_outbox_event
from shop.tenancy import tenant_context

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30


class Command(BaseCommand):
    help = "Process pending outbox events with the local email delivery log."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200, help="Max events to process per run.")

    def handle(self, *args, **options):
        with single_instance("process_outbox") as acquired:
            if not acquired:
                self.stdout.write("Another worker is processing the outbox; skipping.")
                return
            processed = 0
            failed = 0
            events = OutboxEvent.objects.filter(
                status=OutboxEvent.Status.PENDING,
                available_at__lte=timezone.now(),
            ).order_by("available_at", "id")[: options["limit"]]
            for event in events:
                with transaction.atomic():
                    locked = OutboxEvent.objects.select_for_update().get(pk=event.pk)
                    if locked.status != OutboxEvent.Status.PENDING or locked.available_at > timezone.now():
                        continue
                    locked.attempts += 1
                    try:
                        self._handle_event(locked)
                    except Exception as exc:  # noqa: BLE001 - persist the failure for operators
                        if locked.attempts >= MAX_ATTEMPTS:
                            locked.status = OutboxEvent.Status.FAILED
                            failed += 1
                        else:
                            # Bounded exponential backoff; stays PENDING until available_at.
                            delay = BACKOFF_BASE_SECONDS * (2 ** (locked.attempts - 1))
                            locked.available_at = timezone.now() + timedelta(seconds=delay)
                        locked.last_error = str(exc)[:2000]
                        locked.save(update_fields=["status", "attempts", "available_at", "last_error", "updated_at"])
                    else:
                        locked.status = OutboxEvent.Status.SENT
                        locked.sent_at = timezone.now()
                        locked.last_error = ""
                        locked.save(update_fields=["status", "attempts", "sent_at", "last_error", "updated_at"])
                        processed += 1
            self.stdout.write(self.style.SUCCESS(f"Processed {processed} outbox events ({failed} moved to failed)."))

    def _handle_event(self, event: OutboxEvent) -> None:
        with tenant_context(event.tenant_id):
            if event.event_type == "payment.auto_refund_required":
                from shop.services.refunds import process_compensation_refund

                process_compensation_refund(int(event.aggregate_id))
                return

            result = deliver_outbox_event(event)
            if result is None and event.event_type not in EMAIL_EVENTS:
                raise RuntimeError(f"Unknown outbox event type: {event.event_type}")
