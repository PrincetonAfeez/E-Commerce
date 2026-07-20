"""GDPR user data export and account deletion scrubbing personal data from orders"""
from __future__ import annotations

from django.db import transaction

from shop.models import (
    Address,
    Cart,
    CustomerSubscription,
    Order,
    Review,
    StoreCredit,
    TenantMembership,
    WishlistItem,
)
from shop.services.credit import get_balance
from shop.tenancy import tenant_context


def export_user_data(user) -> dict:
    """A machine-readable export of everything we hold about a customer (GDPR access)."""
    memberships = TenantMembership.objects.filter(user=user).select_related("tenant")
    tenants = [m.tenant for m in memberships]
    if not tenants:
        from shop.models import Tenant

        tenants = list(Tenant.objects.filter(active=True).order_by("id")[:1])

    per_tenant = []
    for tenant in tenants:
        with tenant_context(tenant):
            per_tenant.append(
                {
                    "tenant": tenant.slug,
                    "orders": [
                        {
                            "order_number": o.order_number,
                            "status": o.status,
                            "total": str(o.total),
                            "created": o.created_at.isoformat(),
                            "items": list(
                                o.items.values("sku", "product_name", "quantity", "unit_price", "line_total")
                            ),
                        }
                        for o in Order.objects.filter(user=user).prefetch_related("items")
                    ],
                    "subscriptions": list(
                        CustomerSubscription.objects.filter(user=user).values(
                            "interval", "status", "unit_price", "next_renewal_at"
                        )
                    ),
                    "store_credit": str(get_balance(user)),
                }
            )

    return {
        "account": {
            "username": user.get_username(),
            "email": user.email,
            "joined": user.date_joined.isoformat(),
        },
        "addresses": list(
            user.addresses.values(
                "label", "name", "address1", "address2", "city", "region", "postal_code", "country", "phone"
            )
        ),
        "tenants": per_tenant,
        "reviews": list(user.reviews.values("product_id", "rating", "title", "body", "created_at")),
        "wishlist": list(user.wishlist_items.values("variant__sku")),
    }


def delete_account(user) -> None:
    """Erase a customer's PII (GDPR erasure) while retaining anonymized order records."""
    with transaction.atomic():
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
                shipping_country="",
            )
        Review.objects.filter(user=user).update(user=None, author_name="Former customer")
        CustomerSubscription.objects.filter(user=user).update(status=CustomerSubscription.Status.CANCELLED)
        Address.objects.filter(user=user).delete()
        WishlistItem.objects.filter(user=user).delete()
        for membership in TenantMembership.objects.filter(user=user):
            StoreCredit.objects.filter(user=user, tenant_id=membership.tenant_id).delete()
        Cart.objects.filter(user=user).update(user=None)
        user.delete()
