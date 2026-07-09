# Effective variant pricing via customer groups and per-group price list entries
from __future__ import annotations

from decimal import Decimal

from shop.models import AccountProfile, PriceListEntry
from shop.tenancy import get_current_tenant_id

from .money import quantize_money


def _user_group(user):
    if not getattr(user, "is_authenticated", False):
        return None
    profile = (
        AccountProfile.objects.filter(user=user).select_related("customer_group").first()
    )
    group = profile.customer_group if profile else None
    if group is None:
        return None
    # Only honour a group that belongs to the store currently being shopped.
    tid = get_current_tenant_id()
    if tid is not None and group.tenant_id != tid:
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
