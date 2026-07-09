# DRF serializers exposing catalog, cart, checkout, and order API representations
from __future__ import annotations

from rest_framework import serializers

try:
    from drf_spectacular.utils import extend_schema_field
except ImportError:  # schema generation is optional

    def extend_schema_field(_field):
        def decorator(func):
            return func

        return decorator

from .models import (
    Cart,
    CartItem,
    Category,
    CheckoutAttempt,
    Collection,
    Fulfillment,
    Order,
    OrderItem,
    Product,
    ProductImage,
    ProductVariant,
)
from .services.inventory import available_to_sell


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "slug", "description", "parent_id"]


class CollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = ["id", "name", "slug", "description", "active", "sort_order"]


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ["id", "image_url", "image", "alt_text", "sort_order"]


class ProductVariantSerializer(serializers.ModelSerializer):
    available_to_sell = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    on_sale = serializers.BooleanField(read_only=True)
    discount_percent = serializers.IntegerField(read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "sku",
            "title",
            "display_name",
            "attributes",
            "price",
            "compare_at_price",
            "on_sale",
            "discount_percent",
            "currency",
            "available_to_sell",
            "active",
        ]

    @extend_schema_field(serializers.IntegerField())
    def get_available_to_sell(self, obj):
        return available_to_sell(obj)

    @extend_schema_field(serializers.CharField())
    def get_display_name(self, obj):
        return obj.display_name()


class ProductSerializer(serializers.ModelSerializer):
    variants = ProductVariantSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    category = CategorySerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "status",
            "featured",
            "seo_title",
            "meta_description",
            "stock_visibility",
            "category",
            "variants",
            "images",
        ]


class CartItemSerializer(serializers.ModelSerializer):
    variant = ProductVariantSerializer(read_only=True)
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = ["id", "variant", "quantity", "line_total"]

    @extend_schema_field(serializers.CharField())
    def get_line_total(self, obj):
        return str(obj.line_total())


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    item_count = serializers.IntegerField(read_only=True)
    totals = serializers.SerializerMethodField()
    coupon = serializers.CharField(source="coupon_code.normalized_code", read_only=True, default="")

    class Meta:
        model = Cart
        fields = ["id", "token", "status", "coupon", "warning", "item_count", "items", "totals"]

    @extend_schema_field(serializers.DictField())
    def get_totals(self, obj):
        from .services.cart import recalculate_cart

        totals = recalculate_cart(obj)
        return {
            "subtotal": str(totals.subtotal),
            "discount_total": str(totals.discount_total),
            "shipping_total": str(totals.shipping_total),
            "tax_total": str(totals.tax_total),
            "total": str(totals.total),
            "shipping_method": totals.shipping_method,
        }


class CartAddItemSerializer(serializers.Serializer):
    variant_id = serializers.IntegerField(required=False)
    sku = serializers.CharField(required=False)
    quantity = serializers.IntegerField(min_value=1, default=1)

    def validate(self, attrs):
        if not attrs.get("variant_id") and not attrs.get("sku"):
            raise serializers.ValidationError("variant_id or sku is required.")
        return attrs


class CouponActionSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=60)


class CheckoutAttemptSerializer(serializers.ModelSerializer):
    reservations = serializers.SerializerMethodField()

    class Meta:
        model = CheckoutAttempt
        fields = [
            "id",
            "status",
            "idempotency_key",
            "guest_email",
            "subtotal",
            "discount_total",
            "shipping_total",
            "tax_total",
            "total",
            "currency",
            "gateway_reference",
            "expires_at",
            "order_id",
            "price_drift_message",
            "reservations",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_reservations(self, obj):
        return [
            {"sku": reservation.variant.sku, "quantity": reservation.quantity, "status": reservation.status}
            for reservation in obj.reservations.select_related("variant")
        ]


class BeginCheckoutSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True)
    shipping_method = serializers.ChoiceField(choices=["Standard", "Express"], default="Standard")
    address1 = serializers.CharField(required=False, allow_blank=True)
    address2 = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(required=False, allow_blank=True)
    region = serializers.CharField(required=False, allow_blank=True)
    postal_code = serializers.CharField(required=False, allow_blank=True)
    country = serializers.CharField(default="US", min_length=2, max_length=2)
    expected_subtotal = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    use_store_credit = serializers.BooleanField(default=False)


class ConfirmPaymentSerializer(serializers.Serializer):
    card_token = serializers.CharField(default="tok_visa")
    authorize_mode = serializers.CharField(default="approve")
    confirm_mode = serializers.CharField(default="approve")


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            "id",
            "sku",
            "product_name",
            "variant_title",
            "attributes",
            "quantity",
            "unit_price",
            "discount_total",
            "tax_total",
            "shipping_total",
            "line_total",
        ]


class FulfillmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fulfillment
        fields = ["status", "carrier", "tracking_number", "shipped_at", "delivered_at"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    fulfillment = FulfillmentSerializer(read_only=True)
    lifecycle = serializers.CharField(source="paid_status", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "order_token",
            "status",
            "lifecycle",
            "guest_email",
            "subtotal",
            "discount_total",
            "shipping_total",
            "tax_total",
            "total",
            "refund_total",
            "currency",
            "selected_shipping_method",
            "created_at",
            "items",
            "fulfillment",
        ]


class StaffTransitionSerializer(serializers.Serializer):
    target_status = serializers.ChoiceField(
        choices=[
            Fulfillment.Status.PROCESSING,
            Fulfillment.Status.SHIPPED,
            Fulfillment.Status.DELIVERED,
        ]
    )
    carrier = serializers.CharField(required=False, allow_blank=True)
    tracking_number = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)


class RefundSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    restock = serializers.BooleanField(default=False)
    reason = serializers.CharField(required=False, allow_blank=True)


class InventoryAdjustmentSerializer(serializers.Serializer):
    variant_id = serializers.IntegerField()
    delta = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True)
