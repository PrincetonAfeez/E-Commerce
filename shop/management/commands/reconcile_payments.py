"""Polls the simulated gateway to resolve stranded or abandoned payment attempts"""


from datetime import timedelta

from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.payments import reconcile_stranded_payments


class Command(BaseCommand):
    help = "Poll the simulated gateway for stranded payment-pending checkout attempts."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-seconds", type=int, default=0)
        parser.add_argument(
            "--abandon-authorized-after-seconds",
            type=int,
            default=3600,
            help="Cancel authorizations never confirmed after this many seconds (releases stock).",
        )

    def handle(self, *args, **options):
        with single_instance("reconcile_payments") as acquired:
            if not acquired:
                self.stdout.write("Another worker is reconciling payments; skipping.")
                return
            count = reconcile_stranded_payments(
                older_than=timedelta(seconds=options["older_than_seconds"]),
                abandon_authorized_after=timedelta(seconds=options["abandon_authorized_after_seconds"]),
            )
            self.stdout.write(self.style.SUCCESS(f"Resolved {count} stranded payments."))
