from __future__ import annotations

from django.db import transaction

from shop.models import (
    Address,
    Cart,
    CustomerSubscription,
    Order,
    Review,
    StoreCredit,
    WishlistItem,
)


def export_user_data(user) -> dict:
    """A machine-readable export of everything we hold about a customer (GDPR access)."""
    return {
        "account": {"username": user.get_username(), "email": user.email, "joined": user.date_joined.isoformat()},
        "addresses": list(
            user.addresses.values("label", "name", "address1", "address2", "city", "region", "postal_code", "country", "phone")
        ),
        "orders": [
            {
                "order_number": o.order_number,
                "status": o.status,
                "total": str(o.total),
                "created": o.created_at.isoformat(),
                "items": list(o.items.values("sku", "product_name", "quantity", "unit_price", "line_total")),
            }
            for o in user.orders.prefetch_related("items")
        ],
        "subscriptions": list(user.subscriptions.values("interval", "status", "unit_price", "next_renewal_at")),
        "reviews": list(user.reviews.values("product_id", "rating", "title", "body", "created_at")),
        "wishlist": list(user.wishlist_items.values("variant__sku")),
        "store_credit": str(getattr(getattr(user, "store_credit", None), "balance", "0.00")),
    }


def delete_account(user) -> None:
    """Erase a customer's PII (GDPR erasure) while retaining anonymized order records
    the merchant is legally required to keep for accounting/tax."""
    with transaction.atomic():
        # Retain orders for merchant records, but strip personal data and unlink the user.
        for order in Order.objects.filter(user=user):
            Order.objects.filter(pk=order.pk).update(
                user=None,
                guest_email="",
                shipping_name="[deleted]",
                shipping_address1="",
                shipping_address2="",
                shipping_city="",
                shipping_region="",
                shipping_postal_code="",
            )
        # Anonymize authored reviews (kept for other shoppers), forfeit personal data.
        Review.objects.filter(user=user).update(user=None, author_name="Former customer")
        CustomerSubscription.objects.filter(user=user).update(
            status=CustomerSubscription.Status.CANCELLED
        )
        Address.objects.filter(user=user).delete()
        WishlistItem.objects.filter(user=user).delete()
        StoreCredit.objects.filter(user=user).delete()
        # Unlink carts (they may be PROTECT-referenced by retained checkout attempts) so
        # deleting the user doesn't cascade into them.
        Cart.objects.filter(user=user).update(user=None)
        # Finally remove the account itself.
        user.delete()
