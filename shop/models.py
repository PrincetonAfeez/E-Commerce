"""Django ORM models for multi-tenant catalog, checkout, orders, billing, and webhooks"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q, Sum
from django.db.models.signals import m2m_changed
from django.utils import timezone
from django.utils.text import slugify

from .tenancy import TenantManager, default_tenant_id, get_current_tenant_id
from .validators import validate_image_upload


def _validate_tenant_match(*, expected_tenant_id: int | None, actual_tenant_id: int | None, label: str) -> None:
    if expected_tenant_id is None and actual_tenant_id is None:
        return
    if expected_tenant_id is None:
        raise ValidationError(f"{label} tenant does not match.")
    if actual_tenant_id is not None and expected_tenant_id != actual_tenant_id:
        raise ValidationError(f"{label} tenant does not match.")


def _related_tenant_id(instance, field_name: str, model_class: type) -> int | None:
    fk_id = getattr(instance, f"{field_name}_id", None)
    if not fk_id:
        return None
    related = getattr(instance, field_name, None)
    if related is not None:
        return getattr(related, "tenant_id", None)
    return model_class._base_manager.filter(pk=fk_id).values_list("tenant_id", flat=True).first()


MONEY_MAX_DIGITS = 12
MONEY_DECIMAL_PLACES = 2


def money_zero() -> Decimal:
    return Decimal("0.00")


def generate_order_number() -> str:
    return f"EC-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def normalize_coupon_code(code: str | None) -> str:
    return (code or "").strip().upper()


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Tenant(TimeStampedModel):
    """A merchant store in the multi-tenant platform (shared-schema isolation)."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    primary_domain = models.CharField(
        max_length=253, blank=True, db_index=True, help_text="Host that routes to this store."
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["primary_domain"],
                condition=Q(primary_domain__gt=""),
                name="unique_tenant_primary_domain_nonempty",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        if self.slug:
            base = self.slug
            suffix = 1
            while Tenant.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                self.slug = f"{base}-{suffix}"
                suffix += 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class TenantScopedModel(TimeStampedModel):
    """Abstract base that tags a row with its owning tenant and scopes the default
    manager to the active tenant (see shop.tenancy). ``tenant`` is auto-populated on
    save from the current context, falling back to the default tenant."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="%(class)ss")

    objects = TenantManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            from django.conf import settings as dj_settings

            tid = get_current_tenant_id()
            if tid is None:
                if getattr(dj_settings, "IS_PRODUCTION", False) and not getattr(dj_settings, "RUNNING_TESTS", False):
                    from django.core.exceptions import ValidationError

                    raise ValidationError("Tenant context is required to save tenant-scoped records.")
                self.tenant_id = default_tenant_id()
            else:
                self.tenant_id = tid
        super().save(*args, **kwargs)


class Category(TenantScopedModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "slug"], name="unique_category_slug_per_tenant"),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        if self.tenant_id is None and self.parent_id:
            self.tenant_id = _related_tenant_id(self, "parent", Category)
        if self.parent_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "parent", Category),
                label="Parent category",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Collection(TenantScopedModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "slug"], name="unique_collection_slug_per_tenant"),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Product(TenantScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    class StockVisibility(models.TextChoices):
        EXACT = "exact", "Show exact quantity"
        LOW_STOCK = "low_stock", "Show low stock"
        AVAILABILITY = "availability", "Show availability"
        HIDE_OUT_OF_STOCK = "hide_oos", "Hide out-of-stock variants"
        DISABLE_OUT_OF_STOCK = "disable_oos", "Disable out-of-stock variants"

    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
    collections = models.ManyToManyField(Collection, blank=True, related_name="products")
    related_products = models.ManyToManyField("self", blank=True, symmetrical=False)
    name = models.CharField(max_length=180)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.DRAFT)
    featured = models.BooleanField(default=False)
    seo_title = models.CharField(max_length=180, blank=True)
    meta_description = models.CharField(max_length=320, blank=True)
    stock_visibility = models.CharField(max_length=32, choices=StockVisibility, default=StockVisibility.AVAILABILITY)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["tenant", "status", "featured"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "slug"], name="unique_product_slug_per_tenant"),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        if self.category_id and self.tenant_id:
            category_tenant_id = (
                self.category.tenant_id
                if "category" in self.__dict__ and self.category is not None
                else Category.objects.filter(pk=self.category_id).values_list("tenant_id", flat=True).first()
            )
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=category_tenant_id,
                label="Category",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


def _validate_product_m2m_tenant(*, product: Product, model_class: type, pk_set: set, label: str) -> None:
    if not pk_set or not product.tenant_id:
        return
    if model_class._base_manager.filter(pk__in=pk_set).exclude(tenant_id=product.tenant_id).exists():
        raise ValidationError(f"{label} tenant does not match.")


def _product_collections_changed(sender, instance, action, pk_set, **kwargs):
    if action in ("pre_add", "pre_set") and pk_set:
        _validate_product_m2m_tenant(product=instance, model_class=Collection, pk_set=pk_set, label="Collection")


def _product_related_changed(sender, instance, action, pk_set, **kwargs):
    if action in ("pre_add", "pre_set") and pk_set:
        _validate_product_m2m_tenant(product=instance, model_class=Product, pk_set=pk_set, label="Related product")


class ProductImage(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    image_url = models.URLField(blank=True)
    image = models.ImageField(upload_to="product-images/", blank=True, validators=[validate_image_upload])
    alt_text = models.CharField(max_length=180, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [models.UniqueConstraint(fields=["product", "sort_order"], name="unique_product_image_order")]

    def save(self, *args, **kwargs):
        if self.product_id:
            product_tenant_id = (
                self.product.tenant_id
                if "product" in self.__dict__ and self.product is not None
                else Product.objects.filter(pk=self.product_id).values_list("tenant_id", flat=True).first()
            )
            if product_tenant_id is not None:
                from shop.tenancy import get_current_tenant_id

                expected = get_current_tenant_id() or product_tenant_id
                _validate_tenant_match(
                    expected_tenant_id=expected,
                    actual_tenant_id=product_tenant_id,
                    label="Product",
                )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.alt_text or f"Image for {self.product}"


class ProductVariant(TenantScopedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField(max_length=64)
    title = models.CharField(max_length=120, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    compare_at_price = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        null=True,
        blank=True,
        help_text="Original/list price for strikethrough. Display only — never charged.",
    )
    currency = models.CharField(max_length=3, default="USD")
    quantity = models.PositiveIntegerField(default=0)
    reorder_point = models.PositiveIntegerField(default=0)
    subscription_interval = models.CharField(
        max_length=12,
        blank=True,
        default="",
        choices=[
            ("weekly", "Weekly"),
            ("monthly", "Monthly"),
            ("quarterly", "Quarterly"),
            ("annual", "Annual"),
        ],
        help_text="If set, purchasing this variant starts a recurring subscription.",
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product__name", "sku"]
        indexes = [
            models.Index(fields=["sku"]),
            models.Index(fields=["product", "active"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=0), name="variant_quantity_nonnegative"),
            models.CheckConstraint(condition=Q(price__gte=0), name="variant_price_nonnegative"),
            models.CheckConstraint(
                condition=Q(compare_at_price__isnull=True) | Q(compare_at_price__gte=F("price")),
                name="variant_compare_at_gte_price",
            ),
            models.UniqueConstraint(fields=["tenant", "sku"], name="unique_variant_sku_per_tenant"),
        ]

    @property
    def is_subscription(self) -> bool:
        return bool(self.subscription_interval)

    @property
    def on_sale(self) -> bool:
        return self.compare_at_price is not None and self.compare_at_price > self.price

    @property
    def discount_percent(self) -> int:
        if not self.on_sale or not self.compare_at_price:
            return 0
        return int(round((self.compare_at_price - self.price) / self.compare_at_price * 100))

    def available_to_sell(self) -> int:
        # Use a pre-annotated ``active_reserved`` (see inventory.variants_with_availability)
        # when present to avoid a per-variant aggregate query (N+1) in list/detail rendering.
        active_reserved = getattr(self, "active_reserved", None)
        if active_reserved is None:
            active_reserved = (
                self.reservations.filter(status=Reservation.Status.ACTIVE).aggregate(total=Sum("quantity"))["total"]
                or 0
            )
        return max(self.quantity - active_reserved, 0)

    def display_name(self) -> str:
        return self.title or ", ".join(f"{key}: {value}" for key, value in self.attributes.items()) or self.sku

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.product_id:
            self.tenant_id = _related_tenant_id(self, "product", Product)
        if self.product_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "product", Product),
                label="Product",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.product.name} / {self.sku}"


class Cart(TenantScopedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MERGED = "merged", "Merged"
        ORDERED = "ordered", "Ordered"
        ABANDONED = "abandoned", "Abandoned"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="carts"
    )
    session_key = models.CharField(max_length=80, blank=True, db_index=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    coupon_code = models.ForeignKey("CouponCode", null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE)
    warning = models.TextField(blank=True)
    recovery_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "session_key"]),
            models.Index(fields=["status", "user"]),
            models.Index(fields=["status", "updated_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user"],
                condition=Q(user__isnull=False, status="active"),
                name="unique_active_cart_per_user",
            ),
            models.UniqueConstraint(
                fields=["tenant", "session_key"],
                condition=Q(session_key__gt="", status="active"),
                name="unique_active_cart_per_session",
            ),
        ]

    def item_count(self) -> int:
        return self.items.aggregate(total=Sum("quantity"))["total"] or 0

    def save(self, *args, **kwargs):
        if self.coupon_code_id and self.tenant_id:
            coupon_tenant_id = (
                self.coupon_code.tenant_id
                if "coupon_code" in self.__dict__ and self.coupon_code is not None
                else CouponCode.objects.filter(pk=self.coupon_code_id).values_list("tenant_id", flat=True).first()
            )
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=coupon_tenant_id,
                label="Coupon code",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        owner = self.user_id or self.session_key or self.token
        return f"Cart {owner}"


class CartItem(TimeStampedModel):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="cart_items")
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["cart", "variant"], name="unique_variant_per_cart"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="cart_item_quantity_positive"),
        ]

    def unit_price(self) -> Decimal:
        from .services.pricing import effective_price

        return effective_price(self.variant, self.cart.user if self.cart_id else None)

    def line_total(self) -> Decimal:
        return self.unit_price() * self.quantity

    def save(self, *args, **kwargs):
        if self.cart_id and self.variant_id:
            cart_tenant_id = (
                self.cart.tenant_id
                if "cart" in self.__dict__ and self.cart is not None
                else Cart.objects.filter(pk=self.cart_id).values_list("tenant_id", flat=True).first()
            )
            variant_tenant_id = (
                self.variant.tenant_id
                if "variant" in self.__dict__ and self.variant is not None
                else ProductVariant.objects.filter(pk=self.variant_id).values_list("tenant_id", flat=True).first()
            )
            _validate_tenant_match(
                expected_tenant_id=cart_tenant_id,
                actual_tenant_id=variant_tenant_id,
                label="CartItem variant",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.quantity} x {self.variant.sku}"


class Promotion(TenantScopedModel):
    class Type(models.TextChoices):
        PERCENTAGE = "percentage", "Percentage"
        FIXED_AMOUNT = "fixed_amount", "Fixed amount"
        FREE_SHIPPING = "free_shipping", "Free shipping"

    name = models.CharField(max_length=140)
    type = models.CharField(max_length=20, choices=Type)
    active = models.BooleanField(default=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    min_subtotal = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    per_customer_usage_limit = models.PositiveIntegerField(null=True, blank=True)
    release_redemption_on_refund = models.BooleanField(default=False)
    auto_apply = models.BooleanField(default=False, help_text="Apply automatically at cart (no code) when eligible.")
    priority = models.IntegerField(default=0, help_text="Higher wins when several auto promos qualify.")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(usage_limit__isnull=True) | Q(used_count__lte=F("usage_limit")),
                name="promotion_used_not_over_limit",
            ),
            models.CheckConstraint(condition=Q(used_count__gte=0), name="promotion_used_nonnegative"),
            models.CheckConstraint(condition=Q(discount_percent__gte=0), name="promotion_percent_nonnegative"),
            models.CheckConstraint(condition=Q(discount_percent__lte=100), name="promotion_percent_max_100"),
            models.CheckConstraint(condition=Q(discount_amount__gte=0), name="promotion_amount_nonnegative"),
            models.CheckConstraint(condition=Q(min_subtotal__gte=0), name="promotion_min_subtotal_nonnegative"),
        ]

    def __str__(self) -> str:
        return self.name


class CouponCode(TenantScopedModel):
    promotion = models.ForeignKey(Promotion, on_delete=models.CASCADE, related_name="coupons")
    code = models.CharField(max_length=60)
    normalized_code = models.CharField(max_length=60, editable=False, db_index=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["normalized_code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "normalized_code"], name="unique_coupon_code_per_tenant"),
        ]

    def save(self, *args, **kwargs):
        self.normalized_code = normalize_coupon_code(self.code)
        self.code = self.normalized_code
        if self.tenant_id is None and self.promotion_id:
            self.tenant_id = _related_tenant_id(self, "promotion", Promotion)
        if self.promotion_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "promotion", Promotion),
                label="Promotion",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.normalized_code


class CheckoutAttempt(TenantScopedModel):
    class Status(models.TextChoices):
        STARTED = "started", "Started"
        RESERVED = "reserved", "Reserved"
        PAYMENT_PENDING = "payment_pending", "Payment pending"
        PAYMENT_CONFIRMED = "payment_confirmed", "Payment confirmed"
        FINALIZED = "finalized", "Finalized"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    cart = models.ForeignKey(Cart, on_delete=models.PROTECT, related_name="checkout_attempts")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="checkout_attempts",
    )
    session_key = models.CharField(max_length=80, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=120)
    status = models.CharField(max_length=32, choices=Status, default=Status.STARTED, db_index=True)
    guest_email = models.EmailField(blank=True)
    shipping_name = models.CharField(max_length=180, blank=True)
    shipping_address1 = models.CharField(max_length=180, blank=True)
    shipping_address2 = models.CharField(max_length=180, blank=True)
    shipping_city = models.CharField(max_length=120, blank=True)
    shipping_region = models.CharField(max_length=120, blank=True)
    shipping_postal_code = models.CharField(max_length=32, blank=True)
    shipping_country = models.CharField(max_length=2, default="US")
    selected_shipping_method = models.CharField(max_length=80, default="Standard")
    subtotal = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero)
    discount_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    shipping_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    tax_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    total = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero)
    credit_applied = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    currency = models.CharField(max_length=3, default="USD")
    coupon_code = models.ForeignKey(CouponCode, null=True, blank=True, on_delete=models.SET_NULL)
    price_drift_message = models.TextField(blank=True)
    gateway_reference = models.CharField(max_length=120, blank=True, db_index=True)
    payment_started_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)

    @property
    def amount_due(self) -> Decimal:
        # Amount to charge the gateway after applying held store credit.
        return max(self.total - self.credit_applied, Decimal("0.00"))

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["cart", "idempotency_key"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["cart", "idempotency_key"], name="unique_checkout_attempt_key"),
            models.CheckConstraint(condition=Q(subtotal__gte=0), name="attempt_subtotal_nonnegative"),
            models.CheckConstraint(condition=Q(discount_total__gte=0), name="attempt_discount_nonnegative"),
            models.CheckConstraint(condition=Q(shipping_total__gte=0), name="attempt_shipping_nonnegative"),
            models.CheckConstraint(condition=Q(tax_total__gte=0), name="attempt_tax_nonnegative"),
            models.CheckConstraint(condition=Q(total__gte=0), name="attempt_total_nonnegative"),
            models.CheckConstraint(condition=Q(credit_applied__gte=0), name="attempt_credit_nonnegative"),
            models.CheckConstraint(condition=Q(credit_applied__lte=F("total")), name="attempt_credit_lte_total"),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.cart_id:
            self.tenant_id = _related_tenant_id(self, "cart", Cart)
        if self.cart_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "cart", Cart),
                label="Cart",
            )
        if self.coupon_code_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "coupon_code", CouponCode),
                label="Coupon code",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"CheckoutAttempt {self.pk} ({self.status})"


class CheckoutLineSnapshot(TimeStampedModel):
    attempt = models.ForeignKey(CheckoutAttempt, on_delete=models.CASCADE, related_name="line_snapshots")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    sku = models.CharField(max_length=64)
    product_name = models.CharField(max_length=180)
    variant_title = models.CharField(max_length=120, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    line_subtotal = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["attempt", "variant"], name="unique_attempt_variant_snapshot"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="attempt_line_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="attempt_line_price_nonnegative"),
            models.CheckConstraint(condition=Q(line_subtotal__gte=0), name="attempt_line_total_nonnegative"),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} x {self.sku}"

    def save(self, *args, **kwargs):
        tenant_ids: list[tuple[str, int | None]] = []
        if self.variant_id:
            tenant_ids.append(("Product variant", _related_tenant_id(self, "variant", ProductVariant)))
        if self.attempt_id:
            tenant_ids.append(("Checkout attempt", _related_tenant_id(self, "attempt", CheckoutAttempt)))
        expected: int | None = None
        for label, actual in tenant_ids:
            if actual is None:
                continue
            if expected is None:
                expected = actual
            else:
                _validate_tenant_match(
                    expected_tenant_id=expected,
                    actual_tenant_id=actual,
                    label=label,
                )
        super().save(*args, **kwargs)


class Reservation(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="reservations")
    cart = models.ForeignKey(Cart, on_delete=models.PROTECT, related_name="reservations")
    checkout_attempt = models.ForeignKey(CheckoutAttempt, on_delete=models.CASCADE, related_name="reservations")
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["expires_at", "id"]
        indexes = [
            models.Index(fields=["variant", "status", "expires_at"]),
            models.Index(fields=["checkout_attempt", "status"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="reservation_quantity_positive"),
        ]

    def save(self, *args, **kwargs):
        tenant_ids: list[tuple[str, int | None]] = []
        if self.variant_id:
            tenant_ids.append(("Product variant", _related_tenant_id(self, "variant", ProductVariant)))
        if self.cart_id:
            tenant_ids.append(("Cart", _related_tenant_id(self, "cart", Cart)))
        if self.checkout_attempt_id:
            tenant_ids.append(("Checkout attempt", _related_tenant_id(self, "checkout_attempt", CheckoutAttempt)))
        expected: int | None = None
        for label, actual in tenant_ids:
            if actual is None:
                continue
            if expected is None:
                expected = actual
            else:
                _validate_tenant_match(
                    expected_tenant_id=expected,
                    actual_tenant_id=actual,
                    label=label,
                )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.quantity} x {self.variant.sku} ({self.status})"


class InventoryMovement(TimeStampedModel):
    class Reason(models.TextChoices):
        SEED = "seed", "Seed"
        MANUAL_ADJUSTMENT = "manual_adjustment", "Manual adjustment"
        RESERVATION_CONSUMED = "reservation_consumed", "Reservation consumed"
        RETURN = "return", "Return"
        CORRECTION = "correction", "Correction"

    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="inventory_movements")
    quantity_delta = models.IntegerField()
    reason = models.CharField(max_length=40, choices=Reason)
    reservation = models.ForeignKey(
        Reservation, null=True, blank=True, on_delete=models.SET_NULL, related_name="inventory_movements"
    )
    order = models.ForeignKey(
        "Order", null=True, blank=True, on_delete=models.SET_NULL, related_name="inventory_movements"
    )
    staff_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["variant", "created_at"])]
        permissions = [("adjust_inventory", "Can adjust variant stock levels")]

    def __str__(self) -> str:
        return f"{self.variant.sku} {self.quantity_delta:+d} ({self.reason})"


class Payment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AUTHORIZED = "authorized", "Authorized"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
        REQUIRES_REFUND = "requires_refund", "Requires refund"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="payments")
    objects = TenantManager()
    checkout_attempt = models.ForeignKey(CheckoutAttempt, on_delete=models.PROTECT, related_name="payments")
    order = models.ForeignKey("Order", null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=32, choices=Status, default=Status.PENDING, db_index=True)
    gateway_reference = models.CharField(max_length=120, null=True, blank=True, unique=True)
    idempotency_key = models.CharField(max_length=120)
    safe_display = models.CharField(max_length=120, blank=True)
    raw_status = models.CharField(max_length=80, blank=True)
    failure_code = models.CharField(max_length=80, blank=True)
    provider = models.CharField(max_length=32, default="simulated", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["checkout_attempt", "status"]),
            models.Index(fields=["gateway_reference"]),
            models.Index(fields=["tenant", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["checkout_attempt", "idempotency_key"],
                name="unique_payment_idempotency_per_attempt",
            ),
            models.UniqueConstraint(
                fields=["checkout_attempt"],
                condition=Q(status="pending"),
                name="unique_pending_payment_per_attempt",
            ),
            models.CheckConstraint(condition=Q(amount__gte=0), name="payment_amount_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.checkout_attempt_id:
            self.tenant_id = (
                self.checkout_attempt.tenant_id
                if "checkout_attempt" in self.__dict__ and self.checkout_attempt is not None
                else CheckoutAttempt.objects.filter(pk=self.checkout_attempt_id)
                .values_list("tenant_id", flat=True)
                .first()
            )
        if self.checkout_attempt_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "checkout_attempt", CheckoutAttempt),
                label="Checkout attempt",
            )
        if self.order_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "order", Order),
                label="Order",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Payment {self.gateway_reference or self.pk} ({self.status})"


class PaymentEvent(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="payment_events")
    objects = TenantManager()
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    checkout_attempt = models.ForeignKey(
        CheckoutAttempt, null=True, blank=True, on_delete=models.SET_NULL, related_name="payment_events"
    )
    gateway_reference = models.CharField(max_length=120, blank=True, db_index=True)
    event_type = models.CharField(max_length=80)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, blank=True)
    processing_result = models.TextField(blank=True)
    provider = models.CharField(max_length=32, default="simulated", db_index=True)
    provider_event_id = models.CharField(max_length=160, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["gateway_reference", "event_type"]),
            models.Index(fields=["tenant", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider", "provider_event_id"],
                condition=~Q(provider_event_id=""),
                name="unique_payment_event_per_provider",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            if self.payment_id:
                self.tenant_id = Payment.objects.filter(pk=self.payment_id).values_list("tenant_id", flat=True).first()
            elif self.checkout_attempt_id:
                self.tenant_id = (
                    CheckoutAttempt.objects.filter(pk=self.checkout_attempt_id)
                    .values_list("tenant_id", flat=True)
                    .first()
                )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.event_type} {self.gateway_reference}"


class Order(TenantScopedModel):
    class Status(models.TextChoices):
        PLACED = "placed", "Placed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders"
    )
    checkout_attempt = models.OneToOneField(CheckoutAttempt, on_delete=models.PROTECT, related_name="order_record")
    order_number = models.CharField(max_length=40, unique=True, default=generate_order_number)
    order_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    guest_email = models.EmailField(blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.PLACED, db_index=True)
    subtotal = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero)
    discount_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    shipping_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    tax_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    total = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero)
    refund_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    credit_applied = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    currency = models.CharField(max_length=3, default="USD")
    coupon_code = models.ForeignKey(CouponCode, null=True, blank=True, on_delete=models.SET_NULL)
    shipping_name = models.CharField(max_length=180, blank=True)
    shipping_address1 = models.CharField(max_length=180, blank=True)
    shipping_address2 = models.CharField(max_length=180, blank=True)
    shipping_city = models.CharField(max_length=120, blank=True)
    shipping_region = models.CharField(max_length=120, blank=True)
    shipping_postal_code = models.CharField(max_length=32, blank=True)
    shipping_country = models.CharField(max_length=2, default="US")
    selected_shipping_method = models.CharField(max_length=80, default="Standard")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["order_number"]),
            models.Index(fields=["order_token"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(subtotal__gte=0), name="order_subtotal_nonnegative"),
            models.CheckConstraint(condition=Q(discount_total__gte=0), name="order_discount_nonnegative"),
            models.CheckConstraint(condition=Q(shipping_total__gte=0), name="order_shipping_nonnegative"),
            models.CheckConstraint(condition=Q(tax_total__gte=0), name="order_tax_nonnegative"),
            models.CheckConstraint(condition=Q(total__gte=0), name="order_total_nonnegative"),
            models.CheckConstraint(condition=Q(refund_total__gte=0), name="order_refund_nonnegative"),
            models.CheckConstraint(condition=Q(credit_applied__gte=0), name="order_credit_nonnegative"),
            models.CheckConstraint(condition=Q(credit_applied__lte=F("total")), name="order_credit_lte_total"),
            models.CheckConstraint(condition=Q(refund_total__lte=F("total")), name="order_refund_lte_total"),
        ]

    @property
    def refundable(self) -> bool:
        # Uses the prefetched payments when available (staff order detail prefetches them).
        if self.status == self.Status.CANCELLED:
            return False
        if self.total - self.refund_total <= 0:
            return False
        return any(
            payment.status in {Payment.Status.CONFIRMED, Payment.Status.PARTIALLY_REFUNDED}
            for payment in self.payments.all()
        )

    def paid_status(self) -> str:
        payment = self.payments.order_by("-created_at").first()
        fulfillment = getattr(self, "fulfillment", None)
        if self.status == self.Status.CANCELLED:
            return "cancelled"
        if self.status == self.Status.REFUNDED:
            return "refunded"
        if fulfillment and fulfillment.status in {Fulfillment.Status.SHIPPED, Fulfillment.Status.DELIVERED}:
            return fulfillment.status
        if fulfillment and fulfillment.status == Fulfillment.Status.PROCESSING:
            return "processing"
        if payment and payment.status in {Payment.Status.CONFIRMED, Payment.Status.PARTIALLY_REFUNDED}:
            return "paid"
        return "pending"

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.checkout_attempt_id:
            self.tenant_id = _related_tenant_id(self, "checkout_attempt", CheckoutAttempt)
        if self.checkout_attempt_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "checkout_attempt", CheckoutAttempt),
                label="Checkout attempt",
            )
        if self.coupon_code_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "coupon_code", CouponCode),
                label="Coupon code",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.order_number


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, null=True, blank=True, on_delete=models.SET_NULL)
    sku = models.CharField(max_length=64)
    product_name = models.CharField(max_length=180)
    variant_title = models.CharField(max_length=120, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    discount_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    tax_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    shipping_total = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    line_total = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="order_item_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0), name="order_item_unit_price_nonnegative"),
            models.CheckConstraint(condition=Q(line_total__gte=0), name="order_item_total_nonnegative"),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} x {self.sku}"

    def save(self, *args, **kwargs):
        if self.variant_id:
            _validate_tenant_match(
                expected_tenant_id=_related_tenant_id(self, "order", Order),
                actual_tenant_id=_related_tenant_id(self, "variant", ProductVariant),
                label="Product variant",
            )
        super().save(*args, **kwargs)


class Fulfillment(TimeStampedModel):
    class Status(models.TextChoices):
        UNFULFILLED = "unfulfilled", "Unfulfilled"
        PROCESSING = "processing", "Processing"
        SHIPPED = "shipped", "Shipped"
        DELIVERED = "delivered", "Delivered"

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="fulfillment")
    status = models.CharField(max_length=32, choices=Status, default=Status.UNFULFILLED)
    carrier = models.CharField(max_length=80, blank=True)
    tracking_number = models.CharField(max_length=120, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        permissions = [
            ("fulfill_orders", "Can drive order fulfillment"),
            ("cancel_orders", "Can cancel orders"),
        ]

    def __str__(self) -> str:
        return f"{self.order.order_number}: {self.status}"


class OrderStatusEvent(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_events")
    event_type = models.CharField(max_length=80)
    from_status = models.CharField(max_length=40, blank=True)
    to_status = models.CharField(max_length=40, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class PromotionRedemption(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="promotion_redemptions")
    objects = TenantManager()
    promotion = models.ForeignKey(Promotion, on_delete=models.PROTECT, related_name="redemptions")
    coupon_code = models.ForeignKey(CouponCode, null=True, blank=True, on_delete=models.SET_NULL)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="promotion_redemptions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="redemptions"
    )
    guest_email = models.EmailField(blank=True)
    discount_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    released = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["promotion", "user", "released"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "order", "promotion"], name="unique_promotion_redemption_per_tenant"
            ),
            models.UniqueConstraint(fields=["coupon_code", "order"], name="unique_coupon_redemption_order"),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.order_id:
            self.tenant_id = Order.objects.filter(pk=self.order_id).values_list("tenant_id", flat=True).first()
        if self.promotion_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "promotion", Promotion),
                label="Promotion",
            )
        if self.order_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "order", Order),
                label="Order",
            )
        super().save(*args, **kwargs)


class OrderDiscountSnapshot(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="discount_snapshots")
    promotion = models.ForeignKey(Promotion, null=True, blank=True, on_delete=models.SET_NULL)
    coupon_code = models.CharField(max_length=60, blank=True)
    label = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="order_discount_amount_nonnegative"),
        ]


class Refund(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    restock = models.BooleanField(default=False)
    reason = models.TextField(blank=True)
    allocation_payload = models.JSONField(default=dict, blank=True)
    gateway_reference = models.CharField(max_length=120, blank=True, db_index=True)

    class Meta:
        permissions = [("process_refunds", "Can create refunds")]
        constraints = [
            models.UniqueConstraint(fields=["payment", "idempotency_key"], name="unique_refund_idempotency"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive"),
        ]

    def save(self, *args, **kwargs):
        if self.payment_id and self.order_id:
            payment_order_id = (
                self.payment.order_id
                if "payment" in self.__dict__ and self.payment is not None
                else Payment.objects.filter(pk=self.payment_id).values_list("order_id", flat=True).first()
            )
            if payment_order_id is not None and payment_order_id != self.order_id:
                raise ValidationError("Payment does not belong to this order.")
        super().save(*args, **kwargs)


class RefundLine(TimeStampedModel):
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name="lines")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="refund_lines")
    quantity = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    restocked = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="refund_line_quantity_positive"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_line_amount_positive"),
        ]


class OutboxEvent(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    event_type = models.CharField(max_length=120)
    aggregate_type = models.CharField(max_length=80)
    aggregate_id = models.CharField(max_length=80)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["available_at", "id"]
        indexes = [models.Index(fields=["status", "available_at"])]


class EmailDelivery(TimeStampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="email_deliveries")
    objects = TenantManager()
    outbox_event = models.ForeignKey(OutboxEvent, null=True, blank=True, on_delete=models.SET_NULL)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL)
    to_email_hash = models.CharField(max_length=128, blank=True)
    template = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status, default=Status.QUEUED)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            if self.order_id:
                self.tenant_id = Order.objects.filter(pk=self.order_id).values_list("tenant_id", flat=True).first()
            elif self.outbox_event_id:
                self.tenant_id = (
                    OutboxEvent.objects.filter(pk=self.outbox_event_id).values_list("tenant_id", flat=True).first()
                )
            if self.tenant_id is None:
                tid = get_current_tenant_id()
                if tid is not None:
                    self.tenant_id = tid
        super().save(*args, **kwargs)


class EmailSuppression(TenantScopedModel):
    """Emails that have unsubscribed from marketing (e.g. cart-recovery)."""

    email = models.EmailField()
    reason = models.CharField(max_length=80, default="unsubscribe")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "email"], name="unique_suppression_per_tenant"),
        ]

    def __str__(self) -> str:
        return self.email


class AuditLog(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="audit_logs")
    objects = TenantManager()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=120)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=80)
    metadata = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["object_type", "object_id", "created_at"])]

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            tid = get_current_tenant_id()
            if tid is not None:
                self.tenant_id = tid
        super().save(*args, **kwargs)


class CustomerGroup(TenantScopedModel):
    """A B2B / segment pricing tier (e.g. Wholesale) — see PriceListEntry + percent_off."""

    name = models.CharField(max_length=120)
    percent_off = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Blanket % off base price for members (overridden by price-list entries).",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="unique_customer_group_name_per_tenant"),
            models.CheckConstraint(condition=Q(percent_off__gte=0), name="customer_group_percent_nonnegative"),
            models.CheckConstraint(condition=Q(percent_off__lte=100), name="customer_group_percent_max_100"),
        ]

    def __str__(self) -> str:
        return self.name


class PriceListEntry(TenantScopedModel):
    """A fixed per-variant price for a customer group (overrides percent_off + base)."""

    group = models.ForeignKey(CustomerGroup, on_delete=models.CASCADE, related_name="price_entries")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name="price_entries")
    price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "variant"], name="unique_price_entry_group_variant"),
            models.CheckConstraint(condition=Q(price__gte=0), name="price_list_entry_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.group_id:
            self.tenant_id = _related_tenant_id(self, "group", CustomerGroup)
        if self.group_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "group", CustomerGroup),
                label="Customer group",
            )
        if self.variant_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "variant", ProductVariant),
                label="Product variant",
            )
        super().save(*args, **kwargs)


class CustomerSubscription(TenantScopedModel):
    """A customer's recurring purchase of a subscription variant."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        PAST_DUE = "past_due", "Past due"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="customer_subscriptions")
    quantity = models.PositiveIntegerField(default=1)
    interval = models.CharField(max_length=12)
    status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE, db_index=True)
    unit_price = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    next_renewal_at = models.DateTimeField(db_index=True)
    last_order = models.ForeignKey("Order", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "variant"],
                condition=Q(status="active"),
                name="unique_active_subscription_per_user_variant",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.variant_id:
            self.tenant_id = _related_tenant_id(self, "variant", ProductVariant)
        if self.variant_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "variant", ProductVariant),
                label="Product variant",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Sub<{self.user_id}> {self.variant.sku} ({self.status})"


