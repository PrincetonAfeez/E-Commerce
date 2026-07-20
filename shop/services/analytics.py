"""Staff dashboard metrics: revenue, AOV, daily series, email and outbox health"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from shop.models import EmailDelivery, Order, OrderItem, OutboxEvent, ProductVariant, Reservation
from shop.tenancy import get_current_tenant_id

from .exceptions import CommerceError

_ZERO = Decimal("0.00")


def _money_sum(qs, field):
    return qs.aggregate(v=Coalesce(Sum(field), _ZERO, output_field=DecimalField()))["v"]


def dashboard_metrics(*, days: int = 14) -> dict:
    if get_current_tenant_id() is None:
        raise CommerceError("Tenant context is required for dashboard metrics.")
    now = timezone.now()
    since = now - timedelta(days=days)

    non_cancelled = Order.objects.exclude(status=Order.Status.CANCELLED)
    gross = _money_sum(non_cancelled, "total")
    refunds = _money_sum(non_cancelled, "refund_total")
    net = gross - refunds
    paid_count = non_cancelled.count()
    aov = (net / paid_count).quantize(Decimal("0.01")) if paid_count else _ZERO

    # Orders + revenue per day over the window.
    per_day = (
        Order.objects.filter(created_at__gte=since)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(orders=Count("id"), revenue=Coalesce(Sum("total"), _ZERO, output_field=DecimalField()))
        .order_by("day")
    )
    series = [
        {"day": row["day"].isoformat(), "orders": row["orders"], "revenue": str(row["revenue"])} for row in per_day
    ]

    status_counts = dict(Order.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n"))

    top_products = list(
        OrderItem.objects.values("product_name", "sku")
        .annotate(units=Sum("quantity"), revenue=Coalesce(Sum("line_total"), _ZERO, output_field=DecimalField()))
        .order_by("-units")[:10]
    )

    # Low stock: available (quantity - active reservations) at or below the reorder point.
    low_stock = list(
        ProductVariant.objects.filter(active=True)
        .annotate(
            reserved=Coalesce(
                Sum("reservations__quantity", filter=Q(reservations__status=Reservation.Status.ACTIVE)),
                0,
            )
        )
        .annotate(available=F("quantity") - F("reserved"))
        .filter(available__lte=F("reorder_point"))
        .select_related("product")
        .order_by("available")[:25]
    )

    return {
        "gross_revenue": gross,
        "net_revenue": net,
        "refunds": refunds,
        "order_count": Order.objects.count(),
        "paid_count": paid_count,
        "aov": aov,
        "series": series,
        "status_counts": status_counts,
        "top_products": top_products,
        "low_stock": low_stock,
        "outbox_pending": OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING).count(),
        "outbox_failed": OutboxEvent.objects.filter(status=OutboxEvent.Status.FAILED).count(),
        "email_failed": EmailDelivery.objects.filter(status=EmailDelivery.Status.FAILED).count(),
        "window_days": days,
    }
