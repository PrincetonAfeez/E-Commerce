# Round 3 follow-up: tenant FKs on ledger models, remove dual Order↔Attempt link, constraints
from django.db import migrations, models
import django.db.models.deletion


def _default_tenant(apps):
    Tenant = apps.get_model("shop", "Tenant")
    return Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()


def backfill_tenant_fks(apps, schema_editor):
    tenant = _default_tenant(apps)
    if tenant is None:
        return
    tid = tenant.pk
    Payment = apps.get_model("shop", "Payment")
    PaymentEvent = apps.get_model("shop", "PaymentEvent")
    PromotionRedemption = apps.get_model("shop", "PromotionRedemption")
    StoreCreditTransaction = apps.get_model("shop", "StoreCreditTransaction")
    ReturnRequest = apps.get_model("shop", "ReturnRequest")
    CheckoutAttempt = apps.get_model("shop", "CheckoutAttempt")
    Order = apps.get_model("shop", "Order")
    StoreCredit = apps.get_model("shop", "StoreCredit")

    for payment in Payment.objects.filter(tenant__isnull=True).select_related("checkout_attempt"):
        attempt_tid = (
            payment.checkout_attempt.tenant_id
            if payment.checkout_attempt_id
            else None
        )
        Payment.objects.filter(pk=payment.pk).update(tenant_id=attempt_tid or tid)

    for event in PaymentEvent.objects.filter(tenant__isnull=True):
        tenant_id = tid
        if event.payment_id:
            tenant_id = Payment.objects.filter(pk=event.payment_id).values_list("tenant_id", flat=True).first() or tid
        elif event.checkout_attempt_id:
            tenant_id = (
                CheckoutAttempt.objects.filter(pk=event.checkout_attempt_id)
                .values_list("tenant_id", flat=True)
                .first()
                or tid
            )
        PaymentEvent.objects.filter(pk=event.pk).update(tenant_id=tenant_id)

    for redemption in PromotionRedemption.objects.filter(tenant__isnull=True):
        order_tid = Order.objects.filter(pk=redemption.order_id).values_list("tenant_id", flat=True).first()
        PromotionRedemption.objects.filter(pk=redemption.pk).update(tenant_id=order_tid or tid)

    for tx in StoreCreditTransaction.objects.filter(tenant__isnull=True):
        tenant_id = tid
        if tx.store_credit_id:
            tenant_id = (
                StoreCredit.objects.filter(pk=tx.store_credit_id).values_list("tenant_id", flat=True).first() or tid
            )
        elif tx.checkout_attempt_id:
            tenant_id = (
                CheckoutAttempt.objects.filter(pk=tx.checkout_attempt_id)
                .values_list("tenant_id", flat=True)
                .first()
                or tid
            )
        elif tx.order_id:
            tenant_id = Order.objects.filter(pk=tx.order_id).values_list("tenant_id", flat=True).first() or tid
        StoreCreditTransaction.objects.filter(pk=tx.pk).update(tenant_id=tenant_id)

    for rr in ReturnRequest.objects.filter(tenant__isnull=True):
        order_tid = Order.objects.filter(pk=rr.order_id).values_list("tenant_id", flat=True).first()
        ReturnRequest.objects.filter(pk=rr.pk).update(tenant_id=order_tid or tid)


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0021_round3_constraints"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="checkoutattempt",
            name="order",
        ),
        migrations.AddField(
            model_name="payment",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payments",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="paymentevent",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payment_events",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="promotionredemption",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="promotion_redemptions",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="storecredittransaction",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="store_credit_transactions",
                to="shop.tenant",
            ),
        ),
        migrations.AddField(
            model_name="returnrequest",
            name="tenant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="return_requests",
                to="shop.tenant",
            ),
        ),
        migrations.RunPython(backfill_tenant_fks, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="payment",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payments",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="paymentevent",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payment_events",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="promotionredemption",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="promotion_redemptions",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="storecredittransaction",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="store_credit_transactions",
                to="shop.tenant",
            ),
        ),
        migrations.AlterField(
            model_name="returnrequest",
            name="tenant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="return_requests",
                to="shop.tenant",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(fields=["tenant", "status"], name="shop_payment_tenant_status_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentevent",
            index=models.Index(fields=["tenant", "created_at"], name="shop_payevent_tenant_created_idx"),
        ),
        migrations.AddIndex(
            model_name="storecredittransaction",
            index=models.Index(fields=["tenant", "created_at"], name="shop_sctx_tenant_created_idx"),
        ),
        migrations.AddConstraint(
            model_name="customergroup",
            constraint=models.CheckConstraint(
                condition=models.Q(percent_off__gte=0),
                name="customer_group_percent_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="customergroup",
            constraint=models.CheckConstraint(
                condition=models.Q(percent_off__lte=100),
                name="customer_group_percent_max_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricelistentry",
            constraint=models.CheckConstraint(
                condition=models.Q(price__gte=0),
                name="price_list_entry_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="giftcard",
            constraint=models.CheckConstraint(
                condition=models.Q(balance__lte=models.F("initial_balance")),
                name="gift_card_balance_lte_initial",
            ),
        ),
        migrations.AddConstraint(
            model_name="taxrate",
            constraint=models.CheckConstraint(
                condition=models.Q(rate__gte=0),
                name="tax_rate_nonnegative",
            ),
        ),
    ]