class AccountProfile(TimeStampedModel):
    """Per-account metadata not on the default User model (email verification, §9)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Profile<{self.user_id}> verified={self.email_verified}"


class TenantCustomerProfile(TenantScopedModel):
    """Per-tenant customer segment (B2B group) for a user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenant_customer_profiles"
    )
    customer_group = models.ForeignKey(
        CustomerGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="tenant_members"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="unique_customer_profile_per_tenant"),
        ]

    def __str__(self) -> str:
        return f"TenantCustomer<{self.user_id}@{self.tenant_id}>"

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.customer_group_id:
            self.tenant_id = _related_tenant_id(self, "customer_group", CustomerGroup)
        if self.customer_group_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "customer_group", CustomerGroup),
                label="Customer group",
            )
        super().save(*args, **kwargs)


class Address(TenantScopedModel):
    """A customer's saved shipping address (reusable across checkouts)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses")
    label = models.CharField(max_length=60, blank=True)
    name = models.CharField(max_length=180)
    address1 = models.CharField(max_length=180)
    address2 = models.CharField(max_length=180, blank=True)
    city = models.CharField(max_length=120)
    region = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=32)
    country = models.CharField(max_length=2, default="US")
    phone = models.CharField(max_length=40, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_default", "-updated_at"]
        indexes = [models.Index(fields=["user", "is_default"])]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user"],
                condition=Q(is_default=True),
                name="unique_default_address_per_user",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} — {self.city}"


class WishlistItem(TenantScopedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wishlist_items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name="wishlisted_by")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "variant"], name="unique_wishlist_user_variant"),
        ]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.variant_id:
            self.tenant_id = _related_tenant_id(self, "variant", ProductVariant)
        if self.variant_id:
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=_related_tenant_id(self, "variant", ProductVariant),
                label="Product variant",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Wishlist<{self.user_id}> {self.variant.sku}"


class Review(TenantScopedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviews"
    )
    author_name = models.CharField(max_length=120, blank=True)
    rating = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=140, blank=True)
    body = models.TextField(blank=True)
    verified_purchase = models.BooleanField(default=False)
    approved = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["product", "approved", "created_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(rating__gte=1) & Q(rating__lte=5), name="review_rating_1_to_5"),
            models.UniqueConstraint(
                fields=["product", "user"],
                condition=Q(user__isnull=False),
                name="unique_review_per_user_product",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.product_id and self.tenant_id:
            product_tenant_id = (
                self.product.tenant_id
                if "product" in self.__dict__ and self.product is not None
                else Product.objects.filter(pk=self.product_id).values_list("tenant_id", flat=True).first()
            )
            _validate_tenant_match(
                expected_tenant_id=self.tenant_id,
                actual_tenant_id=product_tenant_id,
                label="Product",
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.product.name} · {self.rating}★"


class StoreSettings(TenantScopedModel):
    """Singleton store branding/config (becomes per-tenant in the multi-tenant pass)."""

    store_name = models.CharField(max_length=120, default="Aster Commerce")
    tagline = models.CharField(max_length=200, blank=True)
    support_email = models.EmailField(blank=True)
    logo = models.ImageField(upload_to="branding/", blank=True, validators=[validate_image_upload])
    primary_color = models.CharField(max_length=9, default="#3b6fe6")
    accent_color = models.CharField(max_length=9, default="#245c24")
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        verbose_name_plural = "store settings"
        constraints = [
            models.UniqueConstraint(fields=["tenant"], name="unique_store_settings_per_tenant"),
        ]

    @classmethod
    def get_solo(cls) -> "StoreSettings":
        tid = get_current_tenant_id() or default_tenant_id()
        obj, _ = cls.objects.get_or_create(tenant_id=tid)
        return obj

    def __str__(self) -> str:
        return self.store_name


class TenantMembership(TenantScopedModel):
    """A user's staff membership + role in a tenant (multi-tenant access control)."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Manager"
        STAFF = "staff", "Staff"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenant_memberships")
    role = models.CharField(max_length=20, choices=Role, default=Role.STAFF)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="unique_membership_per_tenant"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.tenant_id} ({self.role})"


