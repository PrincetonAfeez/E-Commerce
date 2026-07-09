# Also-bought product suggestions and session-based recently viewed tracking
from __future__ import annotations

from django.db.models import Count

from shop.models import OrderItem, Product


def also_bought(product, *, limit: int = 4):
    """Products frequently purchased in the same orders as this product."""
    order_ids = list(
        OrderItem.objects.filter(variant__product=product).values_list("order_id", flat=True)
    )
    if not order_ids:
        return Product.objects.none()
    ranked = (
        OrderItem.objects.filter(order_id__in=order_ids)
        .exclude(variant__product=product)
        .filter(variant__isnull=False)
        .values("variant__product")
        .annotate(n=Count("id"))
        .order_by("-n")[:limit]
    )
    product_ids = [row["variant__product"] for row in ranked]
    if not product_ids:
        return Product.objects.none()
    # Preserve rank order.
    products = Product.objects.filter(id__in=product_ids, status=Product.Status.ACTIVE)
    by_id = {p.id: p for p in products}
    return [by_id[pid] for pid in product_ids if pid in by_id]


def track_recently_viewed(request, product, *, cap: int = 8) -> None:
    viewed = [pid for pid in request.session.get("recently_viewed", []) if pid != product.id]
    viewed.insert(0, product.id)
    request.session["recently_viewed"] = viewed[:cap]


def recently_viewed(request, *, exclude_id=None, limit: int = 4):
    ids = [pid for pid in request.session.get("recently_viewed", []) if pid != exclude_id]
    if not ids:
        return []
    products = {p.id: p for p in Product.objects.filter(id__in=ids, status=Product.Status.ACTIVE)}
    return [products[pid] for pid in ids if pid in products][:limit]
