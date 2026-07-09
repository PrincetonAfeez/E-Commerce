# Processes pending outbox events and sends transactional emails with retry backoff
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from shop.models import OutboxEvent
from shop.services.notifications import deliver_outbox_event

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30


class Command(BaseCommand):
    help = "Process pending outbox events with the local email delivery log."

    def handle(self, *args, **options):
        processed = 0
        failed = 0
        events = OutboxEvent.objects.filter(
            status=OutboxEvent.Status.PENDING,
            available_at__lte=timezone.now(),
        ).order_by("available_at", "id")
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
                    locked.save(
                        update_fields=["status", "attempts", "sent_at", "last_error", "updated_at"]
                    )
                    processed += 1
        self.stdout.write(
            self.style.SUCCESS(f"Processed {processed} outbox events ({failed} moved to failed).")
        )

    def _handle_event(self, event: OutboxEvent) -> None:
        # Renders and actually sends the transactional email (idempotent per event).
        deliver_outbox_event(event)
