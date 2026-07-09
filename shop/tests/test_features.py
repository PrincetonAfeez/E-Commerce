from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from shop.models import (
    Cart,
    CheckoutAttempt,
    EmailDelivery,
    Fulfillment,
    GiftCard,
    Order,
    OutboxEvent,
    Product,
    ReturnRequest,
    Review,
    ShippingRate,
    StoreCredit,
    StoreSettings,
    TaxRate,
    WebhookDelivery,
    WebhookEndpoint,
    WishlistItem,
)
from shop.services import credit as credit_service
from shop.services import webhooks as webhook_service
from shop.services.calculators import shipping_calculator, tax_calculator
from shop.services.checkout import begin_checkout
from shop.services.orders import transition_fulfillment
from shop.services.payments import authorize_payment, confirm_payment
from shop.services.refunds import create_refund
from shop.services.returns import approve_return, request_return
from shop.services.search import search_products

from .test_checkout_seam import make_cart, make_variant

pytestmark = pytest.mark.django_db


def _order(idem="f", *, email="buyer@example.com", user=None):
    variant = make_variant(quantity=5, price="20.00")
    cart = make_cart(variant, quantity=1)
    if user is not None:
        cart.user = user
        cart.save(update_fields=["user"])
    attempt = begin_checkout(cart, idempotency_key=f"co-{idem}", contact={"email": email})
    payment = authorize_payment(attempt, idempotency_key=f"pay-{idem}")
    return confirm_payment(payment, idempotency_key=f"cf-{idem}"), variant


# --- Real transactional email delivery ---
def test_process_outbox_actually_sends_order_email():
    _order("email")
    assert OutboxEvent.objects.filter(event_type="order.confirmation_email").exists()
    call_command("process_outbox")
    assert any("confirmed" in m.subject.lower() for m in mail.outbox)
    assert EmailDelivery.objects.filter(
        template="order.confirmation_email", status=EmailDelivery.Status.SENT
    ).exists()


def test_shipped_email_sent_on_transition():
    order, _ = _order("ship")
    transition_fulfillment(order, target_status=Fulfillment.Status.PROCESSING)
    transition_fulfillment(order, target_status=Fulfillment.Status.SHIPPED)
    call_command("process_outbox")
    assert any("shipped" in m.subject.lower() for m in mail.outbox)


# --- Abandoned cart recovery ---
def test_abandoned_cart_recovery_queues_email(django_user_model):
    user = django_user_model.objects.create_user(
        username=f"u{uuid.uuid4().hex[:6]}", email="ab@example.com", password="x"
    )
    variant = make_variant(quantity=3)
    cart = make_cart(variant, quantity=1)
    cart.user = user
    cart.save(update_fields=["user"])
    Cart.objects.filter(pk=cart.pk).update(updated_at=timezone.now() - timedelta(hours=2))

    call_command("recover_abandoned_carts", "--older-than-minutes", "60")

    cart.refresh_from_db()
    assert cart.recovery_sent_at is not None
    assert OutboxEvent.objects.filter(
        event_type="cart.recovery_email", aggregate_id=str(cart.pk)
    ).exists()
    # Idempotent: a second run does not re-queue.
    call_command("recover_abandoned_carts", "--older-than-minutes", "60")
    assert OutboxEvent.objects.filter(event_type="cart.recovery_email").count() == 1


def test_recovery_skips_unsubscribed_email(django_user_model):
    from shop.models import EmailSuppression

    user = django_user_model.objects.create_user(
        username=f"u{uuid.uuid4().hex[:6]}", email="optout@example.com", password="x"
    )
    EmailSuppression.objects.create(email="optout@example.com")
    cart = make_cart(make_variant(quantity=3), quantity=1)
    cart.user = user
    cart.save(update_fields=["user"])
    Cart.objects.filter(pk=cart.pk).update(updated_at=timezone.now() - timedelta(hours=2))

    call_command("recover_abandoned_carts", "--older-than-minutes", "60")

    assert not OutboxEvent.objects.filter(event_type="cart.recovery_email").exists()


def test_unsubscribe_link_suppresses_future_marketing(client):
    from django.core import signing

    from shop.models import EmailSuppression

    token = signing.dumps("bye@example.com", salt="unsubscribe")
    resp = client.get(reverse("unsubscribe", args=[token]))

    assert resp.status_code == 200
    assert EmailSuppression.objects.filter(email="bye@example.com").exists()


