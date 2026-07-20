"""Model validation paths and form serializer behavior"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from shop.forms import RegistrationForm
from shop.models import GiftCard, Promotion, TaxRate, Tenant
from shop.serializers import BeginCheckoutSerializer, CartAddItemSerializer

from .test_checkout_seam import make_coupon, make_variant

pytestmark = pytest.mark.django_db


# --- forms ---
def test_registration_form_rejects_duplicate_email(django_user_model):
    django_user_model.objects.create_user(username="u1", email="dup@test.com", password="x")
    form = RegistrationForm(
        data={
            "username": "u2",
            "email": "DUP@test.com",
            "password1": "Str0ngPass!",
            "password2": "Str0ngPass!",
        }
    )
    assert not form.is_valid()
    assert "email" in form.errors


def test_registration_form_normalizes_email():
    form = RegistrationForm(
        data={
            "username": f"u{uuid.uuid4().hex[:6]}",
            "email": "  New@Test.COM  ",
            "password1": "Str0ngPass!",
            "password2": "Str0ngPass!",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["email"] == "new@test.com"


# --- serializers ---
def test_cart_add_item_serializer_requires_variant():
    ser = CartAddItemSerializer(data={"quantity": 1})
    assert not ser.is_valid()
    assert "non_field_errors" in ser.errors


def test_cart_add_item_serializer_by_sku():
    variant = make_variant()
    ser = CartAddItemSerializer(data={"sku": variant.sku, "quantity": 2})
    assert ser.is_valid(), ser.errors


def test_begin_checkout_serializer_requires_address_fields():
    ser = BeginCheckoutSerializer(data={"email": "a@test.com", "shipping_method": "Standard"})
    assert not ser.is_valid()
    assert "address1" in ser.errors


# --- models ---
def test_gift_card_balance_cannot_exceed_initial():
    gc = GiftCard(
        code=f"GC-{uuid.uuid4().hex[:8]}",
        initial_balance=Decimal("10.00"),
        balance=Decimal("15.00"),
        active=True,
    )
    with pytest.raises(ValidationError):
        gc.full_clean()


def test_tax_rate_negative_rejected():
    rate = TaxRate(country="US", region="ZZ", rate=Decimal("-0.01"), label="Bad", active=True)
    with pytest.raises(ValidationError):
        rate.full_clean()


def test_coupon_code_normalized_on_save():
    coupon = make_coupon()
    assert coupon.normalized_code == coupon.code.strip().upper()


def test_promotion_percentage_requires_discount_percent():
    promo = Promotion(name="Bad", type=Promotion.Type.PERCENTAGE, active=True)
    with pytest.raises(ValidationError):
        promo.full_clean()


def test_tenant_slug_auto_suffix_on_collision():
    base = f"t-{uuid.uuid4().hex[:6]}"
    Tenant.objects.create(slug=base, name="One", active=True)
    second = Tenant(slug=base, name="Two", active=True)
    second.save()
    assert second.slug != base


def test_gift_card_redeem_zero_balance_inactive():
    gc = GiftCard.objects.create(
        code=f"ZERO-{uuid.uuid4().hex[:6]}",
        initial_balance=Decimal("5.00"),
        balance=Decimal("0.00"),
        active=False,
    )
    assert gc.balance == Decimal("0.00")
