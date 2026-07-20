# Expires active inventory reservations for non-payment checkout attempts

from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.inventory import expire_reservations


class Command(BaseCommand):
    help = "Expire active reservations whose checkout attempt is not in payment."

    def handle(self, *args, **options):
        with single_instance("expire_reservations") as acquired:
            if not acquired:
                self.stdout.write("Another worker is expiring reservations; skipping.")
                return
            count = expire_reservations()
            self.stdout.write(self.style.SUCCESS(f"Expired {count} reservations."))
