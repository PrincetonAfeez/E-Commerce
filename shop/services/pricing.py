"""Effective variant pricing via customer groups and per-group price list entries"""
from __future__ import annotations

from decimal import Decimal

from shop.models import PriceListEntry, TenantCustomerProfile
from shop.tenancy import get_current_tenant_id

from .money import quantize_money


def _user_group(user):
    if not getattr(user, "is_authenticated", False):
        return None
    tid = get_current_tenant_id()
    if tid is None:
        raise RuntimeError("Tenant context is required for customer-group pricing.")
    profile = TenantCustomerProfile.objects.filter(user=user, tenant_id=tid).select_related("customer_group").first()
    if not profile or not profile.customer_group_id:
        return None
    group = profile.customer_group
    if group.tenant_id != tid:
        return None
    return group


def effective_price(variant, user) -> Decimal:
    """The unit price a given customer pays: price-list entry > group % off > base."""
    group = _user_group(user)
    if group is None:
        return variant.price
    entry = PriceListEntry.objects.filter(group=group, variant=variant).first()
    if entry:
        return entry.price
    if group.percent_off:
        return quantize_money(variant.price * (Decimal("1") - group.percent_off / Decimal("100")))
    return variant.price
