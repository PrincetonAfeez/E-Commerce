"""Re-drive dead-letter queue items (failed outbox events and webhook deliveries)."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.dead_letters import dead_letter_counts, requeue_failed_outbox, requeue_failed_webhooks


class Command(BaseCommand):
    help = "Re-queue FAILED outbox events and/or webhook deliveries for another delivery attempt."

    def add_arguments(self, parser):
        parser.add_argument(
            "--outbox",
            action="store_true",
            help="Re-queue failed outbox events.",
        )
        parser.add_argument(
            "--webhooks",
            action="store_true",
            help="Re-queue failed webhook deliveries.",
        )
        parser.add_argument("--limit", type=int, default=200, help="Max records to re-queue per type.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report dead-letter counts without re-queuing.",
        )

    def handle(self, *args, **options):
        counts = dead_letter_counts()
        self.stdout.write(
            f"Dead letters: outbox_failed={counts['outbox_failed']}, "
            f"webhook_deliveries_failed={counts['webhook_deliveries_failed']}"
        )
        if options["dry_run"]:
            return

        targets = []
        if options["outbox"]:
            targets.append("outbox")
        if options["webhooks"]:
            targets.append("webhooks")
        if not targets:
            targets = ["outbox", "webhooks"]

        with single_instance("reprocess_dead_letters") as acquired:
            if not acquired:
                self.stdout.write("Another reprocess_dead_letters run is in progress; skipping.")
                return
            outbox_requeued = 0
            webhook_requeued = 0
            if "outbox" in targets:
                outbox_requeued = requeue_failed_outbox(limit=options["limit"])
            if "webhooks" in targets:
                webhook_requeued = requeue_failed_webhooks(limit=options["limit"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Re-queued {outbox_requeued} outbox event(s) and {webhook_requeued} webhook delivery(ies)."
                )
            )
