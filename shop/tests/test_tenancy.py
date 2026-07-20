"""Tests multi-tenant isolation, staff scoping, signup, billing, and cross-store access"""
from __future__ import annotations

from decimal import Decimal

import pytest

from shop.models import (
    CouponCode,
    OutboxEvent,
    Product,
    ProductVariant,
    Promotion,
    StoreSettings,
    Subscription,
    Tenant,
    WebhookDelivery,
    WebhookEndpoint,
)
from shop.services.promotions import get_coupon_by_code
from shop.services.webhooks import enqueue_deliveries
from shop.tenancy import clear_current_tenant, set_current_tenant

pytestmark = pytest.mark.django_db


def _tenant(slug, **kw):
    return Tenant.objects.create(name=slug.upper(), slug=slug, **kw)


def _product(name="P", slug="p", status=Product.Status.ACTIVE):
    return Product.objects.create(name=name, slug=slug, status=status)


# --- Core isolation: the default manager scopes to the active tenant ---
def test_products_isolated_by_tenant():
    a, b = _tenant("a"), _tenant("b")

    set_current_tenant(a)
    pa = _product("A product", "shared-slug")
    set_current_tenant(b)
    pb = _product("B product", "shared-slug")  # same slug, different tenant -> allowed

    set_current_tenant(a)
    assert list(Product.objects.all()) == [pa]
    set_current_tenant(b)
    assert list(Product.objects.all()) == [pb]

    clear_current_tenant()  # no context -> unfiltered (admin/jobs)
    assert Product.objects.filter(pk__in=[pa.pk, pb.pk]).count() == 2


def test_cross_tenant_variant_lookup_is_blocked():
    """A shopper on store B must not reach store A's variant by guessing its id."""
    a, b = _tenant("a"), _tenant("b")
    set_current_tenant(a)
    pa = _product("A", "pa")
    va = ProductVariant.objects.create(product=pa, sku="X1", price=Decimal("10.00"))

    set_current_tenant(b)
    assert not ProductVariant.objects.filter(pk=va.pk).exists()
    with pytest.raises(ProductVariant.DoesNotExist):
        ProductVariant.objects.get(pk=va.pk)


def test_slug_and_sku_may_repeat_across_tenants():
    a, b = _tenant("a"), _tenant("b")
    set_current_tenant(a)
    pa = _product("A", "same")
    va = ProductVariant.objects.create(product=pa, sku="SHARED", price=Decimal("5.00"))
    set_current_tenant(b)
    pb = _product("B", "same")  # duplicate slug across tenants is fine
    vb = ProductVariant.objects.create(product=pb, sku="SHARED", price=Decimal("5.00"))
    assert pa.pk != pb.pk and va.pk != vb.pk


def test_store_settings_are_per_tenant():
    a, b = _tenant("a"), _tenant("b")
    set_current_tenant(a)
    sa = StoreSettings.get_solo()
    sa.store_name = "Store A"
    sa.save()
    set_current_tenant(b)
    sb = StoreSettings.get_solo()
    sb.store_name = "Store B"
    sb.save()

    set_current_tenant(a)
    assert StoreSettings.get_solo().store_name == "Store A"
    set_current_tenant(b)
    assert StoreSettings.get_solo().store_name == "Store B"


def test_subscription_is_per_tenant():
    a, b = _tenant("a"), _tenant("b")
    set_current_tenant(a)
    sub_a = Subscription.get_solo()
    set_current_tenant(b)
    sub_b = Subscription.get_solo()
    assert sub_a.pk != sub_b.pk


# --- Request-level isolation: the middleware routes by host ---
def test_storefront_scoped_by_host(client, settings):
    settings.ALLOWED_HOSTS = ["*"]
    a = _tenant("store-a", primary_domain="a.example.com")
    _tenant("store-b", primary_domain="b.example.com")
    set_current_tenant(a)
    _product("Only On A", "only-a")
    clear_current_tenant()

    resp_a = client.get("/", HTTP_HOST="a.example.com")
    resp_b = client.get("/", HTTP_HOST="b.example.com")
    assert b"Only On A" in resp_a.content
    assert b"Only On A" not in resp_b.content


