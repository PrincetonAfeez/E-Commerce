# Charges and fulfils customer subscriptions whose next renewal date is due
from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.subscriptions import generate_due_renewals


class Command(BaseCommand):
    help = "Charge and fulfil subscriptions whose next renewal date is due."

    def handle(self, *args, **options):
        with single_instance("run_subscription_billing") as acquired:
            if not acquired:
                self.stdout.write("Another worker is running subscription billing; skipping.")
                return
            count = generate_due_renewals()
            self.stdout.write(self.style.SUCCESS(f"Generated {count} subscription renewals."))
