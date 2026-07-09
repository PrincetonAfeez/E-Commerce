from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

CENT = Decimal("0.01")


def quantize_money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate_amount(amount: Decimal, weights: Iterable[Decimal]) -> list[Decimal]:
    weights = [Decimal(weight) for weight in weights]
    amount = quantize_money(amount)
    total_weight = sum(weights, Decimal("0.00"))
    if not weights or amount == 0:
        return [Decimal("0.00") for _ in weights]

    # When every weight is zero (e.g. a fully discounted order) fall back to an even
    # split so the amount still reconciles exactly instead of being silently dropped.
    if total_weight == 0:
        weights = [Decimal("1") for _ in weights]
        total_weight = Decimal(len(weights))

    allocations: list[Decimal] = []
    remainder = amount
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            share = remainder
        else:
            share = quantize_money(amount * (weight / total_weight))
            remainder -= share
        allocations.append(quantize_money(share))
    return allocations


def clamp_money(value: Decimal, lower: Decimal = Decimal("0.00")) -> Decimal:
    return max(quantize_money(value), lower)