# --- Background/platform tier is tenant-safe ---
def test_webhook_events_do_not_cross_tenants():
    a, b = _tenant("a"), _tenant("b")
    set_current_tenant(a)
    ep_a = WebhookEndpoint.objects.create(url="https://a.test/hook", secret="k")
    set_current_tenant(b)
    ep_b = WebhookEndpoint.objects.create(url="https://b.test/hook", secret="k")

    set_current_tenant(a)
    event = OutboxEvent.objects.create(event_type="order.confirmation_email", aggregate_type="Order", aggregate_id="1")
    clear_current_tenant()  # simulate the job running with no context

    created = enqueue_deliveries(event)
    assert created == 1
    assert WebhookDelivery.objects.filter(endpoint=ep_a, outbox_event=event).exists()
    assert not WebhookDelivery.objects.filter(endpoint=ep_b, outbox_event=event).exists()


def test_coupon_codes_are_per_tenant():
    a, b = _tenant("a"), _tenant("b")
    set_current_tenant(a)
    pa = Promotion.objects.create(name="A", type=Promotion.Type.PERCENTAGE, discount_percent=Decimal("10"))
    ca = CouponCode.objects.create(promotion=pa, code="SAVE10")
    set_current_tenant(b)
    pb = Promotion.objects.create(name="B", type=Promotion.Type.PERCENTAGE, discount_percent=Decimal("10"))
    cb = CouponCode.objects.create(promotion=pb, code="SAVE10")  # same code, different store -> ok
    assert ca.pk != cb.pk

    set_current_tenant(a)
    assert get_coupon_by_code("SAVE10").pk == ca.pk
    set_current_tenant(b)
    assert get_coupon_by_code("SAVE10").pk == cb.pk


# --- Plan limits ---
def test_product_limit_enforced_per_plan():
    from shop.models import Plan, Subscription
    from shop.services.exceptions import PlanLimitError
    from shop.services.plans import assert_can_create_product, can_create_product, plan_usage

    a = _tenant("a")
    plan = Plan.objects.create(name="Tiny", slug="tiny", max_products=1, active=True)
    set_current_tenant(a)
    sub = Subscription.get_solo()
    sub.plan = plan
    sub.save()

    assert can_create_product() is True
    _product("P1", "p1")
    assert can_create_product() is False
    with pytest.raises(PlanLimitError):
        assert_can_create_product()

    usage = plan_usage()
    assert usage["product_count"] == 1
    assert usage["product_limit"] == 1
    assert usage["product_over"] is True


def test_unlimited_plan_never_blocks():
    from shop.models import Plan, Subscription
    from shop.services.plans import can_create_product

    a = _tenant("a")
    plan = Plan.objects.create(name="Scale", slug="scale", max_products=None, active=True)
    set_current_tenant(a)
    sub = Subscription.get_solo()
    sub.plan = plan
    sub.save()
    for i in range(5):
        _product(f"P{i}", f"p{i}")
    assert can_create_product() is True


def test_tls_check_authorizes_only_known_tenant_domains(client):
    _tenant("acme", primary_domain="shop.acme.com")
    assert client.get("/internal/tls-check/", {"domain": "shop.acme.com"}).status_code == 200
    assert client.get("/internal/tls-check/", {"domain": "evil.example.com"}).status_code == 404


# --- Tier 2: multi-tenant staff, self-serve signup, invites, billing ---
def test_staff_of_one_store_cannot_operate_another(client, settings, django_user_model):
    from shop.models import TenantMembership

    settings.ALLOWED_HOSTS = ["*"]
    a = _tenant("store-a", primary_domain="a.example.com")
    _tenant("store-b", primary_domain="b.example.com")
    owner = django_user_model.objects.create_user(username="owner-a", password="x")
    TenantMembership.objects.create(tenant=a, user=owner, role=TenantMembership.Role.OWNER)
    client.force_login(owner)

    assert client.get("/staff/", HTTP_HOST="a.example.com").status_code == 200
    assert client.get("/staff/", HTTP_HOST="b.example.com").status_code == 403


