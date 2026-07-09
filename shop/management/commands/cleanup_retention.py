from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import ProtectedError
from django.utils import timezone

from shop.models import Cart, CheckoutAttempt, IdempotencyRecord


class Command(BaseCommand):
    help = "Delete expired idempotency records and stale carts/checkout attempts (spec §28.2)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Retention window in days.")

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timedelta(days=options["days"])

        # 1) Idempotency records past their documented TTL.
        idem_deleted, _ = IdempotencyRecord.objects.filter(expires_at__lt=now).delete()

        # 2) Terminal checkout attempts with no order and no payments (safe to remove;
        #    reservations/snapshots cascade). Skip any the DB protects.
        attempts = CheckoutAttempt.objects.filter(
            status__in=[CheckoutAttempt.Status.EXPIRED, CheckoutAttempt.Status.FAILED],
            created_at__lt=cutoff,
            order__isnull=True,
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

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleanup: {idem_deleted} idempotency records, "
                f"{attempts_deleted} checkout attempts, {carts_deleted} carts removed."
            )
        )
