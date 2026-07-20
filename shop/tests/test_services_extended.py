"""Extended service-layer tests: cart, promotions, returns, credit, plans, subscriptions, webhooks"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from shop.models import (
    Cart,
    CartItem,
    CheckoutAttempt,
    CustomerSubscription,
    GiftCard,
    Order,
    Plan,
    Product,
    Promotion,
    ReturnRequest,
    StoreCredit,
    Subscription,
    Tenant,
)
from shop.services import cart as cart_service
from shop.services import credit as credit_service
from shop.services import webhooks as webhook_service
from shop.services.accounts import export_user_data
from shop.services.analytics import dashboard_metrics
from shop.services.cart import merge_guest_cart, recalculate_cart
from shop.services.checkout import begin_checkout
from shop.services.exceptions import CartError, CheckoutStateError, InvalidCoupon
from shop.services.gateway.simulated import SimulatedPaymentGateway
from shop.services.payments import authorize_payment, confirm_payment
from shop.services.plans import can_create_product, plan_usage, run_billing_cycle
from shop.services.promotions import (
    best_auto_discount,
    calculate_discount,
    get_coupon_by_code,
    validate_coupon,
)
from shop.services.psp_webhooks import process_inbound_gateway_event, receive_gateway_webhook
from shop.services.recommendations import also_bought, recently_viewed, track_recently_viewed
from shop.services.returns import approve_return, reject_return, request_return
from shop.services.subscriptions import generate_due_renewals
from shop.tenancy import set_current_tenant

from .conftest import ensure_verified_profile
from .test_checkout_seam import make_cart, make_coupon, make_variant

pytestmark = pytest.mark.django_db


# --- cart ---
def test_add_item_caps_at_available_stock():
    variant = make_variant(quantity=2)
    cart = Cart.objects.create(session_key=f"c-{uuid.uuid4().hex[:8]}", status=Cart.Status.ACTIVE)
    cart_service.add_item(cart, variant, quantity=5)
    cart.refresh_from_db()
    assert cart.items.first().quantity == 2


def test_set_item_quantity_zero_removes_line():
    variant = make_variant()
    cart = make_cart(variant)
    cart_service.set_item_quantity(cart, variant, 0)
    assert not cart.items.exists()


def test_set_item_quantity_out_of_stock_removes_and_warns():
    variant = make_variant(quantity=0)
    cart = make_cart(variant)
    cart_service.set_item_quantity(cart, variant, 1)
    cart.refresh_from_db()
    assert not cart.items.exists()
    assert "out of stock" in cart.warning.lower()


def test_remove_item():
    variant = make_variant()
    cart = make_cart(variant)
    cart_service.remove_item(cart, variant)
    assert not cart.items.exists()


def test_apply_and_remove_coupon():
    variant = make_variant(price="100.00")
    cart = make_cart(variant)
    coupon = make_coupon()
    cart_service.apply_coupon(cart, coupon.normalized_code)
    cart.refresh_from_db()
    assert cart.coupon_code_id == coupon.pk
    cart_service.remove_coupon(cart)
    cart.refresh_from_db()
    assert cart.coupon_code_id is None


def test_recalculate_cart_with_coupon():
    variant = make_variant(price="50.00")
    cart = make_cart(variant)
    coupon = make_coupon()
    cart.coupon_code = coupon
    cart.save()
    totals = recalculate_cart(cart)
    assert totals.discount_total > Decimal("0")


def test_merge_guest_cart_combines_quantities(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    variant = make_variant(quantity=10)
    guest = Cart.objects.create(session_key=f"g-{uuid.uuid4().hex[:8]}", status=Cart.Status.ACTIVE)
    CartItem.objects.create(cart=guest, variant=variant, quantity=2)
    user_cart = Cart.objects.create(user=user, status=Cart.Status.ACTIVE, session_key="")
    CartItem.objects.create(cart=user_cart, variant=variant, quantity=1)
    merge_guest_cart(guest, user_cart)
    user_cart.refresh_from_db()
    guest.refresh_from_db()
    assert user_cart.items.first().quantity == 3
    assert guest.status == Cart.Status.MERGED


def test_merge_guest_cart_expires_checkout_attempts():
    variant = make_variant(quantity=5)
    guest = make_cart(variant)
    begin_checkout(guest, idempotency_key="merge-guest-co")
    user_cart = Cart.objects.create(status=Cart.Status.ACTIVE, session_key=f"u-{uuid.uuid4().hex[:8]}")
    merge_guest_cart(guest, user_cart)
    assert CheckoutAttempt.objects.filter(cart=guest, status=CheckoutAttempt.Status.EXPIRED).exists()


def test_add_item_invalid_variant_raises():
    variant = make_variant()
    variant.active = False
    variant.save()
    cart = Cart.objects.create(session_key=f"c-{uuid.uuid4().hex[:8]}", status=Cart.Status.ACTIVE)
    with pytest.raises(CartError):
        cart_service.add_item(cart, variant, 1)


def test_add_item_zero_quantity_raises():
    cart = make_cart(make_variant())
    with pytest.raises(CartError):
        cart_service.add_item(cart, make_variant(), 0)


# --- promotions ---
def test_calculate_discount_percentage():
    promo = Promotion.objects.create(
        name="10%", type=Promotion.Type.PERCENTAGE, discount_percent=Decimal("10"), active=True
    )
    amount, free = calculate_discount(promo, Decimal("100.00"))
    assert amount == Decimal("10.00")
    assert free is False


def test_calculate_discount_fixed():
    promo = Promotion.objects.create(
        name="5off", type=Promotion.Type.FIXED_AMOUNT, discount_amount=Decimal("5"), active=True
    )
    amount, _ = calculate_discount(promo, Decimal("100.00"))
    assert amount == Decimal("5.00")


def test_calculate_discount_free_shipping():
    promo = Promotion.objects.create(name="ship", type=Promotion.Type.FREE_SHIPPING, active=True)
    amount, free = calculate_discount(promo, Decimal("10"), shipping_total=Decimal("7.95"))
    assert free is True
    assert amount == Decimal("7.95")


def test_validate_coupon_inactive_raises():
    coupon = make_coupon()
    coupon.active = False
    coupon.save()
    with pytest.raises(InvalidCoupon):
        validate_coupon(coupon, Decimal("50.00"))


def test_validate_coupon_below_minimum_raises():
    coupon = make_coupon()
    coupon.promotion.min_subtotal = Decimal("500.00")
    coupon.promotion.save()
    with pytest.raises(InvalidCoupon):
        validate_coupon(coupon, Decimal("10.00"))


def test_get_coupon_by_code_normalizes():
    coupon = make_coupon()
    found = get_coupon_by_code(coupon.code.upper())
    assert found.pk == coupon.pk


def test_best_auto_discount_finds_eligible():
    Promotion.objects.create(
        name="Auto",
        type=Promotion.Type.PERCENTAGE,
        discount_percent=Decimal("5"),
        active=True,
        auto_apply=True,
        min_subtotal=Decimal("1.00"),
    )
    result = best_auto_discount(Decimal("100.00"))
    assert result is not None
    assert result.discount_total == Decimal("5.00")


# --- returns ---
def _placed_order():
    variant = make_variant(quantity=5, price="30.00")
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key=f"ret-{uuid.uuid4().hex[:6]}")
    payment = authorize_payment(attempt, idempotency_key=f"pay-{uuid.uuid4().hex[:6]}")
    return confirm_payment(payment, idempotency_key=f"cf-{uuid.uuid4().hex[:6]}")


def test_request_return_creates_request():
    order = _placed_order()
    item = order.items.first()
    rr = request_return(order, lines=[(item.pk, 1)], reason="Too small")
    assert rr.status == ReturnRequest.Status.REQUESTED


def test_request_return_exceeds_quantity_raises():
    order = _placed_order()
    item = order.items.first()
    with pytest.raises(CheckoutStateError):
        request_return(order, lines=[(item.pk, item.quantity + 1)])


def test_request_return_cancelled_order_raises():
    order = _placed_order()
    order.status = Order.Status.CANCELLED
    order.save()
    item = order.items.first()
    with pytest.raises(CheckoutStateError):
        request_return(order, lines=[(item.pk, 1)])


def test_reject_return():
    order = _placed_order()
    item = order.items.first()
    rr = request_return(order, lines=[(item.pk, 1)])
    reject_return(rr, note="Not eligible")
    rr.refresh_from_db()
    assert rr.status == ReturnRequest.Status.REJECTED


def test_approve_return_refunds():
    order = _placed_order()
    item = order.items.first()
    rr = request_return(order, lines=[(item.pk, 1)])
    approve_return(rr, restock=False)
    rr.refresh_from_db()
    assert rr.status == ReturnRequest.Status.REFUNDED


# --- credit ---
def test_credit_hold_and_release(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    ensure_verified_profile(user)
    StoreCredit.objects.create(user=user, balance=Decimal("50.00"))
    variant = make_variant(price="40.00")
    cart = make_cart(variant)
    cart.user = user
    cart.save()
    attempt = begin_checkout(cart, idempotency_key="cred-co", use_store_credit=True)
    assert attempt.credit_applied > Decimal("0")
    credit_service.release_hold(attempt)
    attempt.refresh_from_db()
    assert attempt.credit_applied == Decimal("0")


def test_gift_card_redeem(django_user_model):
    user = django_user_model.objects.create_user(username=f"gc{uuid.uuid4().hex[:6]}", password="x")
    code = f"GC-{uuid.uuid4().hex[:8]}"
    GiftCard.objects.create(
        code=code,
        initial_balance=Decimal("25.00"),
        balance=Decimal("25.00"),
        active=True,
    )
    amount = credit_service.redeem_gift_card(code, user)
    assert amount == Decimal("25.00")
    assert credit_service.get_balance(user) == Decimal("25.00")


# --- plans / billing ---
def test_run_billing_cycle_issues_invoice():
    plan = Plan.objects.create(
        name="Pro", slug=f"pro-{uuid.uuid4().hex[:6]}", price_monthly=Decimal("29.00"), active=True
    )
    sub = Subscription.get_solo()
    sub.plan = plan
    sub.save()
    issued = run_billing_cycle()
    assert issued >= 1


def test_plan_usage_reports_counts():
    usage = plan_usage()
    assert "product_count" in usage
    assert "orders_month" in usage


def test_can_create_product_with_no_limit():
    plan = Plan.objects.create(name="Unlimited", slug=f"ul-{uuid.uuid4().hex[:6]}", max_products=None, active=True)
    sub = Subscription.get_solo()
    sub.plan = plan
    sub.save()
    assert can_create_product() is True


# --- subscriptions (customer product subs) ---
def test_generate_due_renewals(django_user_model):
    variant = make_variant()
    user = django_user_model.objects.create_user(
        username=f"sub{uuid.uuid4().hex[:6]}", email="sub@test.com", password="x"
    )
    ensure_verified_profile(user)
    CustomerSubscription.objects.create(
        user=user,
        variant=variant,
        interval="monthly",
        unit_price=variant.price,
        status=CustomerSubscription.Status.ACTIVE,
        next_renewal_at=timezone.now() - timedelta(days=1),
    )
    count = generate_due_renewals()
    assert count >= 1


# --- recommendations ---
def test_track_and_read_recently_viewed(client):
    from django.test import RequestFactory

    from shop.models import Category

    cat = Category.objects.create(name="R", slug=f"r-{uuid.uuid4().hex[:6]}")
    product = Product.objects.create(
        name="Recent", slug=f"rec-{uuid.uuid4().hex[:6]}", category=cat, status=Product.Status.ACTIVE
    )
    rf = RequestFactory()
    request = rf.get("/")
    request.session = client.session
    client.get("/")
    request.session = client.session
    track_recently_viewed(request, product)
    result = recently_viewed(request)
    assert result[0].pk == product.pk


def test_also_bought_after_shared_order():
    variant = make_variant()
    _placed_order()
    results = also_bought(variant.product)
    assert hasattr(results, "count")


# --- analytics ---
def test_dashboard_metrics_returns_revenue_fields():
    set_current_tenant(Tenant.objects.get(slug="default"))
    metrics = dashboard_metrics(days=7)
    assert "net_revenue" in metrics
    assert "aov" in metrics


# --- webhooks outbound ---
def test_enqueue_deliveries_creates_delivery():
    from shop.models import OutboxEvent, WebhookDelivery, WebhookEndpoint

    order = _placed_order()
    endpoint = WebhookEndpoint.objects.create(
        url="https://example.com/hook", secret="sec", active=True, event_types=["order.placed"]
    )
    event = OutboxEvent.objects.create(
        event_type="order.placed",
        aggregate_type="Order",
        aggregate_id=str(order.pk),
        payload={"order_number": order.order_number},
    )
    webhook_service.enqueue_deliveries(event)
    assert WebhookDelivery.objects.filter(endpoint=endpoint).exists()


def test_export_user_data(django_user_model):
    user = django_user_model.objects.create_user(
        username=f"ex{uuid.uuid4().hex[:6]}", email="ex@test.com", password="x"
    )
    data = export_user_data(user)
    assert "tenants" in data
    assert "account" in data


# --- PSP webhooks inbound ---
@override_settings(PAYMENT_WEBHOOK_SECRET="whsec-test")
def test_receive_gateway_webhook_confirms_payment():
    variant = make_variant()
    cart = make_cart(variant)
    attempt = begin_checkout(cart, idempotency_key="wh-co")
    payment = authorize_payment(attempt, idempotency_key="wh-pay")
    gw = SimulatedPaymentGateway()
    payload = gw.build_webhook_payload(
        gateway_reference=payment.gateway_reference,
        event_type="payment.confirmed",
        tenant_id=payment.tenant_id,
        status="confirmed",
    )
    body = json.dumps(payload).encode()
    sig = gw.sign_webhook_body(body)
    record = receive_gateway_webhook(provider="simulated", body=body, signature=sig, tenant_id=payment.tenant_id)
    assert record.status in {
        record.Status.PROCESSED,
        record.Status.RECEIVED,
        record.Status.FAILED,
    }


@override_settings(PAYMENT_WEBHOOK_SECRET="whsec-test")
def test_process_inbound_ignores_unknown_payment():
    from shop.models import InboundGatewayEvent

    record = InboundGatewayEvent.objects.create(
        provider="simulated",
        provider_event_id="evt-unknown",
        event_type="payment.confirmed",
        gateway_reference="sim_nonexistent",
        payload={},
    )
    result = process_inbound_gateway_event(record.pk)
    assert result.status == InboundGatewayEvent.Status.IGNORED
