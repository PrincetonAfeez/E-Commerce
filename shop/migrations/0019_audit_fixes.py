# Schema audit fixes: tenant scoping, constraints, TenantCustomerProfile, and backfills
from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count, Q


TENANT_SCOPED_MODELS = [
    "Category",
    "Collection",
    "Product",
    "ProductVariant",
    "Cart",
    "Promotion",
    "CouponCode",
    "GiftCard",
    "CheckoutAttempt",
    "Order",
    "Review",
    "WishlistItem",
    "Address",
    "WebhookEndpoint",
    "TaxRate",
    "ShippingRate",
    "StoreSettings",
    "Subscription",
    "OutboxEvent",
    "CustomerGroup",
    "PriceListEntry",
    "CustomerSubscription",
    "TenantMembership",
    "Invoice",
    "EmailSuppression",
    "StoreCredit",
    "TenantCustomerProfile",
]


def _default_tenant(apps):
    Tenant = apps.get_model("shop", "Tenant")
    return Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()


def _infer_tenant_ids(apps, default_tenant):
    """Infer tenant_id from related FKs before falling back to the default tenant."""
    inferred: dict[str, int] = {}

    Order = apps.get_model("shop", "Order")
    CheckoutAttempt = apps.get_model("shop", "CheckoutAttempt")
    for order_id, attempt_id in Order.objects.filter(tenant__isnull=True).values_list("id", "checkout_attempt_id"):
        tid = CheckoutAttempt.objects.filter(pk=attempt_id).values_list("tenant_id", flat=True).first()
        if tid:
            inferred[f"Order:{order_id}"] = tid

    Cart = apps.get_model("shop", "Cart")
    CartItem = apps.get_model("shop", "CartItem")
    for item_id, cart_id in CartItem.objects.values_list("id", "cart_id"):
        tid = Cart.objects.filter(pk=cart_id).values_list("tenant_id", flat=True).first()
        if tid:
            inferred[f"CartItem:{item_id}"] = tid

    ProductVariant = apps.get_model("shop", "ProductVariant")
    Product = apps.get_model("shop", "Product")
    for variant_id, product_id in ProductVariant.objects.filter(tenant__isnull=True).values_list("id", "product_id"):
        tid = Product.objects.filter(pk=product_id).values_list("tenant_id", flat=True).first()
        if tid:
            inferred[f"ProductVariant:{variant_id}"] = tid

    return inferred


