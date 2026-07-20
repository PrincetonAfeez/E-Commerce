"""Django admin registrations and inlines for shop models with inventory and plan guards"""
from django import forms
from django.contrib import admin

from .models import (
    AccountProfile,
    Address,
    AuditLog,
    Cart,
    CartItem,
    Category,
    CheckoutAttempt,
    CheckoutLineSnapshot,
    Collection,
    CouponCode,
    CustomerGroup,
    CustomerSubscription,
    EmailDelivery,
    EmailSuppression,
    Fulfillment,
    GiftCard,
    InventoryMovement,
    Invoice,
    Order,
    OrderDiscountSnapshot,
    OrderItem,
    OrderStatusEvent,
    OutboxEvent,
    Payment,
    PaymentEvent,
    Plan,
    PriceListEntry,
    Product,
    ProductImage,
    ProductVariant,
    Promotion,
    PromotionRedemption,
    Refund,
    RefundLine,
    Reservation,
    ReturnLine,
    ReturnRequest,
    Review,
    ShippingRate,
    SimulatedGatewayIntent,
    StoreCredit,
    StoreCreditTransaction,
    StoreSettings,
    Subscription,
    TaxRate,
    Tenant,
    TenantCustomerProfile,
    TenantMembership,
    WebhookDelivery,
    WebhookEndpoint,
    WishlistItem,
)
from .services.inventory import adjust_stock
from .services.plans import can_create_product
from .tenancy import default_tenant_id


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ["sku", "title", "attributes", "price", "currency", "quantity", "active"]


class ProductAdminForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk is None:  # creating a new product
            tenant = cleaned.get("tenant")
            tenant_id = tenant.pk if tenant else default_tenant_id()
            if not can_create_product(tenant_id):
                raise forms.ValidationError(
                    "This store has reached its plan's product limit. Upgrade the plan to add more."
                )
        return cleaned


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = ["name", "status", "category", "featured", "updated_at"]
    list_filter = ["status", "featured", "category"]
    search_fields = ["name", "slug", "description", "variants__sku"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ProductImageInline, ProductVariantInline]


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ["sku", "product", "price", "quantity", "active"]
    list_filter = ["active", "product__status"]
    search_fields = ["sku", "product__name"]

    def save_model(self, request, obj, form, change):
        old_quantity = None
        if change and obj.pk:
            old_quantity = ProductVariant.objects.get(pk=obj.pk).quantity
        if old_quantity is not None and old_quantity != obj.quantity:
            # Apply the stock change through the service (row lock + ledger entry) rather
            # than mutating quantity inline (spec §15.2). Save other fields at old qty,
            # then let adjust_stock move quantity and record the InventoryMovement.
            delta = obj.quantity - old_quantity
            obj.quantity = old_quantity
            super().save_model(request, obj, form, change)
            adjust_stock(obj, delta, actor=request.user, note="Changed through Django admin")
            obj.refresh_from_db(fields=["quantity"])
        else:
            super().save_model(request, obj, form, change)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "parent"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ["name", "active", "sort_order"]
    list_filter = ["active"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ("name",)}


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "session_key", "status", "item_count", "updated_at"]
    list_filter = ["status"]
    search_fields = ["token", "session_key", "user__username", "user__email"]
    inlines = [CartItemInline]


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ["name", "type", "active", "auto_apply", "priority", "usage_limit", "used_count", "ends_at"]
    list_filter = ["type", "active", "auto_apply"]
    search_fields = ["name", "coupons__normalized_code"]


@admin.register(CouponCode)
class CouponCodeAdmin(admin.ModelAdmin):
    list_display = ["normalized_code", "promotion", "active"]
    list_filter = ["active"]
    search_fields = ["normalized_code"]


class CheckoutLineSnapshotInline(admin.TabularInline):
    model = CheckoutLineSnapshot
    extra = 0
    readonly_fields = ["variant", "sku", "product_name", "quantity", "unit_price", "line_subtotal"]


class ReservationInline(admin.TabularInline):
    model = Reservation
    extra = 0
    readonly_fields = ["variant", "quantity", "status", "expires_at", "released_at"]


@admin.register(CheckoutAttempt)
class CheckoutAttemptAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "cart", "total", "gateway_reference", "expires_at", "updated_at"]
    list_filter = ["status"]
    search_fields = ["idempotency_key", "gateway_reference", "guest_email"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [CheckoutLineSnapshotInline, ReservationInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "amount", "gateway_reference", "checkout_attempt", "order"]
    list_filter = ["status"]
    search_fields = ["gateway_reference", "idempotency_key"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ["sku", "product_name", "quantity", "unit_price", "line_total"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["order_number", "status", "user", "guest_email", "total", "refund_total", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["order_number", "guest_email", "user__username", "user__email"]
    readonly_fields = ["order_number", "order_token", "created_at", "updated_at"]
    inlines = [OrderItemInline]


@admin.register(Fulfillment)
class FulfillmentAdmin(admin.ModelAdmin):
    list_display = ["order", "status", "carrier", "tracking_number", "updated_at"]
    list_filter = ["status"]
    search_fields = ["order__order_number", "tracking_number"]


admin.site.register(InventoryMovement)
admin.site.register(Reservation)
admin.site.register(PaymentEvent)
admin.site.register(PromotionRedemption)
admin.site.register(OrderDiscountSnapshot)
admin.site.register(OrderStatusEvent)
admin.site.register(Refund)
admin.site.register(RefundLine)
admin.site.register(OutboxEvent)
admin.site.register(EmailDelivery)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Append-only: the audit trail must not be editable or deletable in-app."""

    list_display = ["created_at", "actor", "action", "object_type", "object_id"]
    list_filter = ["action", "object_type"]
    search_fields = ["action", "object_type", "object_id", "request_id"]
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(SimulatedGatewayIntent)
admin.site.register(AccountProfile)
admin.site.register(Address)
admin.site.register(WishlistItem)
admin.site.register(ReturnRequest)
admin.site.register(ReturnLine)
admin.site.register(WebhookEndpoint)
admin.site.register(WebhookDelivery)
admin.site.register(TaxRate)
admin.site.register(ShippingRate)
admin.site.register(GiftCard)
admin.site.register(StoreCredit)
admin.site.register(StoreCreditTransaction)
admin.site.register(StoreSettings)
admin.site.register(Plan)
admin.site.register(Subscription)
admin.site.register(CustomerGroup)
admin.site.register(PriceListEntry)
admin.site.register(CustomerSubscription)
admin.site.register(TenantMembership)
admin.site.register(TenantCustomerProfile)
admin.site.register(Invoice)
admin.site.register(EmailSuppression)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "primary_domain", "active", "created_at"]
    list_filter = ["active"]
    search_fields = ["name", "slug", "primary_domain"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ["product", "rating", "author_name", "verified_purchase", "approved", "created_at"]
    list_filter = ["approved", "verified_purchase", "rating"]
    search_fields = ["product__name", "author_name", "title", "body"]
    list_editable = ["approved"]
    actions = ["approve_reviews", "unapprove_reviews"]

    @admin.action(description="Approve selected reviews")
    def approve_reviews(self, request, queryset):
        queryset.update(approved=True)

    @admin.action(description="Unapprove selected reviews")
    def unapprove_reviews(self, request, queryset):
        queryset.update(approved=False)
