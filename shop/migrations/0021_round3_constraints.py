# Round 3 schema constraints and indexes
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0020_idempotency_tenant_required"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="promotion",
            constraint=models.CheckConstraint(
                condition=models.Q(discount_percent__lte=100),
                name="promotion_percent_max_100",
            ),
        ),
        migrations.AddIndex(
            model_name="idempotencyrecord",
            index=models.Index(fields=["expires_at"], name="shop_idempo_expires_idx"),
        ),
    ]
