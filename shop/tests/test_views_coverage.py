"""Storefront, account, checkout, and ops view coverage"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.test import override_settings
from django.urls import reverse

from shop.models import AccountProfile, Tenant, TenantMembership
from shop.services.checkout import begin_checkout
from shop.services.payments import authorize_payment, confirm_payment

from .conftest import ensure_verified_profile
from .test_checkout_seam import make_cart, make_coupon, make_variant

pytestmark = pytest.mark.django_db


def _place_order(*, user=None, email="view-guest@example.com"):
    variant = make_variant(quantity=5, price="30.00")
    cart = make_cart(variant)
    if user:
        cart.user = user
        cart.save()
        ensure_verified_profile(user)
    attempt = begin_checkout(cart, idempotency_key=f"vw-{uuid.uuid4().hex[:6]}", contact={"email": email})
    payment = authorize_payment(attempt, idempotency_key=f"pay-{uuid.uuid4().hex[:6]}")
    return confirm_payment(payment, idempotency_key=f"cf-{uuid.uuid4().hex[:6]}"), variant


# --- ops / static ---
def test_healthz_and_readyz(client):
    assert client.get(reverse("healthz")).status_code == 200
    assert client.get(reverse("readyz")).status_code == 200


@override_settings(OPS_METRICS_SECRET="metrics-secret")
def test_internal_metrics_with_secret(client):
    denied = client.get(reverse("internal_metrics"))
    assert denied.status_code == 403
    ok = client.get(reverse("internal_metrics"), HTTP_X_OPS_METRICS_SECRET="metrics-secret")
    assert ok.status_code == 200


def test_robots_sitemap_legal_theme(client):
    assert client.get(reverse("robots")).status_code == 200
    assert client.get(reverse("sitemap")).status_code == 200
    assert client.get(reverse("terms")).status_code == 200
    assert client.get(reverse("privacy")).status_code == 200
    assert client.get(reverse("theme_css")).status_code == 200


@override_settings(TLS_CHECK_SECRET="tls-secret")
def test_tls_check_requires_secret_and_domain(client):
    tenant = Tenant.objects.get(slug="default")
    tenant.primary_domain = "tls-unit.test"
    tenant.save(update_fields=["primary_domain"])
    denied = client.get(reverse("tls_check"))
    assert denied.status_code == 403
    ok = client.get(
        reverse("tls_check"),
        {"domain": "tls-unit.test"},
        HTTP_X_TLS_CHECK_SECRET="tls-secret",
    )
    assert ok.status_code == 200


# --- auth ---
def test_register_and_verify_email_flow(client):
    resp = client.post(
        reverse("register"),
        {
            "username": f"reg{uuid.uuid4().hex[:6]}",
            "email": "new@test.com",
            "password1": "Str0ngPass!",
            "password2": "Str0ngPass!",
        },
    )
    assert resp.status_code in {200, 302}
    user = get_user_model().objects.get(email="new@test.com")
    assert not AccountProfile.objects.get(user=user).email_verified


def test_resend_verification_requires_login(client):
    resp = client.get(reverse("resend_verification"))
    assert resp.status_code in {302, 403}


# --- catalog ---
def test_catalog_list_and_product_detail(client):
    variant = make_variant()
    product = variant.product
    assert client.get(reverse("catalog:list")).status_code == 200
    assert client.get(reverse("catalog:detail", args=[product.slug])).status_code == 200


# --- cart ---
def test_cart_add_update_and_coupon(client):
    variant = make_variant(price="50.00")
    coupon = make_coupon()
    client.get(reverse("cart:detail"))
    add = client.post(reverse("cart:add"), {"variant_id": variant.pk, "quantity": 2})
    assert add.status_code == 302
    update = client.post(reverse("cart:update"), {"variant_id": variant.pk, "quantity": 1})
    assert update.status_code == 302
    apply = client.post(reverse("cart:apply_coupon"), {"code": coupon.code})
    assert apply.status_code == 302
    remove = client.post(reverse("cart:remove_coupon"))
    assert remove.status_code == 302


# --- checkout ---
def test_checkout_web_flow(client):
    variant = make_variant(quantity=3)
    client.post(reverse("cart:add"), {"variant_id": variant.pk, "quantity": 1})
    start = client.post(
        reverse("checkout:start"),
        {
            "email": "checkout@test.com",
            "name": "Buyer",
            "address1": "1 St",
            "city": "City",
            "postal_code": "12345",
            "shipping_method": "Standard",
        },
    )
    assert start.status_code in {200, 302}
    if start.status_code == 302:
        from shop.models import CheckoutAttempt

        attempt = CheckoutAttempt.objects.order_by("-pk").first()
        pay = client.post(reverse("checkout:payment", args=[attempt.pk]), {"card_token": "tok_visa"})
        assert pay.status_code in {200, 302}


# --- orders ---
def test_guest_order_lookup_view(client):
    order, _ = _place_order(email="lookup@test.com")
    resp = client.post(
        reverse("orders:lookup"),
        {"email": "lookup@test.com", "order_number": order.order_number},
    )
    assert resp.status_code in {200, 302}


def test_order_history_requires_login(client):
    assert client.get(reverse("orders:history")).status_code in {302, 403}


def test_order_history_for_user(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"oh{uuid.uuid4().hex[:6]}", password="x")
    _place_order(user=user, email="oh@test.com")
    client.force_login(user)
    assert client.get(reverse("orders:history")).status_code == 200


def test_reorder_view(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"ro{uuid.uuid4().hex[:6]}", password="x")
    order, _ = _place_order(user=user)
    client.force_login(user)
    resp = client.post(reverse("orders:reorder", args=[order.order_number]))
    assert resp.status_code == 302


# --- account ---
def test_wishlist_views(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"wl{uuid.uuid4().hex[:6]}", password="x")
    variant = make_variant()
    client.force_login(user)
    assert client.get(reverse("account:wishlist")).status_code == 200
    toggle = client.post(reverse("account:wishlist_toggle"), {"variant_id": variant.pk})
    assert toggle.status_code in {200, 302}


def test_store_credit_view(client, django_user_model):
    from shop.models import StoreCredit

    user = django_user_model.objects.create_user(username=f"sc{uuid.uuid4().hex[:6]}", password="x")
    StoreCredit.objects.create(user=user, balance=Decimal("15.00"))
    client.force_login(user)
    assert client.get(reverse("account:store_credit")).status_code == 200


def test_account_data_export(client, django_user_model):
    user = django_user_model.objects.create_user(
        username=f"ex{uuid.uuid4().hex[:6]}", email="ex@test.com", password="x"
    )
    client.force_login(user)
    resp = client.get(reverse("account:data_export"))
    assert resp.status_code == 200


def test_address_crud(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"ad{uuid.uuid4().hex[:6]}", password="x")
    client.force_login(user)
    assert client.get(reverse("account:addresses")).status_code == 200
    create = client.post(
        reverse("account:address_create"),
        {
            "label": "Home",
            "name": "Me",
            "address1": "1 St",
            "city": "City",
            "postal_code": "12345",
            "country": "US",
        },
    )
    assert create.status_code in {200, 302}
    from shop.models import Address

    addr = Address.objects.filter(user=user).first()
    if addr:
        assert client.post(reverse("account:address_default", args=[addr.pk])).status_code in {200, 302}
        assert client.post(reverse("account:address_delete", args=[addr.pk])).status_code in {200, 302}


# --- staff ---
def test_staff_dashboard_requires_membership(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"st{uuid.uuid4().hex[:6]}", password="x", is_staff=True)
    client.force_login(user)
    assert client.get(reverse("staff_ops:dashboard")).status_code == 403


def test_staff_dashboard_for_member(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"st2{uuid.uuid4().hex[:6]}", password="x")
    tenant = Tenant.objects.get(slug="default")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.MANAGER)
    _place_order()
    client.force_login(user)
    assert client.get(reverse("staff_ops:dashboard")).status_code == 200
    assert client.get(reverse("staff_ops:queue")).status_code == 200
    assert client.get(reverse("staff_ops:low_stock")).status_code == 200
    assert client.get(reverse("staff_ops:settings")).status_code == 200
    assert client.get(reverse("staff_ops:billing")).status_code == 200
    assert client.get(reverse("staff_ops:team")).status_code == 200


def test_unsubscribe_view(client):
    token = signing.dumps("unsub@test.com", salt="unsubscribe")
    assert client.get(reverse("unsubscribe", args=[token])).status_code == 200