def test_self_serve_signup_creates_store(client, settings, django_user_model):
    from shop.models import Plan, Subscription, Tenant, TenantMembership

    settings.ALLOWED_HOSTS = ["*"]
    Plan.objects.create(name="Starter", slug="starter", price_monthly=Decimal("0.00"), active=True)
    resp = client.post(
        "/signup/",
        {"store_name": "Acme Co", "subdomain": "acme", "email": "o@acme.test", "password": "pw12345xyz"},
    )
    assert resp.status_code == 200
    tenant = Tenant.objects.get(slug="acme")
    owner = django_user_model.objects.get(username="o@acme.test")
    assert TenantMembership.objects.filter(tenant=tenant, user=owner, role=TenantMembership.Role.OWNER).exists()
    set_current_tenant(tenant)
    sub = Subscription.get_solo()
    assert sub.plan.slug == "starter"
    assert sub.status == Subscription.Status.TRIALING


def test_team_invite_adds_membership(client, settings, django_user_model):
    from shop.models import TenantMembership

    settings.ALLOWED_HOSTS = ["*"]
    a = _tenant("store-a", primary_domain="a.example.com")
    owner = django_user_model.objects.create_user(username="owner-a", password="x")
    TenantMembership.objects.create(tenant=a, user=owner, role=TenantMembership.Role.OWNER)
    client.force_login(owner)

    client.post("/staff/team/invite/", {"email": "new@a.test", "role": "staff"}, HTTP_HOST="a.example.com")
    new_user = django_user_model.objects.get(username="new@a.test")
    assert TenantMembership.objects.filter(tenant=a, user=new_user).exists()


def test_billing_cycle_issues_invoice():
    from shop.models import Invoice, Plan, Subscription
    from shop.services.plans import run_billing_cycle

    a = _tenant("a")
    plan = Plan.objects.create(name="Growth", slug="growth", price_monthly=Decimal("49.00"), active=True)
    set_current_tenant(a)
    sub = Subscription.get_solo()
    sub.plan = plan
    sub.status = Subscription.Status.ACTIVE
    sub.save()
    clear_current_tenant()

    assert run_billing_cycle() >= 1
    set_current_tenant(a)
    assert Invoice.objects.filter(plan_name="Growth", amount=Decimal("49.00")).exists()


def test_staff_api_is_tenant_scoped(client, settings, django_user_model):
    """A staffer of store A must not operate store B's orders via the API by host."""
    from shop.models import TenantMembership

    settings.ALLOWED_HOSTS = ["*"]
    a = _tenant("store-a", primary_domain="a.example.com")
    _tenant("store-b", primary_domain="b.example.com")
    manager = django_user_model.objects.create_user(username="mgr-a", password="x")
    TenantMembership.objects.create(tenant=a, user=manager, role=TenantMembership.Role.MANAGER)
    client.force_login(manager)

    # Allowed on A's host, forbidden on B's host.
    assert client.get("/api/v1/staff/orders/", HTTP_HOST="a.example.com").status_code == 200
    assert client.get("/api/v1/staff/orders/", HTTP_HOST="b.example.com").status_code == 403


def test_staff_api_denies_non_member(client, settings, django_user_model):
    settings.ALLOWED_HOSTS = ["*"]
    _tenant("store-a", primary_domain="a.example.com")
    # is_staff alone (no membership) must NOT grant tenant staff API access.
    outsider = django_user_model.objects.create_user(username="outsider", password="x", is_staff=True)
    client.force_login(outsider)
    assert client.get("/api/v1/staff/orders/", HTTP_HOST="a.example.com").status_code == 403
