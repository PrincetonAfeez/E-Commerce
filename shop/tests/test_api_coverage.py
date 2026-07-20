"""Broad REST API coverage: catalog, cart, checkout, orders, guest, and staff endpoints"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core import mail
from django.urls import reverse

from shop.models import Fulfillment, Tenant, TenantMembership
from shop.services.checkout import begin_checkout
from shop.services.payments import authorize_payment, confirm_payment

from .conftest import ensure_verified_profile
from .test_checkout_seam import make_cart, make_coupon, make_variant

pytestmark = pytest.mark.django_db


def _checkout_payload():
    return {
        "shipping_method": "Standard",
        "email": "api-guest@example.com",
        "name": "Guest Buyer",
        "address1": "1 Main St",
        "city": "Town",
        "region": "CA",
        "postal_code": "90210",
        "country": "US",
    }


def _api_checkout(client, *, idem: str | None = None, force_logout: bool = False):
    if force_logout:
        client.logout()
    variant = make_variant(quantity=5, price="25.00")
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    key = idem or f"api-co-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        reverse("api-checkout-attempts"),
        _checkout_payload(),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    assert resp.status_code == 201
    attempt_id = resp.json()["id"]
    pay = client.post(
        reverse("api-checkout-confirm-payment", args=[attempt_id]),
        {"card_token": "tok_visa"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=f"{key}-pay",
    )
    assert pay.status_code == 201
    return pay.json(), variant


def _staff_manager(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"mgr{uuid.uuid4().hex[:6]}", password="x")
    tenant = Tenant.objects.get(slug="default")
    TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.MANAGER)
    client.force_login(user)
    return user


# --- catalog ---
def test_api_products_list_and_detail(client):
    variant = make_variant()
    product = variant.product
    listing = client.get(reverse("api-products-list"))
    assert listing.status_code == 200
    assert any(r["slug"] == product.slug for r in listing.json()["results"])

    detail = client.get(reverse("api-products-detail", args=[product.slug]))
    assert detail.status_code == 200
    assert detail.json()["slug"] == product.slug
    assert detail.json()["variants"]


def test_api_products_search_and_filters(client):
    variant = make_variant(price="45.00")
    product = variant.product
    by_q = client.get(reverse("api-products-list"), {"q": product.name[:8]})
    assert by_q.status_code == 200
    by_price = client.get(reverse("api-products-list"), {"min_price": "40", "max_price": "50"})
    assert by_price.status_code == 200


def test_api_categories_and_collections(client):
    from shop.models import Category, Collection

    cat = Category.objects.create(name="API Cat", slug=f"api-cat-{uuid.uuid4().hex[:6]}")
    coll = Collection.objects.create(name="API Coll", slug=f"api-coll-{uuid.uuid4().hex[:6]}", active=True)
    assert client.get(reverse("api-categories-list")).status_code == 200
    assert client.get(reverse("api-categories-detail", args=[cat.slug])).status_code == 200
    assert client.get(reverse("api-collections-list")).status_code == 200
    assert client.get(reverse("api-collections-detail", args=[coll.slug])).status_code == 200


# --- cart ---
def test_api_cart_crud(client):
    variant = make_variant(quantity=10)
    empty = client.get(reverse("api-cart"))
    assert empty.status_code == 200

    add = client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 2},
        content_type="application/json",
    )
    assert add.status_code == 201
    assert add.json()["items"][0]["quantity"] == 2

    patch = client.patch(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 3},
        content_type="application/json",
    )
    assert patch.status_code == 200
    assert patch.json()["items"][0]["quantity"] == 3

    delete = client.delete(
        reverse("api-cart-items"),
        {"variant_id": variant.pk},
        content_type="application/json",
    )
    assert delete.status_code == 200
    assert delete.json()["items"] == []


def test_api_cart_delete_via_query_params(client):
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    resp = client.delete(reverse("api-cart-items") + f"?variant_id={variant.pk}")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_api_apply_and_remove_coupon(client):
    variant = make_variant(price="100.00")
    coupon = make_coupon()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    applied = client.post(
        reverse("api-cart-apply-coupon"),
        {"code": coupon.code},
        content_type="application/json",
    )
    assert applied.status_code == 200
    assert Decimal(applied.json()["totals"]["discount_total"]) > 0

    removed = client.post(reverse("api-cart-remove-coupon"))
    assert removed.status_code == 200
    assert removed.json()["coupon"] == ""


def test_api_checkout_requires_idempotency_key(client):
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    resp = client.post(
        reverse("api-checkout-attempts"),
        _checkout_payload(),
        content_type="application/json",
    )
    assert resp.status_code == 400


# --- checkout / payment ---
def test_api_confirm_payment_full_flow(client):
    order, _ = _api_checkout(client)
    assert order["order_number"]
    assert order["status"] in {"paid", "placed"}


def test_api_authorize_then_confirm_separately(client):
    variant = make_variant()
    client.post(
        reverse("api-cart-items"),
        {"variant_id": variant.pk, "quantity": 1},
        content_type="application/json",
    )
    key = f"split-{uuid.uuid4().hex[:8]}"
    co = client.post(
        reverse("api-checkout-attempts"),
        _checkout_payload(),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    attempt_id = co.json()["id"]
    auth = client.post(
        reverse("api-checkout-authorize-payment", args=[attempt_id]),
        {"card_token": "tok_visa"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=f"{key}-auth",
    )
    assert auth.status_code == 201
    assert auth.json()["status"] == "authorized"


# --- orders ---
def test_api_order_list_requires_auth(client):
    assert client.get(reverse("api-orders")).status_code in {401, 403}


def test_api_order_list_for_authenticated_user(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"buy{uuid.uuid4().hex[:6]}", password="x")
    ensure_verified_profile(user)
    variant = make_variant()
    cart = make_cart(variant)
    cart.user = user
    cart.save()
    attempt = begin_checkout(cart, idempotency_key="list-co", contact={"email": user.email})
    payment = authorize_payment(attempt, idempotency_key="list-pay")
    confirm_payment(payment, idempotency_key="list-cf")

    client.force_login(user)
    resp = client.get(reverse("api-orders"))
    assert resp.status_code == 200
    assert resp.json()["results"]


def test_api_order_detail_with_token(client):
    from django.test import Client

    order, _ = _api_checkout(client, idem="detail-token")
    from shop.models import Order

    db_order = Order.objects.get(order_number=order["order_number"])
    denied = Client().get(reverse("api-order-detail", args=[db_order.order_number]))
    assert denied.status_code == 403
    ok = client.get(
        reverse("api-order-detail", args=[db_order.order_number]),
        {"token": str(db_order.order_token)},
    )
    assert ok.status_code == 200


# --- guest ---
def test_api_guest_order_lookup_sends_email(client):
    order, _ = _api_checkout(client, idem="guest-lookup")
    mail.outbox.clear()
    resp = client.post(
        reverse("api-guest-order-lookup"),
        {"email": "api-guest@example.com", "order_number": order["order_number"]},
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert mail.outbox


def test_api_guest_order_by_token(client):
    order, _ = _api_checkout(client, idem="guest-token")
    from shop.models import Order

    db_order = Order.objects.get(order_number=order["order_number"])
    resp = client.get(reverse("api-guest-order", args=[db_order.order_token]))
    assert resp.status_code == 200
    assert resp.json()["order_number"] == db_order.order_number


# --- staff ---
def test_api_staff_orders_list(client, django_user_model):
    _api_checkout(client, idem="staff-list")
    _staff_manager(client, django_user_model)
    resp = client.get(reverse("api-staff-orders"))
    assert resp.status_code == 200
    assert resp.json()["results"]


def test_api_staff_transition_fulfillment(client, django_user_model):
    order, _ = _api_checkout(client, idem="staff-trans")
    _staff_manager(client, django_user_model)
    from shop.models import Order

    db_order = Order.objects.get(order_number=order["order_number"])
    resp = client.post(
        reverse("api-staff-order-transition", args=[db_order.pk]),
        {"target_status": Fulfillment.Status.PROCESSING},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="st-trans-1",
    )
    assert resp.status_code == 200
    assert resp.json()["fulfillment"]["status"] == Fulfillment.Status.PROCESSING


def test_api_staff_refund(client, django_user_model):
    order, _ = _api_checkout(client, idem="staff-ref")
    _staff_manager(client, django_user_model)
    from shop.models import Order

    db_order = Order.objects.get(order_number=order["order_number"])
    resp = client.post(
        reverse("api-staff-order-refund", args=[db_order.pk]),
        {"amount": str(db_order.total), "reason": "Test"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="st-ref-1",
    )
    assert resp.status_code == 200
    assert Decimal(resp.json()["amount"]) == db_order.total


def test_api_staff_cancel_order(client, django_user_model):
    order, _ = _api_checkout(client, idem="staff-cancel")
    _staff_manager(client, django_user_model)
    from shop.models import Order

    db_order = Order.objects.get(order_number=order["order_number"])
    resp = client.post(
        reverse("api-staff-order-cancel", args=[db_order.pk]),
        {"note": "Customer request", "restock": True},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="st-cancel-1",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_api_staff_inventory_adjustment(client, django_user_model):
    variant = make_variant(quantity=5)
    _staff_manager(client, django_user_model)
    before = variant.quantity
    resp = client.post(
        reverse("api-staff-inventory-adjust"),
        {"variant_id": variant.pk, "delta": 3, "note": "Restock"},
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="inv-adj-1",
    )
    assert resp.status_code == 200
    variant.refresh_from_db()
    assert variant.quantity == before + 3


def test_api_staff_checkout_replay(client, django_user_model):
    variant = make_variant(quantity=3)
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="replay-co")
    authorize_payment(attempt, idempotency_key="replay-pay", mode="dropped_confirmation")
    _staff_manager(client, django_user_model)
    resp = client.post(
        reverse("api-staff-checkout-replay", args=[attempt.pk]),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="replay-final",
    )
    assert resp.status_code in {200, 201, 409}
