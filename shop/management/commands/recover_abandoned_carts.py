from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from shop.models import Cart, EmailSuppression, OutboxEvent
from shop.services.cart import recalculate_cart
from shop.tenancy import tenant_context


class Command(BaseCommand):
    help = "Queue recovery emails for active carts abandoned past a threshold."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=60)
        parser.add_argument("--max", type=int, default=500)

    def handle(self, *args, **options):
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
            if not email or EmailSuppression.objects.filter(email__iexact=email).exists():
                continue  # no address, or the customer opted out of marketing
            # Scope to the cart's own store so tax/shipping rates + settings are correct.
            with tenant_context(cart.tenant_id):
                totals = recalculate_cart(cart)
                OutboxEvent.objects.create(
                    event_type="cart.recovery_email",
                    aggregate_type="Cart",
                    aggregate_id=str(cart.pk),
                    payload={
                        "email": email,
                        "item_count": cart.item_count(),
                        "total": str(totals.total),
                    },
                )
            Cart.objects.filter(pk=cart.pk).update(recovery_sent_at=timezone.now())
            queued += 1
        self.stdout.write(self.style.SUCCESS(f"Queued {queued} cart recovery emails."))


def _cart_email(cart: Cart) -> str:
    if cart.user_id and cart.user.email:
        return cart.user.email
    attempt = cart.checkout_attempts.exclude(guest_email="").order_by("-created_at").first()
    return attempt.guest_email if attempt else ""
