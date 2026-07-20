# Issues this month simulated subscription invoice for each subscribed tenant
from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.plans import run_billing_cycle


class Command(BaseCommand):
    help = "Issue this month's subscription invoice for each subscribed tenant (simulated)."

    def handle(self, *args, **options):
        with single_instance("run_billing") as acquired:
            if not acquired:
                self.stdout.write("Another worker is running billing; skipping.")
                return
            count = run_billing_cycle()
            self.stdout.write(self.style.SUCCESS(f"Issued {count} invoices."))
