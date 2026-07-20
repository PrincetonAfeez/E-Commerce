"""Cart lifecycle: get/create, add/update items, coupons, and total recalculation"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.http import HttpRequest

from shop.models import Cart, CartItem, CheckoutAttempt, CouponCode, ProductVariant

from .calculators import shipping_calculator, tax_calculator
from .exceptions import CartError, InvalidCoupon, OutOfStock
from .inventory import available_to_sell
from .money import clamp_money, quantize_money
from .pricing import effective_price
from .promotions import best_auto_discount, get_coupon_by_code, validate_coupon


@dataclass(frozen=True)
class CartTotals:
    subtotal: Decimal
    discount_total: Decimal
    shipping_total: Decimal
    tax_total: Decimal
    total: Decimal
    coupon: CouponCode | None
    shipping_method: str = "Standard"
    discount_label: str = ""


def ensure_session_key(request: HttpRequest) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def get_or_create_cart_for_request(request: HttpRequest) -> Cart:
    session_key = ensure_session_key(request)
    with transaction.atomic():
        guest_cart = Cart.objects.filter(
            user__isnull=True,
            session_key=session_key,
            status=Cart.Status.ACTIVE,
        ).first()

        if request.user.is_authenticated:
            try:
                user_cart, _ = Cart.objects.get_or_create(
                    user=request.user,
                    status=Cart.Status.ACTIVE,
                    defaults={"session_key": ""},
                )
            except IntegrityError:
                user_cart = Cart.objects.filter(
                    user=request.user,
                    status=Cart.Status.ACTIVE,
                ).first()
                if user_cart is None:
                    raise
            if guest_cart and guest_cart.pk != user_cart.pk:
                merge_guest_cart(guest_cart, user_cart)
            return user_cart

        try:
            cart, _ = Cart.objects.get_or_create(
                user=None,
                session_key=session_key,
                status=Cart.Status.ACTIVE,
                defaults={},
            )
        except IntegrityError:
            cart = Cart.objects.filter(
                user=None,
                session_key=session_key,
                status=Cart.Status.ACTIVE,
            ).first()
            if cart is None:
                raise
        return cart


def subtotal_for_cart(cart: Cart) -> Decimal:
    subtotal = Decimal("0.00")
    for item in cart.items.select_related("variant"):
        subtotal += effective_price(item.variant, cart.user) * item.quantity
    return quantize_money(subtotal)


def refresh_cart_coupon(cart: Cart, *, guest_email: str = "") -> None:
    """Persist removal of a coupon that is no longer valid (mutation path only)."""
    if not cart.coupon_code_id:
        return
    subtotal = subtotal_for_cart(cart)
    shipping_quote = shipping_calculator.quote(subtotal)
    try:
        validate_coupon(
            cart.coupon_code,
            subtotal,
            user=cart.user,
            guest_email=guest_email,
            shipping_total=shipping_quote.amount,
        )
    except InvalidCoupon:
        cart.coupon_code = None
        cart.warning = "Coupon was removed because it is no longer valid."
        cart.save(update_fields=["coupon_code", "warning", "updated_at"])


def recalculate_cart(
    cart: Cart,
    *,
    shipping_method: str = "Standard",
    region: str = "",
    country: str = "US",
    guest_email: str = "",
) -> CartTotals:
    subtotal = subtotal_for_cart(cart)
    shipping_quote = shipping_calculator.quote(subtotal, method=shipping_method)
    coupon = cart.coupon_code
    discount_total = Decimal("0.00")
    taxable_discount = Decimal("0.00")
    discount_label = ""

    if coupon:
        try:
            coupon_quote = validate_coupon(
                coupon,
                subtotal,
                user=cart.user,
                guest_email=guest_email,
                shipping_total=shipping_quote.amount,
            )
            discount_total = coupon_quote.discount_total
            discount_label = coupon.promotion.name
            if not coupon_quote.free_shipping:
                taxable_discount = discount_total
        except InvalidCoupon:
            # Pure read: report the coupon as inapplicable in the returned totals but do
            # not mutate the cart here. Persisted removal happens in refresh_cart_coupon().
            coupon = None
            discount_total = Decimal("0.00")
            taxable_discount = Decimal("0.00")

    if not coupon:
        # No coupon applied — apply the best eligible automatic (no-code) promotion.
        auto = best_auto_discount(
            subtotal,
            user=cart.user,
            guest_email=guest_email,
            shipping_total=shipping_quote.amount,
        )
        if auto:
            discount_total = auto.discount_total
            discount_label = auto.label
            taxable_discount = Decimal("0.00") if auto.free_shipping else discount_total

    taxable_amount = clamp_money(subtotal - taxable_discount)
    tax_quote = tax_calculator.quote(taxable_amount, region=region, country=country)
    total = clamp_money(subtotal + shipping_quote.amount + tax_quote.amount - discount_total)
    return CartTotals(
        subtotal=subtotal,
        discount_total=quantize_money(discount_total),
        shipping_total=shipping_quote.amount,
        tax_total=tax_quote.amount,
        total=total,
        coupon=coupon,
        shipping_method=shipping_quote.method,
        discount_label=discount_label,
    )


def add_item(cart: Cart, variant: ProductVariant, quantity: int = 1) -> CartItem:
    if quantity <= 0:
        raise CartError("Quantity must be positive.")
    with transaction.atomic():
        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
        locked_variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        if not locked_variant.active or locked_variant.product.status != locked_variant.product.Status.ACTIVE:
            raise CartError("This variant is not available.")

        # Do not create a zero-quantity row: the cart_item_quantity_positive CHECK
        # constraint rejects it on INSERT. Fetch first, then create with the final qty.
        item = CartItem.objects.select_for_update().filter(cart=locked_cart, variant=locked_variant).first()
        current_quantity = item.quantity if item else 0
        max_available = available_to_sell(locked_variant)
        if max_available <= 0:
            raise OutOfStock("This item is out of stock.")
        desired_quantity = current_quantity + quantity
        if desired_quantity > max_available:
            desired_quantity = max_available
            locked_cart.warning = f"Quantity capped at {max_available} because of current availability."
            locked_cart.save(update_fields=["warning", "updated_at"])
        if item:
            item.quantity = desired_quantity
            item.save(update_fields=["quantity", "updated_at"])
        else:
            item = CartItem.objects.create(cart=locked_cart, variant=locked_variant, quantity=desired_quantity)
        return item


def set_item_quantity(cart: Cart, variant: ProductVariant, quantity: int) -> CartItem | None:
    with transaction.atomic():
        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
        item = CartItem.objects.select_for_update().filter(cart=locked_cart, variant=variant).first()
        if not item:
            return None
        if quantity <= 0:
            item.delete()
            return None
        locked_variant = ProductVariant.objects.select_for_update().get(pk=variant.pk)
        max_available = available_to_sell(locked_variant)
        capped = min(quantity, max_available)
        if capped <= 0:
            # Out of stock: drop the line and surface a warning. Do NOT raise here or the
            # atomic block would roll the delete back, leaving the dead line in the cart.
            item.delete()
            locked_cart.warning = "This item is out of stock and was removed from your cart."
            locked_cart.save(update_fields=["warning", "updated_at"])
            return None
        if quantity > max_available:
            locked_cart.warning = f"Quantity capped at {max_available} because of current availability."
            locked_cart.save(update_fields=["warning", "updated_at"])
        item.quantity = capped
        item.save(update_fields=["quantity", "updated_at"])
        return item


def remove_item(cart: Cart, variant: ProductVariant) -> None:
    with transaction.atomic():
        Cart.objects.select_for_update().get(pk=cart.pk)
        CartItem.objects.filter(cart=cart, variant=variant).delete()


def apply_coupon(cart: Cart, code: str) -> CartTotals:
    coupon = get_coupon_by_code(code)
    with transaction.atomic():
        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
        subtotal = subtotal_for_cart(locked_cart)
        shipping_quote = shipping_calculator.quote(subtotal)
        validate_coupon(coupon, subtotal, user=locked_cart.user, shipping_total=shipping_quote.amount)
        locked_cart.coupon_code = coupon
        locked_cart.warning = ""
        locked_cart.save(update_fields=["coupon_code", "warning", "updated_at"])
        cart.coupon_code = coupon
    return recalculate_cart(cart)


def remove_coupon(cart: Cart) -> CartTotals:
    with transaction.atomic():
        locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
        locked_cart.coupon_code = None
        locked_cart.save(update_fields=["coupon_code", "updated_at"])
        cart.coupon_code = None
    return recalculate_cart(cart)


def clear_cart(cart: Cart) -> None:
    with transaction.atomic():
        locked = Cart.objects.select_for_update().get(pk=cart.pk)
        locked.items.all().delete()
        locked.status = Cart.Status.ORDERED
        locked.save(update_fields=["status", "updated_at"])


def merge_guest_cart(guest_cart: Cart, user_cart: Cart) -> Cart:
    warning_parts: list[str] = []
    with transaction.atomic():
        locked_guest = Cart.objects.select_for_update().get(pk=guest_cart.pk)
        locked_user = Cart.objects.select_for_update().get(pk=user_cart.pk)
        from .checkout import _expire_attempts_for_cart

        _expire_attempts_for_cart(
            locked_guest,
            statuses={
                CheckoutAttempt.Status.STARTED,
                CheckoutAttempt.Status.RESERVED,
                CheckoutAttempt.Status.PAYMENT_PENDING,
            },
        )
        for guest_item in locked_guest.items.select_related("variant").order_by("variant_id"):
            variant = ProductVariant.objects.select_for_update().get(pk=guest_item.variant_id)
            user_item = CartItem.objects.select_for_update().filter(cart=locked_user, variant=variant).first()
            current = user_item.quantity if user_item else 0
            requested = current + guest_item.quantity
            capped = min(requested, available_to_sell(variant))
            if capped < requested:
                warning_parts.append(f"{variant.sku} capped at {capped}.")
            if capped <= 0:
                if user_item:
                    user_item.delete()
            elif user_item:
                user_item.quantity = capped
                user_item.save(update_fields=["quantity", "updated_at"])
            else:
                CartItem.objects.create(cart=locked_user, variant=variant, quantity=capped)

        if locked_guest.coupon_code_id and not locked_user.coupon_code_id:
            try:
                subtotal = subtotal_for_cart(locked_user)
                shipping_quote = shipping_calculator.quote(subtotal)
                validate_coupon(
                    locked_guest.coupon_code,
                    subtotal,
                    user=locked_user.user,
                    shipping_total=shipping_quote.amount,
                )
                locked_user.coupon_code = locked_guest.coupon_code
            except InvalidCoupon:
                warning_parts.append("Guest coupon was not valid for your account and was not merged.")

        locked_user.warning = " ".join(warning_parts)
        locked_user.save(update_fields=["coupon_code", "warning", "updated_at"])
        locked_guest.items.all().delete()
        locked_guest.status = Cart.Status.MERGED
        locked_guest.save(update_fields=["status", "updated_at"])
    return user_cart


def cart_summary(cart: Cart) -> dict:
    totals = recalculate_cart(cart)
    return {
        "id": cart.pk,
        "token": str(cart.token),
        "item_count": cart.item_count(),
        "subtotal": str(totals.subtotal),
        "discount_total": str(totals.discount_total),
        "shipping_total": str(totals.shipping_total),
        "tax_total": str(totals.tax_total),
        "total": str(totals.total),
        "coupon": totals.coupon.normalized_code if totals.coupon else "",
        "warning": cart.warning,
    }