class Plan(TimeStampedModel):
    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    price_monthly = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    max_products = models.PositiveIntegerField(null=True, blank=True, help_text="Blank = unlimited.")
    max_orders_per_month = models.PositiveIntegerField(null=True, blank=True)
    features = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price_monthly"]

    def __str__(self) -> str:
        return f"{self.name} (${self.price_monthly}/mo)"


class Subscription(TenantScopedModel):
    """Store-wide subscription (simulated billing; per-tenant in the multi-tenant pass)."""

    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELLED = "cancelled", "Cancelled"

    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status, default=Status.TRIALING)
    trial_end = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant"], name="unique_subscription_per_tenant"),
        ]

    @classmethod
    def get_solo(cls) -> "Subscription":
        tid = get_current_tenant_id() or default_tenant_id()
        obj, _ = cls.objects.get_or_create(tenant_id=tid)
        return obj

    def __str__(self) -> str:
        return f"Subscription: {self.plan.name if self.plan else 'none'} ({self.status})"


class Invoice(TenantScopedModel):
    """A monthly (simulated) billing invoice for a tenant's subscription."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PAID = "paid", "Paid"
        PAST_DUE = "past_due", "Past due"
        VOID = "void", "Void"

    period_start = models.DateField()
    period_end = models.DateField()
    plan_name = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    orders_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status, default=Status.OPEN)

    class Meta:
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "period_start"], name="unique_invoice_per_period"),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.period_start} ${self.amount} ({self.status})"


class GiftCard(TenantScopedModel):
    code = models.CharField(max_length=40)
    initial_balance = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    balance = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    currency = models.CharField(max_length=3, default="USD")
    active = models.BooleanField(default=True)
    issued_to_email = models.EmailField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(balance__gte=0), name="gift_card_balance_nonnegative"),
            models.CheckConstraint(
                condition=Q(balance__lte=F("initial_balance")),
                name="gift_card_balance_lte_initial",
            ),
            models.UniqueConstraint(fields=["tenant", "code"], name="unique_gift_card_code_per_tenant"),
        ]

    def save(self, *args, **kwargs):
        self.code = normalize_coupon_code(self.code)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.code} (${self.balance})"


class StoreCredit(TenantScopedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="store_credits")
    balance = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(balance__gte=0), name="store_credit_balance_nonnegative"),
            models.UniqueConstraint(fields=["tenant", "user"], name="unique_store_credit_per_tenant"),
        ]

    def __str__(self) -> str:
        return f"StoreCredit<{self.user_id}@{self.tenant_id}> ${self.balance}"


class StoreCreditTransaction(TimeStampedModel):
    class Reason(models.TextChoices):
        GIFT_CARD = "gift_card", "Gift card redeemed"
        CHECKOUT_HOLD = "checkout_hold", "Held at checkout"
        CHECKOUT_SPEND = "checkout_spend", "Spent on order"
        HOLD_RELEASE = "hold_release", "Hold released"
        REFUND_CREDIT = "refund_credit", "Refunded to credit"
        MANUAL = "manual", "Manual adjustment"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="store_credit_transactions")
    objects = TenantManager()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="store_credit_transactions"
    )
    store_credit = models.ForeignKey(
        StoreCredit, null=True, blank=True, on_delete=models.CASCADE, related_name="transactions"
    )
    delta = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    reason = models.CharField(max_length=30, choices=Reason)
    order = models.ForeignKey("Order", null=True, blank=True, on_delete=models.SET_NULL)
    checkout_attempt = models.ForeignKey(CheckoutAttempt, null=True, blank=True, on_delete=models.SET_NULL)
    note = models.CharField(max_length=180, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "created_at"]), models.Index(fields=["tenant", "created_at"])]

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            if self.store_credit_id:
                self.tenant_id = (
                    StoreCredit.objects.filter(pk=self.store_credit_id).values_list("tenant_id", flat=True).first()
                )
            elif self.checkout_attempt_id:
                self.tenant_id = (
                    CheckoutAttempt.objects.filter(pk=self.checkout_attempt_id)
                    .values_list("tenant_id", flat=True)
                    .first()
                )
            elif self.order_id:
                self.tenant_id = Order.objects.filter(pk=self.order_id).values_list("tenant_id", flat=True).first()
        super().save(*args, **kwargs)


class ReturnRequest(TimeStampedModel):
    """A customer-initiated return/RMA against a delivered/paid order (spec §20.2)."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        RECEIVED = "received", "Received"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="return_requests")
    objects = TenantManager()
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="return_requests")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="return_requests"
    )
    status = models.CharField(max_length=20, choices=Status, default=Status.REQUESTED, db_index=True)
    reason = models.TextField(blank=True)
    staff_note = models.TextField(blank=True)
    refund = models.ForeignKey(
        "Refund", null=True, blank=True, on_delete=models.SET_NULL, related_name="return_requests"
    )

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.tenant_id is None and self.order_id:
            self.tenant_id = Order.objects.filter(pk=self.order_id).values_list("tenant_id", flat=True).first()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Return<{self.pk}> {self.order.order_number} ({self.status})"


