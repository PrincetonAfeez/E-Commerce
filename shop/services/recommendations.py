"""Also-bought product suggestions and session-based recently viewed tracking"""
from __future__ import annotations

from django.db.models import Count

from shop.models import OrderItem, Product
from shop.tenancy import get_current_tenant_id


def also_bought(product, *, limit: int = 4):
    """Products frequently purchased in the same orders as this product."""
    tid = get_current_tenant_id()
    order_items = OrderItem.objects.filter(variant__product=product)
    if tid is not None:
        order_items = order_items.filter(order__tenant_id=tid)
    order_ids = list(order_items.values_list("order_id", flat=True))
    if not order_ids:
        return Product.objects.none()
    ranked = (
        OrderItem.objects.filter(order_id__in=order_ids).exclude(variant__product=product).filter(variant__isnull=False)
    )
    if tid is not None:
        ranked = ranked.filter(order__tenant_id=tid)
    ranked = ranked.values("variant__product").annotate(n=Count("id")).order_by("-n")[:limit]
    product_ids = [row["variant__product"] for row in ranked]
    if not product_ids:
        return Product.objects.none()
    # Preserve rank order.
    products = Product.objects.filter(id__in=product_ids, status=Product.Status.ACTIVE)
    by_id = {p.id: p for p in products}
    return [by_id[pid] for pid in product_ids if pid in by_id]


def _recently_viewed_key() -> str:
    tid = get_current_tenant_id()
    return f"recently_viewed:{tid}" if tid is not None else "recently_viewed"


def track_recently_viewed(request, product, *, cap: int = 8) -> None:
    key = _recently_viewed_key()
    viewed = [pid for pid in request.session.get(key, []) if pid != product.id]
    viewed.insert(0, product.id)
    request.session[key] = viewed[:cap]


def recently_viewed(request, *, exclude_id=None, limit: int = 4):
    ids = [pid for pid in request.session.get(_recently_viewed_key(), []) if pid != exclude_id]
    if not ids:
        return []
    products = {p.id: p for p in Product.objects.filter(id__in=ids, status=Product.Status.ACTIVE)}
    return [products[pid] for pid in ids if pid in products][:limit]
