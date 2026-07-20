"""Enqueues recent outbox events to webhook endpoints and delivers pending ones"""
from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.webhooks import deliver_pending, scan_and_enqueue


class Command(BaseCommand):
    help = "Enqueue recent domain events to webhook endpoints and deliver pending ones."

    def add_arguments(self, parser):
        parser.add_argument("--since-minutes", type=int, default=1440)

    def handle(self, *args, **options):
        with single_instance("deliver_webhooks") as acquired:
            if not acquired:
                self.stdout.write("Another worker is delivering webhooks; skipping.")
                return
            enqueued = scan_and_enqueue(since_minutes=options["since_minutes"])
            sent, failed = deliver_pending()
            self.stdout.write(self.style.SUCCESS(f"Enqueued {enqueued}, delivered {sent}, failed {failed}."))
