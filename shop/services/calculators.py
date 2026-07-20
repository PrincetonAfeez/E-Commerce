"""Flat-rate shipping and tax calculator abstractions with DB-backed overrides"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import clamp_money, quantize_money


@dataclass(frozen=True)
class ShippingQuote:
    method: str
    amount: Decimal


@dataclass(frozen=True)
class TaxQuote:
    rate: Decimal
    amount: Decimal
    label: str = "Configured sales tax"


class ShippingCalculator:
    def quote(self, subtotal: Decimal, *, method: str = "Standard") -> ShippingQuote:
        raise NotImplementedError


class FlatRateShippingCalculator(ShippingCalculator):
    def __init__(
        self,
        *,
        standard_rate: Decimal = Decimal("7.95"),
        express_rate: Decimal = Decimal("14.95"),
        free_threshold: Decimal = Decimal("100.00"),
    ):
        self.standard_rate = standard_rate
        self.express_rate = express_rate
        self.free_threshold = free_threshold

    def quote(self, subtotal: Decimal, *, method: str = "Standard") -> ShippingQuote:
        normalized = method if method in {"Standard", "Express"} else "Standard"
        if subtotal >= self.free_threshold and normalized == "Standard":
            return ShippingQuote(method=normalized, amount=Decimal("0.00"))
        amount = self.express_rate if normalized == "Express" else self.standard_rate
        return ShippingQuote(method=normalized, amount=quantize_money(amount))


class TaxCalculator:
    def quote(self, taxable_amount: Decimal, *, region: str = "", country: str = "US") -> TaxQuote:
        raise NotImplementedError


class ConfiguredRateTaxCalculator(TaxCalculator):
    def __init__(self, *, rate: Decimal = Decimal("0.0825")):
        self.rate = rate

    def quote(self, taxable_amount: Decimal, *, region: str = "", country: str = "US") -> TaxQuote:
        if country and country.upper() != "US":
            return TaxQuote(rate=Decimal("0.0000"), amount=Decimal("0.00"), label="No tax")
        amount = clamp_money(taxable_amount * self.rate)
        return TaxQuote(rate=self.rate, amount=amount)


class DBTaxCalculator(TaxCalculator):
    """Merchant-configurable tax rates from TaxRate rows; falls back to the flat default."""

    def __init__(self, fallback: TaxCalculator | None = None):
        self.fallback = fallback or ConfiguredRateTaxCalculator()

    def quote(self, taxable_amount: Decimal, *, region: str = "", country: str = "US") -> TaxQuote:
        from shop.models import TaxRate

        country = (country or "US").upper()
        rates = list(TaxRate.objects.filter(active=True, country=country))
        if rates:
            # Prefer an exact region match, else a country-wide (blank region) rule.
            match = next((r for r in rates if r.region and r.region.lower() == region.lower()), None)
            match = match or next((r for r in rates if not r.region), None)
            if match:
                return TaxQuote(rate=match.rate, amount=clamp_money(taxable_amount * match.rate), label=match.label)
            return TaxQuote(rate=Decimal("0.0000"), amount=Decimal("0.00"), label="No tax")
        return self.fallback.quote(taxable_amount, region=region, country=country)


class DBShippingCalculator(ShippingCalculator):
    """Merchant-configurable shipping rates from ShippingRate rows; falls back to flat."""

    def __init__(self, fallback: ShippingCalculator | None = None):
        self.fallback = fallback or FlatRateShippingCalculator()

    def quote(self, subtotal: Decimal, *, method: str = "Standard") -> ShippingQuote:
        from shop.models import ShippingRate

        rate = ShippingRate.objects.filter(active=True, method=method).first()
        if rate is None:
            return self.fallback.quote(subtotal, method=method)
        if rate.free_threshold is not None and subtotal >= rate.free_threshold:
            return ShippingQuote(method=rate.method, amount=Decimal("0.00"))
        if subtotal < rate.min_subtotal:
            return self.fallback.quote(subtotal, method=method)
        return ShippingQuote(method=rate.method, amount=quantize_money(rate.flat_amount))


# DB-backed by default, with the flat/configured implementations as fallback.
shipping_calculator = DBShippingCalculator()
tax_calculator = DBTaxCalculator()
