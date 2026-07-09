# Data migration assigning existing rows to a default tenant
from django.db import migrations

# Tenant-scoped models that need their existing rows attached to the default tenant.
TENANT_MODELS = [
    "Category", "Collection", "Product", "ProductVariant", "Cart", "Promotion",
    "CouponCode", "GiftCard", "CheckoutAttempt", "Order", "Review", "WishlistItem",
    "Address", "WebhookEndpoint", "TaxRate", "ShippingRate", "StoreSettings", "Subscription",
]


def create_default_tenant(apps, schema_editor):
    Tenant = apps.get_model("shop", "Tenant")
    tenant, _ = Tenant.objects.get_or_create(
        slug="default", defaults={"name": "Default Store", "active": True}
    )
    for model_name in TENANT_MODELS:
        model = apps.get_model("shop", model_name)
        model.objects.filter(tenant__isnull=True).update(tenant=tenant)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0009_tenant_alter_category_slug_alter_collection_slug_and_more"),
    ]

    operations = [migrations.RunPython(create_default_tenant, noop)]
