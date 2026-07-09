# SaaS plan limits, usage tracking, billing cycle invoicing, and product caps
from __future__ import annotations

from datetime import datetime, time

from django.utils import timezone

from shop.models import Order, Product, Subscription
from shop.tenancy import default_tenant_id, get_current_tenant_id

from .exceptions import PlanLimitError


def _tid(tenant_id=None) -> int:
    return tenant_id or get_current_tenant_id() or default_tenant_id()


def current_plan(tenant_id=None):
    sub = Subscription._base_manager.filter(tenant_id=_tid(tenant_id)).select_related("plan").first()
    return sub.plan if sub else None


def _month_start():
    now = timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def product_count(tenant_id=None) -> int:
    return Product._base_manager.filter(tenant_id=_tid(tenant_id)).count()


def orders_this_month(tenant_id=None) -> int:
    tid = _tid(tenant_id)
    return Order._base_manager.filter(tenant_id=tid, created_at__gte=_month_start()).count()


def can_create_product(tenant_id=None) -> bool:
    tid = _tid(tenant_id)
    plan = current_plan(tid)
    if plan is None or plan.max_products is None:
        return True
    return product_count(tid) < plan.max_products


def assert_can_create_product(tenant_id=None) -> None:
    if not can_create_product(tenant_id):
        raise PlanLimitError(
            "This store has reached its plan's product limit. Upgrade your plan to add more products."
        )


def _period_bounds(now):
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date()
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end


def run_billing_cycle(*, now=None) -> int:
    """Issue this month's invoice for each subscribed tenant (simulated charge)."""
    from shop.models import Invoice, Subscription
    from shop.tenancy import tenant_context

    now = now or timezone.now()
    period_start, period_end = _period_bounds(now)
    issued = 0
    subs = (
        Subscription._base_manager.select_related("plan")
        .exclude(plan__isnull=True)
        .exclude(status=Subscription.Status.CANCELLED)
    )
    for sub in subs:
        with tenant_context(sub.tenant_id):
            _, created = Invoice.objects.get_or_create(
                period_start=period_start,
                defaults={
                    "period_end": period_end,
                    "plan_name": sub.plan.name,
                    "amount": sub.plan.price_monthly,
                    "orders_count": orders_this_month(sub.tenant_id),
                    # Simulated processor always succeeds (matches the storefront gateway).
                    "status": Invoice.Status.PAID,
                },
            )
            if created:
                period_end_dt = timezone.make_aware(
                    datetime.combine(period_end, time.min)
                )
                Subscription._base_manager.filter(pk=sub.pk).update(current_period_end=period_end_dt)
                issued += 1
    return issued


def plan_usage(tenant_id=None) -> dict:
    """Usage vs. limits for the dashboard/billing page (order limit is soft/advisory)."""
    tid = _tid(tenant_id)
    plan = current_plan(tid)
    products = product_count(tid)
    orders = orders_this_month(tid)
    product_limit = plan.max_products if plan else None
    order_limit = plan.max_orders_per_month if plan else None
    return {
        "plan": plan,
        "product_count": products,
        "product_limit": product_limit,
        "product_over": product_limit is not None and products >= product_limit,
        "orders_month": orders,
        "order_limit": order_limit,
        "order_over": order_limit is not None and orders >= order_limit,
    }
