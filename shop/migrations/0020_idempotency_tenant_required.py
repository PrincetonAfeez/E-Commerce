# Data migration: enforce non-null IdempotencyRecord.tenant
from django.db import migrations, models
import django.db.models.deletion


def backfill_idempotency_tenant(apps, schema_editor):
    Tenant = apps.get_model("shop", "Tenant")
    IdempotencyRecord = apps.get_model("shop", "IdempotencyRecord")
    default = Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()
    if default is None:
        return
    IdempotencyRecord.objects.filter(tenant__isnull=True).update(tenant_id=default.pk)


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0019_audit_fixes"),
    ]

    operations = [
        migrations.RunPython(backfill_idempotency_tenant, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="idempotencyrecord",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="idempotency_records",
                to="shop.tenant",
            ),
        ),
    ]
