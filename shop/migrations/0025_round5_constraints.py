"""Round 5: one pending payment per checkout attempt (SVC5-002)"""
import logging

from django.db import migrations, models
from django.db.models import Q

logger = logging.getLogger(__name__)


def dedupe_pending_payments(apps, schema_editor):
    Payment = apps.get_model("shop", "Payment")
    seen_attempts: set[int] = set()
    for payment in Payment.objects.filter(status="pending").order_by("checkout_attempt_id", "id"):
        if payment.checkout_attempt_id in seen_attempts:
            logger.warning(
                "Demoting duplicate pending payment id=%s for checkout_attempt_id=%s",
                payment.pk,
                payment.checkout_attempt_id,
            )
            Payment.objects.filter(pk=payment.pk).update(status="failed")
        else:
            seen_attempts.add(payment.checkout_attempt_id)


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0024_remove_product_shop_produc_slug_76971b_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(dedupe_pending_payments, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                fields=("checkout_attempt",),
                condition=Q(status="pending"),
                name="unique_pending_payment_per_attempt",
            ),
        ),
    ]