def test_unsubscribe_rejects_tampered_token(client):
    resp = client.get(reverse("unsubscribe", args=["not-a-valid-token"]))
    assert resp.status_code == 400


# --- Reorder ---
def test_reorder_adds_items_back_to_cart(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    order, variant = _order("reorder", user=user)
    client.force_login(user)
    resp = client.post(reverse("orders:reorder", args=[order.order_number]))
    assert resp.status_code == 302
    assert user.carts.filter(status=Cart.Status.ACTIVE, items__variant=variant).exists()


# --- Wishlist ---
def test_wishlist_toggle_adds_then_removes(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    variant = make_variant(quantity=3)
    client.force_login(user)
    client.post(reverse("account:wishlist_toggle"), {"variant_id": variant.pk})
    assert WishlistItem.objects.filter(user=user, variant=variant).exists()
    client.post(reverse("account:wishlist_toggle"), {"variant_id": variant.pk})
    assert not WishlistItem.objects.filter(user=user, variant=variant).exists()


# --- Reviews ---
def test_review_marked_verified_after_purchase(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    order, variant = _order("review", user=user)
    client.force_login(user)
    slug = variant.product.slug
    client.post(reverse("reviews:submit", args=[slug]), {"rating": "5", "title": "Great", "body": "Nice"})
    review = Review.objects.get(product=variant.product, user=user)
    assert review.rating == 5
    assert review.verified_purchase is True
    assert review.approved is True


def test_review_rejects_out_of_range_rating(client, django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    order, variant = _order("review2", user=user)
    client.force_login(user)
    client.post(reverse("reviews:submit", args=[variant.product.slug]), {"rating": "9"})
    assert not Review.objects.filter(product=variant.product).exists()


# --- Returns / RMA ---
def test_return_request_and_approval_refunds_and_restocks(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    order, variant = _order("ret", user=user)
    variant.refresh_from_db()
    assert variant.quantity == 4  # consumed at finalization
    item = order.items.first()

    rr = request_return(order, user=user, lines=[(item.pk, 1)], reason="Too small")
    assert rr.status == ReturnRequest.Status.REQUESTED

    approve_return(rr, restock=True)

    rr.refresh_from_db()
    order.refresh_from_db()
    variant.refresh_from_db()
    assert rr.status == ReturnRequest.Status.REFUNDED
    assert order.refund_total == order.total
    assert variant.quantity == 5  # returned unit restocked


# --- Outbound webhooks ---
def test_webhook_delivery_signs_and_sends(monkeypatch):
    endpoint = WebhookEndpoint.objects.create(
        url="https://example.test/hook", secret="s3cr3t", event_types=[]
    )
    event = OutboxEvent.objects.create(
        event_type="order.confirmation_email",
        aggregate_type="Order",
        aggregate_id="1",
        payload={"order_number": "EC-1"},
    )
    assert webhook_service.enqueue_deliveries(event) == 1

    captured = {}

    def fake_post(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["sig"] = headers["X-Webhook-Signature"]
        return 200

    monkeypatch.setattr(webhook_service, "_post", fake_post)
    sent, failed = webhook_service.deliver_pending()

    assert (sent, failed) == (1, 0)
    delivery = WebhookDelivery.objects.get(endpoint=endpoint, outbox_event=event)
    assert delivery.status == WebhookDelivery.Status.SUCCESS
    assert delivery.response_code == 200
    expected = "sha256=" + webhook_service.sign("s3cr3t", captured["body"])
    assert captured["sig"] == expected


def test_webhook_endpoint_respects_subscription():
    ep = WebhookEndpoint.objects.create(url="https://x.test", secret="k", event_types=["order.shipped_email"])
    assert ep.subscribes_to("order.shipped_email")
    assert not ep.subscribes_to("order.confirmation_email")


# --- DB-backed tax/shipping ---
def test_db_shipping_rate_overrides_default():
    # No rows -> flat default.
    assert shipping_calculator.quote(Decimal("50.00"), method="Standard").amount == Decimal("7.95")
    ShippingRate.objects.create(method="Standard", flat_amount=Decimal("5.00"), free_threshold=Decimal("40.00"))
    assert shipping_calculator.quote(Decimal("30.00"), method="Standard").amount == Decimal("5.00")
    assert shipping_calculator.quote(Decimal("50.00"), method="Standard").amount == Decimal("0.00")


def test_db_tax_rate_overrides_default():
    assert tax_calculator.quote(Decimal("100.00"), country="US").amount == Decimal("8.25")
    TaxRate.objects.create(country="US", region="", rate=Decimal("0.1000"), label="State")
    assert tax_calculator.quote(Decimal("100.00"), country="US").amount == Decimal("10.00")


# --- Merchant dashboard ---
def test_dashboard_renders_for_staff(client, django_user_model):
    staff = django_user_model.objects.create_user(
        username=f"s{uuid.uuid4().hex[:6]}", password="x", is_staff=True, is_superuser=True
    )
    _order("dash")
    client.force_login(staff)
    resp = client.get(reverse("staff_ops:dashboard"))
    assert resp.status_code == 200
    assert b"Net revenue" in resp.content


# --- Search ---
def test_search_finds_product_by_name():
    variant = make_variant()
    results = list(search_products(Product.objects.filter(status=Product.Status.ACTIVE), "Product"))
    assert variant.product in results
    assert list(search_products(Product.objects.all(), "zzz-no-match")) == []


# --- Gift cards & store credit ---
def _credit_cart(user, *, price="20.00", qty=1):
    variant = make_variant(quantity=5, price=price)
    cart = make_cart(variant, quantity=qty)
    cart.user = user
    cart.save(update_fields=["user"])
    return cart, variant


def test_gift_card_redeems_into_store_credit(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    GiftCard.objects.create(code="GIFT50", initial_balance=Decimal("50.00"), balance=Decimal("50.00"))
    amount = credit_service.redeem_gift_card("gift50", user)
    assert amount == Decimal("50.00")
    assert credit_service.get_balance(user) == Decimal("50.00")
    # Single use: a second redeem fails.
    with pytest.raises(Exception):
        credit_service.redeem_gift_card("GIFT50", user)


def test_store_credit_reduces_charge_and_is_spent_on_finalize(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    StoreCredit.objects.create(user=user, balance=Decimal("10.00"))
    cart, _ = _credit_cart(user)

    attempt = begin_checkout(cart, idempotency_key="cr1", use_store_credit=True)
    assert attempt.credit_applied == Decimal("10.00")
    assert attempt.amount_due == attempt.total - Decimal("10.00")

    payment = authorize_payment(attempt, idempotency_key="cr1-pay")
    assert payment.amount == attempt.amount_due  # gateway charged only the remainder
    order = confirm_payment(payment, idempotency_key="cr1-cf")

    order.refresh_from_db()
    assert order.credit_applied == Decimal("10.00")
    assert credit_service.get_balance(user) == Decimal("0.00")  # spent, not restored


def test_failed_payment_restores_held_credit(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    StoreCredit.objects.create(user=user, balance=Decimal("10.00"))
    cart, _ = _credit_cart(user)

    attempt = begin_checkout(cart, idempotency_key="cr2", use_store_credit=True)
    assert credit_service.get_balance(user) == Decimal("0.00")  # held
    authorize_payment(attempt, idempotency_key="cr2-pay", card_token="tok_decline")

    attempt.refresh_from_db()
    assert attempt.status == CheckoutAttempt.Status.FAILED
    assert attempt.credit_applied == Decimal("0.00")
    assert credit_service.get_balance(user) == Decimal("10.00")  # restored


def test_refund_of_credit_paid_order_returns_to_store_credit(django_user_model):
    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    StoreCredit.objects.create(user=user, balance=Decimal("100.00"))
    cart, _ = _credit_cart(user)

    attempt = begin_checkout(cart, idempotency_key="cr3", use_store_credit=True)
    order_total = attempt.total
    assert attempt.amount_due == Decimal("0.00")  # fully covered by credit
    payment = authorize_payment(attempt, idempotency_key="cr3-pay")
    order = confirm_payment(payment, idempotency_key="cr3-cf")

    spent_balance = credit_service.get_balance(user)
    assert spent_balance == Decimal("100.00") - order_total

    create_refund(order, amount=order_total, idempotency_key="cr3-refund", reason="return")

    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    # The credit-paid portion is returned to store credit, not the gateway.
    assert credit_service.get_balance(user) == Decimal("100.00")


# --- Store settings, theming & billing (Wave 3a) ---
def test_theme_css_reflects_and_sanitizes_color(client):
    store = StoreSettings.get_solo()
    store.primary_color = "#ff0000"
    store.save()
    resp = client.get("/theme.css")
    assert resp["Content-Type"].startswith("text/css")
    assert b"#ff0000" in resp.content
    # A malicious value is rejected and the default is used (no CSS injection).
    store.primary_color = "red}body{display:none"
    store.save()
    resp = client.get("/theme.css")
    assert b"display:none" not in resp.content
    assert b"#3b6fe6" in resp.content


def test_staff_settings_update(client, django_user_model):
    staff = django_user_model.objects.create_user(
        username=f"s{uuid.uuid4().hex[:6]}", password="x", is_staff=True, is_superuser=True
    )
    client.force_login(staff)
    client.post(
        reverse("staff_ops:settings"),
        {"store_name": "My Shop", "primary_color": "#123456", "currency": "USD"},
    )
    store = StoreSettings.get_solo()
    assert store.store_name == "My Shop"
    assert store.primary_color == "#123456"


def test_staff_billing_switch_plan(client, django_user_model):
    from shop.models import Plan, Subscription

    staff = django_user_model.objects.create_user(
        username=f"s{uuid.uuid4().hex[:6]}", password="x", is_staff=True, is_superuser=True
    )
    Plan.objects.create(name="Pro", slug="pro", price_monthly=Decimal("10.00"), active=True)
    client.force_login(staff)
    resp = client.post(reverse("staff_ops:billing"), {"plan": "pro"})
    assert resp.status_code == 302
    assert Subscription.get_solo().plan.slug == "pro"


def test_store_name_in_page_via_context_processor(client):
    store = StoreSettings.get_solo()
    store.store_name = "Brandy McBrandface"
    store.save()
    resp = client.get(reverse("catalog:list"))
    assert b"Brandy McBrandface" in resp.content


# --- Tier 0: sale pricing, auto discounts, SEO ---
def test_variant_sale_pricing():
    variant = make_variant(price="80.00")
    variant.compare_at_price = Decimal("100.00")
    variant.save()
    assert variant.on_sale is True
    assert variant.discount_percent == 20
    variant.compare_at_price = None
    variant.save()
    assert variant.on_sale is False
    assert variant.discount_percent == 0


def test_auto_apply_discount_without_coupon():
    from shop.models import Promotion
    from shop.services.cart import recalculate_cart

    variant = make_variant(quantity=5, price="100.00")
    cart = make_cart(variant, quantity=1)  # subtotal 100
    Promotion.objects.create(
        name="Auto10",
        type=Promotion.Type.PERCENTAGE,
        active=True,
        auto_apply=True,
        discount_percent=Decimal("10.00"),
        min_subtotal=Decimal("50.00"),
    )
    totals = recalculate_cart(cart)
    assert totals.discount_total == Decimal("10.00")
    assert totals.discount_label == "Auto10"


def test_auto_apply_skipped_below_minimum():
    from shop.models import Promotion
    from shop.services.cart import recalculate_cart

    variant = make_variant(quantity=5, price="20.00")
    cart = make_cart(variant, quantity=1)  # subtotal 20
    Promotion.objects.create(
        name="Auto10",
        type=Promotion.Type.PERCENTAGE,
        active=True,
        auto_apply=True,
        discount_percent=Decimal("10.00"),
        min_subtotal=Decimal("50.00"),
    )
    totals = recalculate_cart(cart)
    assert totals.discount_total == Decimal("0.00")


def test_seo_tags_sitemap_and_robots(client):
    variant = make_variant()
    detail = client.get(reverse("catalog:detail", args=[variant.product.slug]))
    assert b"application/ld+json" in detail.content
    assert b'property="og:title"' in detail.content
    assert b'rel="canonical"' in detail.content

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert b"<urlset" in sitemap.content
    assert variant.product.slug.encode() in sitemap.content

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert b"Sitemap:" in robots.content


def test_also_bought_recommendations():
    from shop.models import CartItem
    from shop.services.recommendations import also_bought

    va = make_variant(quantity=5, price="20.00")
    vb = make_variant(quantity=5, price="30.00")
    cart = Cart.objects.create(session_key=f"s-{uuid.uuid4().hex}")
    CartItem.objects.create(cart=cart, variant=va, quantity=1)
    CartItem.objects.create(cart=cart, variant=vb, quantity=1)
    attempt = begin_checkout(cart, idempotency_key="ab-co")
    payment = authorize_payment(attempt, idempotency_key="ab-pay")
    confirm_payment(payment, idempotency_key="ab-cf")

    recs = list(also_bought(va.product))
    assert vb.product in recs
    assert va.product not in recs


# --- Tier 1: B2B group pricing ---
def test_b2b_group_pricing(django_user_model):
    from shop.models import AccountProfile, CustomerGroup, PriceListEntry
    from shop.services.pricing import effective_price

    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    variant = make_variant(price="100.00")
    group = CustomerGroup.objects.create(name="Wholesale", percent_off=Decimal("20.00"))
    AccountProfile.objects.create(user=user, customer_group=group)

    assert effective_price(variant, user) == Decimal("80.00")          # % off
    PriceListEntry.objects.create(group=group, variant=variant, price=Decimal("70.00"))
    assert effective_price(variant, user) == Decimal("70.00")          # entry overrides
    assert effective_price(variant, None) == Decimal("100.00")         # anon -> base


def test_b2b_pricing_flows_into_checkout(django_user_model):
    from shop.models import AccountProfile, CustomerGroup

    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    variant = make_variant(quantity=5, price="100.00")
    group = CustomerGroup.objects.create(name="Wholesale", percent_off=Decimal("25.00"))
    AccountProfile.objects.create(user=user, customer_group=group)
    cart = make_cart(variant, quantity=1)
    cart.user = user
    cart.save(update_fields=["user"])

    attempt = begin_checkout(cart, idempotency_key="b2b-co")
    assert attempt.subtotal == Decimal("75.00")  # 100 - 25%
    line = attempt.line_snapshots.first()
    assert line.unit_price == Decimal("75.00")


# --- Tier 1: subscriptions ---
def test_subscription_created_and_renewed(django_user_model):
    from shop.models import CustomerSubscription
    from shop.services.subscriptions import generate_due_renewals

    user = django_user_model.objects.create_user(username=f"u{uuid.uuid4().hex[:6]}", password="x")
    variant = make_variant(quantity=10, price="20.00")
    variant.subscription_interval = "monthly"
    variant.save()
    cart = make_cart(variant, quantity=1)
    cart.user = user
    cart.save(update_fields=["user"])
    attempt = begin_checkout(cart, idempotency_key="sub-co")
    payment = authorize_payment(attempt, idempotency_key="sub-pay")
    confirm_payment(payment, idempotency_key="sub-cf")

    sub = CustomerSubscription.objects.get(user=user, variant=variant)
    assert sub.status == CustomerSubscription.Status.ACTIVE

    orders_before = Order.objects.count()
    CustomerSubscription.objects.filter(pk=sub.pk).update(
        next_renewal_at=timezone.now() - timedelta(days=1)
    )
    assert generate_due_renewals() == 1
    assert Order.objects.count() == orders_before + 1  # a renewal order was created
    sub.refresh_from_db()
    assert sub.next_renewal_at > timezone.now()
    # No duplicate subscription created by the renewal order.
    assert CustomerSubscription.objects.filter(user=user, variant=variant).count() == 1


# --- Privacy: data export + account deletion ---
def test_account_data_export(client, django_user_model):
    user = django_user_model.objects.create_user(
        username=f"u{uuid.uuid4().hex[:6]}", email="e@x.test", password="x"
    )
    order, _ = _order("export", user=user)
    client.force_login(user)
    resp = client.get(reverse("account:data_export"))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/json"
    import json

    data = json.loads(resp.content)
    assert data["account"]["email"] == "e@x.test"
    assert any(o["order_number"] == order.order_number for o in data["orders"])


def test_account_deletion_scrubs_pii_keeps_order(client, django_user_model):
    user = django_user_model.objects.create_user(
        username=f"u{uuid.uuid4().hex[:6]}", email="e@x.test", password="x"
    )
    order, _ = _order("del", user=user, email="buyer@x.test")
    order_number = order.order_number
    client.force_login(user)
    resp = client.post(reverse("account:delete"))
    assert resp.status_code == 302
    assert not django_user_model.objects.filter(pk=user.pk).exists()
    order.refresh_from_db()
    assert order.user_id is None  # unlinked
    assert order.guest_email == ""  # PII scrubbed
    assert Order.objects.filter(order_number=order_number).exists()  # record retained
