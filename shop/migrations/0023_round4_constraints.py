"""Round 4 schema constraints, indexes, and optional tenant FKs on audit/email models"""
from django.db import migrations, models
import django.db.models.deletion


def _default_tenant(apps):
    Tenant = apps.get_model("shop", "Tenant")
    return Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()


def dedupe_customer_group_names(apps, schema_editor):
    CustomerGroup = apps.get_model("shop", "CustomerGroup")
    seen: set[tuple[int, str]] = set()
    for group in CustomerGroup.objects.order_by("tenant_id", "name", "id"):
        key = (group.tenant_id, group.name)
        if key in seen:
            suffix = 2
            new_name = f"{group.name} ({suffix})"
            while (group.tenant_id, new_name) in seen:
                suffix += 1
                new_name = f"{group.name} ({suffix})"
            CustomerGroup.objects.filter(pk=group.pk).update(name=new_name)
            seen.add((group.tenant_id, new_name))
        else:
            seen.add(key)


def dedupe_shipping_rate_methods(apps, schema_editor):
    ShippingRate = apps.get_model("shop", "ShippingRate")
    seen: set[tuple[int, str]] = set()
    for rate in ShippingRate.objects.order_by("tenant_id", "method", "id"):
        key = (rate.tenant_id, rate.method)
        if key in seen:
            suffix = 2
            new_method = f"{rate.method}-{suffix}"
            while (rate.tenant_id, new_method) in seen:
                suffix += 1
                new_method = f"{rate.method}-{suffix}"
            ShippingRate.objects.filter(pk=rate.pk).update(method=new_method)
            seen.add((rate.tenant_id, new_method))
        else:
            seen.add(key)


def backfill_audit_email_tenants(apps, schema_editor):
    tenant = _default_tenant(apps)
    if tenant is None:
        return
    tid = tenant.pk
    AuditLog = apps.get_model("shop", "AuditLog")
    EmailDelivery = apps.get_model("shop", "EmailDelivery")
    Order = apps.get_model("shop", "Order")
    OutboxEvent = apps.get_model("shop", "OutboxEvent")

    for delivery in EmailDelivery.objects.filter(tenant__isnull=True):
        tenant_id = tid
        if delivery.order_id:
            tenant_id = Order.objects.filter(pk=delivery.order_id).values_list("tenant_id", flat=True).first() or tid
        elif delivery.outbox_event_id:
            tenant_id = (
                OutboxEvent.objects.filter(pk=delivery.outbox_event_id)
                .values_list("tenant_id", flat=True)
                .first()
                or tid
            )
        EmailDelivery.objects.filter(pk=delivery.pk).update(tenant_id=tenant_id)

    # Audit logs predate tenant tagging; leave null unless we can infer from Order metadata.
    for log in AuditLog.objects.filter(tenant__isnull=True, object_type="Order"):
        order_tid = Order.objects.filter(pk=log.object_id).values_list("tenant_id", flat=True).first()
        if order_tid:
            AuditLog.objects.filter(pk=log.pk).update(tenant_id=order_tid)


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0022_round3_followup"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="audit_logs",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="emaildelivery",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="email_deliveries",
                to="shop.tenant",
            ),
        ),
        migrations.RunPython(backfill_audit_email_tenants, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="promotionredemption",
            name="unique_promotion_redemption_order",
        ),
        migrations.RunPython(dedupe_customer_group_names, migrations.RunPython.noop),
        migrations.RunPython(dedupe_shipping_rate_methods, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="customergroup",
            constraint=models.UniqueConstraint(
                fields=("tenant", "name"),
                name="unique_customer_group_name_per_tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="shippingrate",
            constraint=models.UniqueConstraint(
                fields=("tenant", "method"),
                name="unique_shipping_rate_method_per_tenant",
            ),
        ),
        migrations.AddConstraint(
            model_name="promotionredemption",
            constraint=models.UniqueConstraint(
                fields=("tenant", "order", "promotion"),
                name="unique_promotion_redemption_per_tenant",
            ),
        ),
        migrations.RemoveIndex(
            model_name="product",
            name="shop_produc_status_a6bcb3_idx",
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                fields=["tenant", "status", "featured"],
                name="shop_product_tenant_status_feat_idx",
            ),
        ),
    ]
