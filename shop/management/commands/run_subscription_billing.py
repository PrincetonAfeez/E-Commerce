from django.core.management.base import BaseCommand

from shop.services.subscriptions import generate_due_renewals


class Command(BaseCommand):
    help = "Charge and fulfil subscriptions whose next renewal date is due."

    def handle(self, *args, **options):
        count = generate_due_renewals()
        self.stdout.write(self.style.SUCCESS(f"Generated {count} subscription renewals."))
