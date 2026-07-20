# Deletes expired idempotency records and stale carts or checkout attempts

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import ProtectedError
from django.utils import timezone

from shop.locks import single_instance
from shop.models import (
    Cart,
    CheckoutAttempt,
    EmailDelivery,
    IdempotencyRecord,
    OutboxEvent,
    WebhookDelivery,
)

IDEM_DELETE_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Delete expired idempotency records and stale carts/checkout attempts (spec §28.2)."

    def add_arguments(self, parser):

        parser.add_argument("--days", type=int, default=30, help="Retention window in days.")

    def handle(self, *args, **options):

        with single_instance("cleanup_retention") as acquired:
            if not acquired:
                self.stdout.write("Another worker is running cleanup retention; skipping.")

                return

            now = timezone.now()

            cutoff = now - timedelta(days=options["days"])

            # 1) Idempotency records past their documented TTL (batched to limit lock time).

            idem_deleted = 0

            while True:
                batch_ids = list(
                    IdempotencyRecord.objects.filter(expires_at__lt=now).values_list("pk", flat=True)[
                        :IDEM_DELETE_BATCH_SIZE
                    ]
                )

                if not batch_ids:
                    break

                deleted, _ = IdempotencyRecord.objects.filter(pk__in=batch_ids).delete()

                idem_deleted += deleted

            # 2) Terminal checkout attempts with no order and no payments (safe to remove;

            #    reservations/snapshots cascade). Skip any the DB protects.

            attempts = CheckoutAttempt.objects.filter(
                status__in=[CheckoutAttempt.Status.EXPIRED, CheckoutAttempt.Status.FAILED],
                created_at__lt=cutoff,
                order_record__isnull=True,
            ).exclude(payments__isnull=False)

            attempts_deleted = 0

            for attempt in attempts:
                try:
                    attempt.delete()

                    attempts_deleted += 1

                except ProtectedError:
                    continue

            # 3) Carts that are done (merged/ordered/abandoned) and no longer referenced.

            carts = Cart.objects.filter(
                status__in=[Cart.Status.MERGED, Cart.Status.ORDERED, Cart.Status.ABANDONED],
                updated_at__lt=cutoff,
                checkout_attempts__isnull=True,
                reservations__isnull=True,
            )

            carts_deleted = 0

            for cart in carts:
                try:
                    cart.delete()

                    carts_deleted += 1

                except ProtectedError:
                    continue

            # 4) Terminal outbox / delivery logs past the retention window.

            outbox_deleted, _ = OutboxEvent.objects.filter(
                status__in=[OutboxEvent.Status.SENT, OutboxEvent.Status.FAILED],
                created_at__lt=cutoff,
            ).delete()

            email_deleted, _ = EmailDelivery.objects.filter(
                status__in=[EmailDelivery.Status.SENT, EmailDelivery.Status.FAILED],
                created_at__lt=cutoff,
            ).delete()

            webhook_deleted, _ = WebhookDelivery.objects.filter(
                status__in=[WebhookDelivery.Status.SUCCESS, WebhookDelivery.Status.FAILED],
                created_at__lt=cutoff,
            ).delete()

            self.stdout.write(
                self.style.SUCCESS(
                    f"Cleanup: {idem_deleted} idempotency records, "
                    f"{attempts_deleted} checkout attempts, {carts_deleted} carts, "
                    f"{outbox_deleted} outbox events, {email_deleted} email deliveries, "
                    f"{webhook_deleted} webhook deliveries removed."
                )
            )