class ReturnLine(TimeStampedModel):
    return_request = models.ForeignKey(ReturnRequest, on_delete=models.CASCADE, related_name="lines")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="return_lines")
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="return_line_quantity_positive"),
        ]

    def save(self, *args, **kwargs):
        if self.return_request_id and self.order_item_id:
            return_order_id = (
                self.return_request.order_id
                if "return_request" in self.__dict__ and self.return_request is not None
                else ReturnRequest.objects.filter(pk=self.return_request_id).values_list("order_id", flat=True).first()
            )
            order_item_order_id = (
                self.order_item.order_id
                if "order_item" in self.__dict__ and self.order_item is not None
                else OrderItem.objects.filter(pk=self.order_item_id).values_list("order_id", flat=True).first()
            )
            if (
                return_order_id is not None
                and order_item_order_id is not None
                and return_order_id != order_item_order_id
            ):
                raise ValidationError("Order item does not belong to this return request's order.")
        super().save(*args, **kwargs)


class WebhookEndpoint(TenantScopedModel):
    """A merchant-registered URL that receives signed domain-event callbacks."""

    url = models.URLField()
    secret = models.CharField(max_length=120, help_text="Used to HMAC-sign payloads.")
    description = models.CharField(max_length=180, blank=True)
    event_types = models.JSONField(default=list, blank=True, help_text="Subscribed event types; empty = all.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def subscribes_to(self, event_type: str) -> bool:
        return not self.event_types or event_type in self.event_types

    def __str__(self) -> str:
        return self.url


class WebhookDelivery(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    outbox_event = models.ForeignKey(
        "OutboxEvent", null=True, blank=True, on_delete=models.SET_NULL, related_name="webhook_deliveries"
    )
    event_type = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING, db_index=True)
    response_code = models.PositiveIntegerField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["endpoint", "outbox_event"], name="unique_webhook_delivery_per_event"),
        ]


