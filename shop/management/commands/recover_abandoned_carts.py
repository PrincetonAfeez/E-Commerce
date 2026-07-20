# Queues cart recovery emails for active carts abandoned past a threshold
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from shop.feature_flags import is_enabled
from shop.locks import single_instance
from shop.models import Cart, EmailSuppression, OutboxEvent
from shop.services.cart import recalculate_cart
from shop.tenancy import tenant_context


class Command(BaseCommand):
    help = "Queue recovery emails for active carts abandoned past a threshold."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=60)
        parser.add_argument("--max", type=int, default=500)

    def handle(self, *args, **options):
        if not is_enabled("ABANDONED_CART_RECOVERY"):
            self.stdout.write("Abandoned cart recovery disabled (FF_ABANDONED_CART_RECOVERY=0).")
            return
        with single_instance("recover_abandoned_carts") as acquired:
            if not acquired:
                self.stdout.write("Another worker is recovering abandoned carts; skipping.")
                return
            cutoff = timezone.now() - timedelta(minutes=options["older_than_minutes"])
            carts = (
                Cart.objects.filter(
                    status=Cart.Status.ACTIVE,
                    recovery_sent_at__isnull=True,
                    updated_at__lte=cutoff,
                )
                .select_related("user")
                .prefetch_related("items")[: options["max"]]
            )
            queued = 0
            for cart in carts:
                if not cart.items.exists():
                    continue
                email = _cart_email(cart)
                if not email:
                    continue
                with tenant_context(cart.tenant_id):
                    with transaction.atomic():
                        locked = Cart.objects.select_for_update().get(pk=cart.pk)
                        if locked.recovery_sent_at is not None:
                            continue
                        if EmailSuppression.objects.filter(email__iexact=email).exists():
                            continue
                        totals = recalculate_cart(locked)
                        OutboxEvent.objects.create(
                            event_type="cart.recovery_email",
                            aggregate_type="Cart",
                            aggregate_id=str(locked.pk),
                            payload={
                                "email": email,
                                "item_count": locked.item_count(),
                                "total": str(totals.total),
                            },
                        )
                        locked.recovery_sent_at = timezone.now()
                        locked.save(update_fields=["recovery_sent_at", "updated_at"])
                        queued += 1
            self.stdout.write(self.style.SUCCESS(f"Queued {queued} cart recovery emails."))


def _cart_email(cart: Cart) -> str:
    if cart.user_id and cart.user.email:
        return cart.user.email
    attempt = cart.checkout_attempts.exclude(guest_email="").order_by("-created_at").first()
    return attempt.guest_email if attempt else ""
