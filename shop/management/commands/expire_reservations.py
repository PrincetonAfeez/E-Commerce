from django.core.management.base import BaseCommand

from shop.services.inventory import expire_reservations


class Command(BaseCommand):
    help = "Expire active reservations whose checkout attempt is not in payment."

    def handle(self, *args, **options):
        count = expire_reservations()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} reservations."))