class TaxRate(TenantScopedModel):
    """Merchant-configurable destination tax rate (spec §22.1)."""

    country = models.CharField(max_length=2, default="US")
    region = models.CharField(max_length=120, blank=True, help_text="Blank matches the whole country.")
    rate = models.DecimalField(max_digits=6, decimal_places=4)
    label = models.CharField(max_length=120, default="Sales tax")
    priority = models.IntegerField(default=0, help_text="Higher priority wins on a tie.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-priority", "country", "region"]
        indexes = [models.Index(fields=["active", "country", "region"])]
        constraints = [models.CheckConstraint(condition=Q(rate__gte=0), name="tax_rate_nonnegative")]

    def __str__(self) -> str:
        return f"{self.country}/{self.region or '*'} {self.rate}"


class ShippingRate(TenantScopedModel):
    """Merchant-configurable shipping method + rate (spec §22.2)."""

    method = models.CharField(max_length=80)
    label = models.CharField(max_length=120, blank=True)
    flat_amount = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    free_threshold = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, null=True, blank=True
    )
    min_subtotal = models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, default=money_zero
    )
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "method"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "method"], name="unique_shipping_rate_method_per_tenant"),
            models.CheckConstraint(condition=Q(flat_amount__gte=0), name="shipping_rate_amount_nonnegative"),
        ]

    def __str__(self) -> str:
        return f"{self.method} ${self.flat_amount}"