def backfill_audit_data(apps, schema_editor):
    tenant = _default_tenant(apps)
    if tenant is None:
        return

    Tenant = apps.get_model("shop", "Tenant")
    multi_tenant = Tenant.objects.count() > 1
    inferred = _infer_tenant_ids(apps, tenant) if multi_tenant else {}

    for model_name in TENANT_SCOPED_MODELS:
        model = apps.get_model("shop", model_name)
        null_rows = model.objects.filter(tenant__isnull=True)
        if not null_rows.exists():
            continue
        for row in null_rows.iterator():
            key = f"{model_name}:{row.pk}"
            tid = inferred.get(key, tenant.pk)
            model.objects.filter(pk=row.pk).update(tenant_id=tid)
        if multi_tenant and model.objects.filter(tenant__isnull=True).exists():
            print(
                f"WARNING: {model_name} still has tenant-null rows after inference; "
                "verify tenant assignments on multi-tenant databases."
            )

    IdempotencyRecord = apps.get_model("shop", "IdempotencyRecord")
    IdempotencyRecord.objects.filter(tenant__isnull=True).update(tenant=tenant)

    AccountProfile = apps.get_model("shop", "AccountProfile")
    TenantCustomerProfile = apps.get_model("shop", "TenantCustomerProfile")
    for profile in AccountProfile.objects.exclude(customer_group__isnull=True).select_related(
        "customer_group"
    ):
        group = profile.customer_group
        tid = group.tenant_id or tenant.pk
        TenantCustomerProfile.objects.get_or_create(
            tenant_id=tid,
            user_id=profile.user_id,
            defaults={"customer_group_id": group.pk},
        )

    StoreCredit = apps.get_model("shop", "StoreCredit")
    StoreCreditTransaction = apps.get_model("shop", "StoreCreditTransaction")
    for tx in StoreCreditTransaction.objects.filter(store_credit__isnull=True).iterator():
        sc = StoreCredit.objects.filter(user_id=tx.user_id).order_by("id").first()
        if sc is not None:
            StoreCreditTransaction.objects.filter(pk=tx.pk).update(store_credit_id=sc.pk)

    Address = apps.get_model("shop", "Address")
    duplicate_defaults = (
        Address.objects.filter(is_default=True)
        .values("tenant_id", "user_id")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
    )
    for row in duplicate_defaults:
        keepers = (
            Address.objects.filter(
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                is_default=True,
            )
            .order_by("-updated_at", "-id")
            .values_list("id", flat=True)
        )
        for addr_id in list(keepers)[1:]:
            Address.objects.filter(pk=addr_id).update(is_default=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0018_emailsuppression"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="idempotencyrecord",
            name="unique_idempotency_record",
        ),
        migrations.AddField(
            model_name="storecredit",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="emailsuppression",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="idempotencyrecord",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="idempotency_records",
                to="shop.tenant",
            ),
        ),
        migrations.CreateModel(
            name="TenantCustomerProfile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "customer_group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tenant_members",
                        to="shop.customergroup",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(class)ss",
                        to="shop.tenant",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tenant_customer_profiles",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="storecredittransaction",
            name="store_credit",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="transactions",
                to="shop.storecredit",
            ),
        ),
        migrations.AlterField(
            model_name="giftcard",
            name="code",
            field=models.CharField(max_length=40),
        ),
        migrations.AlterField(
            model_name="emailsuppression",
            name="email",
            field=models.EmailField(max_length=254),
        ),
        migrations.AlterField(
            model_name="storecredit",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="store_credits",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_audit_data, noop),
        migrations.RemoveField(
            model_name="accountprofile",
            name="customer_group",
        ),
        migrations.AlterField(
            model_name="address",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="cart",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="category",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="checkoutattempt",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="collection",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="couponcode",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="customergroup",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="customersubscription",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="emailsuppression",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="giftcard",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="invoice",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="order",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="outboxevent",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="pricelistentry",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="product",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="productvariant",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="promotion",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="review",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="shippingrate",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="storecredit",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="storesettings",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="taxrate",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="tenantcustomerprofile",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="tenantmembership",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="webhookendpoint",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="wishlistitem",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="%(class)ss",
                to="shop.tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="tenant",
            constraint=models.UniqueConstraint(
                condition=Q(primary_domain__gt=""),
                fields=("primary_domain",),
                name="unique_tenant_primary_domain_nonempty",
            ),
        ),
        migrations.AddConstraint(
            model_name="address",
            constraint=models.UniqueConstraint(
                condition=Q(is_default=True),
                fields=("tenant", "user"),
                name="unique_default_address_per_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkoutattempt",
            constraint=models.CheckConstraint(
                condition=Q(credit_applied__gte=0),
                name="attempt_credit_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="checkoutattempt",
            constraint=models.CheckConstraint(
                condition=Q(credit_applied__lte=models.F("total")),
                name="attempt_credit_lte_total",
            ),
        ),
        migrations.AddConstraint(
            model_name="emailsuppression",
            constraint=models.UniqueConstraint(
                fields=("tenant", "email"),
                name="unique_suppression_per_tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="giftcard",
            constraint=models.UniqueConstraint(
                fields=("tenant", "code"),
                name="unique_gift_card_code_per_tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="idempotencyrecord",
            constraint=models.UniqueConstraint(
                fields=("scope", "key", "actor_hash", "tenant"),
                name="unique_idempotency_record",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=Q(credit_applied__gte=0),
                name="order_credit_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=Q(credit_applied__lte=models.F("total")),
                name="order_credit_lte_total",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=Q(refund_total__lte=models.F("total")),
                name="order_refund_lte_total",
            ),
        ),
        migrations.AddConstraint(
            model_name="productvariant",
            constraint=models.CheckConstraint(
                condition=Q(compare_at_price__isnull=True)
                | Q(compare_at_price__gte=models.F("price")),
                name="variant_compare_at_gte_price",
            ),
        ),
        migrations.AddConstraint(
            model_name="storecredit",
            constraint=models.UniqueConstraint(
                fields=("tenant", "user"),
                name="unique_store_credit_per_tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="tenantcustomerprofile",
            constraint=models.UniqueConstraint(
                fields=("tenant", "user"),
                name="unique_customer_profile_per_tenant",
            ),
        ),
    ]
