# Coupon validation, auto-apply discounts, redemption, and order discount snapshots
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from shop.models import (
    CouponCode,
    Order,
    OrderDiscountSnapshot,
    Promotion,
    PromotionRedemption,
    normalize_coupon_code,
)

from .exceptions import InvalidCoupon
from .money import quantize_money


@dataclass(frozen=True)
class CouponQuote:
    coupon: CouponCode
    discount_total: Decimal
    free_shipping: bool = False


def calculate_discount(
    promotion: Promotion,
    subtotal: Decimal,
    *,
    shipping_total: Decimal = Decimal("0.00"),
) -> tuple[Decimal, bool]:
    if promotion.type == Promotion.Type.PERCENTAGE:
        amount = subtotal * (promotion.discount_percent / Decimal("100"))
        return min(quantize_money(amount), subtotal), False
    if promotion.type == Promotion.Type.FIXED_AMOUNT:
        return min(quantize_money(promotion.discount_amount), subtotal), False
    if promotion.type == Promotion.Type.FREE_SHIPPING:
        return quantize_money(shipping_total), True
    return Decimal("0.00"), False


def validate_coupon(
    coupon: CouponCode,
    subtotal: Decimal,
    *,
    user=None,
    guest_email: str = "",
    shipping_total: Decimal = Decimal("0.00"),
) -> CouponQuote:
    now = timezone.now()
    promotion = coupon.promotion
    if not coupon.active or not promotion.active:
        raise InvalidCoupon("Coupon is not active.")
    if promotion.starts_at and promotion.starts_at > now:
        raise InvalidCoupon("Coupon is not active yet.")
    if promotion.ends_at and promotion.ends_at <= now:
        raise InvalidCoupon("Coupon has expired.")
    if subtotal < promotion.min_subtotal:
        raise InvalidCoupon("Cart subtotal does not meet the coupon minimum.")
    if promotion.usage_limit is not None and promotion.used_count >= promotion.usage_limit:
        raise InvalidCoupon("Coupon has reached its usage limit.")
    if user and promotion.per_customer_usage_limit is not None:
        existing = PromotionRedemption.objects.filter(
            promotion=promotion, user=user, released=False
        ).count()
        if existing >= promotion.per_customer_usage_limit:
            raise InvalidCoupon("Coupon has already been used by this customer.")

    discount_total, free_shipping = calculate_discount(
        promotion, subtotal, shipping_total=shipping_total
    )
    return CouponQuote(coupon=coupon, discount_total=discount_total, free_shipping=free_shipping)


@dataclass(frozen=True)
class AutoDiscount:
    promotion: Promotion
    discount_total: Decimal
    free_shipping: bool
    label: str


def best_auto_discount(
    subtotal: Decimal, *, shipping_total: Decimal = Decimal("0.00")
) -> AutoDiscount | None:
    """Evaluate active auto-apply promotions and return the single best one (no code)."""
    now = timezone.now()
    candidates = Promotion.objects.filter(active=True, auto_apply=True)
    best: AutoDiscount | None = None
    best_key = None
    for promo in candidates:
        if promo.starts_at and promo.starts_at > now:
            continue
        if promo.ends_at and promo.ends_at <= now:
            continue
        if subtotal < promo.min_subtotal:
            continue
        discount, free_shipping = calculate_discount(promo, subtotal, shipping_total=shipping_total)
        if discount <= 0 and not free_shipping:
            continue
        # Rank by (priority, effective value); pick the strongest.
        value = discount + (shipping_total if free_shipping else Decimal("0.00"))
        key = (promo.priority, value)
        if best is None or key > best_key:
            best = AutoDiscount(
                promotion=promo, discount_total=discount, free_shipping=free_shipping, label=promo.name
            )
            best_key = key
    return best


def get_coupon_by_code(code: str) -> CouponCode:
    normalized = normalize_coupon_code(code)
    if not normalized:
        raise InvalidCoupon("Enter a coupon code.")
    try:
        return CouponCode.objects.select_related("promotion").get(normalized_code=normalized)
    except CouponCode.DoesNotExist as exc:
        raise InvalidCoupon("Coupon was not found.") from exc


def redeem_coupon_for_order(order: Order) -> PromotionRedemption | None:
    if not order.coupon_code_id:
        return None
    coupon = CouponCode.objects.select_related("promotion").select_for_update().get(pk=order.coupon_code_id)
    promotion = Promotion.objects.select_for_update().get(pk=coupon.promotion_id)
    validate_coupon(
        coupon,
        order.subtotal,
        user=order.user,
        guest_email=order.guest_email,
        shipping_total=order.shipping_total,
    )

    # Per-customer limit is enforced here under the promotion row lock (serialized),
    # honouring per_customer_usage_limit >= 1 rather than a hard unique-per-user rule.
    if order.user_id and promotion.per_customer_usage_limit is not None:
        active_for_user = PromotionRedemption.objects.filter(
            promotion=promotion, user_id=order.user_id, released=False
        ).count()
        if active_for_user >= promotion.per_customer_usage_limit:
            raise InvalidCoupon("Coupon has already been used by this customer.")

    if promotion.usage_limit is None:
        Promotion.objects.filter(pk=promotion.pk).update(used_count=F("used_count") + 1)
    else:
        updated = Promotion.objects.filter(pk=promotion.pk, used_count__lt=F("usage_limit")).update(
            used_count=F("used_count") + 1
        )
        if updated != 1:
            raise InvalidCoupon("Coupon has reached its usage limit.")

    try:
        redemption = PromotionRedemption.objects.create(
            promotion=promotion,
            coupon_code=coupon,
            order=order,
            user=order.user,
            guest_email=order.guest_email,
            discount_amount=order.discount_total,
        )
    except IntegrityError as exc:
        raise InvalidCoupon("Coupon redemption already exists for this order or customer.") from exc

    if order.discount_total:
        OrderDiscountSnapshot.objects.create(
            order=order,
            promotion=promotion,
            coupon_code=coupon.normalized_code,
            label=promotion.name,
            amount=order.discount_total,
        )
    return redemption


def release_redemptions_for_order(order: Order) -> None:
    with transaction.atomic():
        redemptions = (
            PromotionRedemption.objects.select_for_update()
            .select_related("promotion")
            .filter(order=order, released=False, promotion__release_redemption_on_refund=True)
        )
        for redemption in redemptions:
            Promotion.objects.filter(pk=redemption.promotion_id, used_count__gt=0).update(
                used_count=F("used_count") - 1
            )
            redemption.released = True
            redemption.save(update_fields=["released", "updated_at"])


def preview_coupon_discount(code: str, subtotal: Decimal, *, user=None, shipping_total=Decimal("0.00")) -> CouponQuote:
    coupon = get_coupon_by_code(code)
    return validate_coupon(coupon, subtotal, user=user, shipping_total=shipping_total)