class InboundGatewayEvent(TimeStampedModel):
    """Raw inbound PSP webhook payload awaiting or after processing."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"
        IGNORED = "ignored", "Ignored"

    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.SET_NULL)
    provider = models.CharField(max_length=32, db_index=True)
    provider_event_id = models.CharField(max_length=160, blank=True, db_index=True)
    event_type = models.CharField(max_length=80, blank=True)
    gateway_reference = models.CharField(max_length=120, blank=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    signature = models.CharField(max_length=256, blank=True)
    status = models.CharField(max_length=20, choices=Status, default=Status.RECEIVED, db_index=True)
    processing_result = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["provider", "status", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"],
                condition=~Q(provider_event_id=""),
                name="unique_inbound_gateway_event",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_type or self.provider_event_id}"


class SimulatedGatewayIntent(TimeStampedModel):
    """Provider-side memory for the simulated gateway.

    Kept deliberately separate from ``Payment`` so the simulator behaves like an
    external system that cannot read the application database (ADR-0009).
    """

    gateway_reference = models.CharField(max_length=120, unique=True)
    status = models.CharField(max_length=32)
    amount = models.DecimalField(max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES)
    currency = models.CharField(max_length=3, default="USD")

    def __str__(self) -> str:
        return f"{self.gateway_reference} ({self.status})"


class IdempotencyRecord(TimeStampedModel):
    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="idempotency_records")
    objects = TenantManager()
    scope = models.CharField(max_length=80)
    key = models.CharField(max_length=120)
    actor_hash = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=Status, default=Status.IN_PROGRESS)
    request_hash = models.CharField(max_length=128, blank=True)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.JSONField(default=dict, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "key", "actor_hash", "tenant"],
                name="unique_idempotency_record",
            ),
        ]
        indexes = [models.Index(fields=["expires_at"])]


m2m_changed.connect(_product_collections_changed, sender=Product.collections.through)
m2m_changed.connect(_product_related_changed, sender=Product.related_products.through)
