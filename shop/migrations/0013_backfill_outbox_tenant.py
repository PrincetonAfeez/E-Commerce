# Data migration backfilling tenant on existing OutboxEvent rows
from django.db import migrations


def backfill(apps, schema_editor):
    Tenant = apps.get_model("shop", "Tenant")
    OutboxEvent = apps.get_model("shop", "OutboxEvent")
    tenant = Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()
    if tenant:
        OutboxEvent.objects.filter(tenant__isnull=True).update(tenant=tenant)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0012_outboxevent_tenant_alter_couponcode_normalized_code_and_more"),
    ]

    operations = [migrations.RunPython(backfill, noop)]
