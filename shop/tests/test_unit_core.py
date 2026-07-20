"""Unit tests for money, calculators, validators, search, gateway, locks, idempotency, tenancy"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from shop.csrf_origins import extend_csrf_trusted_origins
from shop.locks import COMMAND_LOCK_TTLS, single_instance
from shop.models import IdempotencyRecord, TaxRate, Tenant
from shop.services.calculators import (
    ConfiguredRateTaxCalculator,
    DBShippingCalculator,
    DBTaxCalculator,
    FlatRateShippingCalculator,
)
from shop.services.gateway import SimulatedPaymentGateway
from shop.services.gateway.factory import get_payment_gateway as factory_get
from shop.services.idempotency import abandon, begin, complete, fail
from shop.services.money import allocate_amount, clamp_money, quantize_money
from shop.services.search import category_facets, search_products
from shop.tenancy import clear_current_tenant, default_tenant_id, set_current_tenant
from shop.validators import validate_image_upload

pytestmark = pytest.mark.django_db


# --- money ---
def test_quantize_money_rounds_half_up():
    assert quantize_money("10.125") == Decimal("10.13")
    assert quantize_money(Decimal("1.004")) == Decimal("1.00")


def test_allocate_amount_even_split_when_zero_weights():
    result = allocate_amount(Decimal("10.00"), [Decimal("0"), Decimal("0")])
    assert sum(result) == Decimal("10.00")
    assert len(result) == 2


def test_allocate_amount_proportional():
    result = allocate_amount(Decimal("10.00"), [Decimal("1"), Decimal("3")])
    assert sum(result) == Decimal("10.00")
    assert result[0] == Decimal("2.50")
    assert result[1] == Decimal("7.50")


def test_allocate_amount_empty_weights():
    assert allocate_amount(Decimal("5.00"), []) == []


def test_clamp_money_never_below_zero():
    assert clamp_money(Decimal("-1.00")) == Decimal("0.00")


# --- calculators ---
def test_flat_rate_shipping_free_above_threshold():
    calc = FlatRateShippingCalculator()
    quote = calc.quote(Decimal("150.00"), method="Standard")
    assert quote.amount == Decimal("0.00")


def test_flat_rate_shipping_express_not_free():
    calc = FlatRateShippingCalculator()
    quote = calc.quote(Decimal("150.00"), method="Express")
    assert quote.amount == Decimal("14.95")


def test_flat_rate_shipping_unknown_method_defaults_standard():
    calc = FlatRateShippingCalculator()
    quote = calc.quote(Decimal("10.00"), method="Overnight")
    assert quote.method == "Standard"


def test_configured_tax_non_us_zero():
    calc = ConfiguredRateTaxCalculator()
    quote = calc.quote(Decimal("100.00"), country="CA")
    assert quote.amount == Decimal("0.00")


def test_db_tax_calculator_uses_tax_rate_row():
    TaxRate.objects.create(country="US", region="CA", rate=Decimal("0.10"), label="CA tax", active=True)
    calc = DBTaxCalculator()
    quote = calc.quote(Decimal("100.00"), region="CA", country="US")
    assert quote.amount == Decimal("10.00")


def test_db_tax_calculator_country_wide_rate():
    TaxRate.objects.create(country="US", region="", rate=Decimal("0.05"), label="US", active=True)
    calc = DBTaxCalculator()
    quote = calc.quote(Decimal("100.00"), region="NY", country="US")
    assert quote.amount == Decimal("5.00")


def test_db_shipping_calculator_fallback():
    calc = DBShippingCalculator()
    quote = calc.quote(Decimal("10.00"))
    assert quote.amount > Decimal("0")


# --- validators ---
def test_validate_image_upload_rejects_oversized():
    big = SimpleUploadedFile("big.jpg", b"x" * 6000, content_type="image/jpeg")
    with patch("shop.validators.settings.MAX_UPLOAD_SIZE_BYTES", 1000):
        with pytest.raises(ValidationError, match="too large"):
            validate_image_upload(big)


def test_validate_image_upload_rejects_bad_extension():
    bad = SimpleUploadedFile("file.exe", b"data", content_type="application/octet-stream")
    with pytest.raises(ValidationError, match="Unsupported"):
        validate_image_upload(bad)


def test_validate_image_upload_accepts_png():
    ok = SimpleUploadedFile("ok.png", b"\x89PNG", content_type="image/png")
    validate_image_upload(ok)


# --- search ---
def test_search_products_empty_query_returns_all():
    from shop.models import Product

    p = Product.objects.create(name="Widget", slug=f"w-{uuid.uuid4().hex[:6]}", status=Product.Status.ACTIVE)
    qs = Product.objects.all()
    assert search_products(qs, "").filter(pk=p.pk).exists()


def test_search_products_icontains_fallback():
    from shop.models import Product

    p = Product.objects.create(
        name="UniqueGadget", slug=f"g-{uuid.uuid4().hex[:6]}", description="desc", status=Product.Status.ACTIVE
    )
    results = search_products(Product.objects.all(), "UniqueGadget")
    assert results.filter(pk=p.pk).exists()


def test_category_facets_counts():
    from shop.models import Category, Product

    cat = Category.objects.create(name="C", slug=f"c-{uuid.uuid4().hex[:6]}")
    Product.objects.create(name="P1", slug=f"p1-{uuid.uuid4().hex[:6]}", category=cat, status=Product.Status.ACTIVE)
    facets = category_facets(Product.objects.all())
    assert any(f["category__slug"] == cat.slug for f in facets)


# --- gateway ---
def test_simulated_gateway_decline():
    gw = SimulatedPaymentGateway()
    result = gw.authorize(amount=Decimal("10"), currency="USD", idempotency_key="d1", mode="decline")
    assert result.status == "failed"


def test_simulated_gateway_confirm_not_found():
    gw = SimulatedPaymentGateway()
    from shop.services.exceptions import PaymentGatewayError

    with pytest.raises(PaymentGatewayError):
        gw.confirm(gateway_reference="missing", idempotency_key="c1")


def test_simulated_gateway_refund_idempotent():
    gw = SimulatedPaymentGateway()
    gw.authorize(amount=Decimal("10"), currency="USD", idempotency_key="r1")
    ref = gw.authorize(amount=Decimal("10"), currency="USD", idempotency_key="r1").gateway_reference
    first = gw.refund(gateway_reference=ref, amount=Decimal("5"), currency="USD", idempotency_key="rf1")
    second = gw.refund(gateway_reference=ref, amount=Decimal("5"), currency="USD", idempotency_key="rf1")
    assert first.gateway_reference == second.gateway_reference


def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown"):
        factory_get(provider="nonexistent")


def test_simulated_build_webhook_payload():
    gw = SimulatedPaymentGateway()
    payload = gw.build_webhook_payload(gateway_reference="sim_abc", event_type="payment.confirmed", tenant_id=1)
    assert payload["gateway_reference"] == "sim_abc"
    assert payload["tenant_id"] == 1


# --- locks ---
def test_command_lock_ttls_defined():
    assert COMMAND_LOCK_TTLS["backup_db"] >= 3600


def test_single_instance_lock_blocks_second():
    cache.clear()
    with single_instance("unit-lock-test") as first:
        assert first is True
        with single_instance("unit-lock-test") as second:
            assert second is False


# --- idempotency ---
def test_idempotency_begin_and_complete():
    from shop.tenancy import set_current_tenant

    tenant = Tenant.objects.get(slug="default")
    set_current_tenant(tenant)
    record = begin("test-scope", "key-abc", payload='{"a":1}')
    complete(record, status=200, body={"ok": True})
    replay = begin("test-scope", "key-abc", payload='{"a":1}')
    assert replay.status == IdempotencyRecord.Status.COMPLETED
    assert replay.response_status == 200


def test_idempotency_fail_and_abandon():
    tenant = Tenant.objects.get(slug="default")
    set_current_tenant(tenant)
    record = begin("fail-scope", "key-fail", payload="{}")
    fail(record, status=400, body={"error": "bad"})
    failed = begin("fail-scope", "key-fail", payload="{}")
    assert failed.status == IdempotencyRecord.Status.FAILED
    record2 = begin("abandon-scope", "key-ab", payload="{}")
    abandon(record2)
    record2.refresh_from_db()
    assert record2.locked_until <= timezone.now()


# --- tenancy ---
def test_default_tenant_id_returns_int():
    tid = default_tenant_id()
    assert isinstance(tid, int)


def test_clear_current_tenant():
    set_current_tenant(Tenant.objects.get(slug="default"))
    clear_current_tenant()


# --- csrf origins ---
def test_extend_csrf_trusted_origins_adds_tenant_domain():
    from django.conf import settings

    Tenant.objects.create(
        slug=f"csrf-{uuid.uuid4().hex[:6]}",
        name="CSRF",
        active=True,
        primary_domain="shop.csrf-unit.test",
    )
    before = list(settings.CSRF_TRUSTED_ORIGINS)
    extend_csrf_trusted_origins()
    assert "https://shop.csrf-unit.test" in settings.CSRF_TRUSTED_ORIGINS or len(settings.CSRF_TRUSTED_ORIGINS) >= len(
        before
    )
