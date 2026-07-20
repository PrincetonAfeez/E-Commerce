"""Remove uploaded media files not referenced by ProductImage or StoreSettings.logo."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.models import ProductImage, StoreSettings


class Command(BaseCommand):
    help = "Delete orphan files under MEDIA_ROOT (product-images/ and branding/)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report orphan files without deleting them.",
        )

    def handle(self, *args, **options):
        with single_instance("cleanup_orphan_media") as acquired:
            if not acquired:
                self.stdout.write("Another cleanup_orphan_media run is in progress; skipping.")
                return

            media_root = Path(settings.MEDIA_ROOT)
            if not media_root.exists():
                self.stdout.write("MEDIA_ROOT does not exist; nothing to clean.")
                return

            referenced: set[str] = set()
            for image in ProductImage.objects.exclude(image="").iterator():
                if image.image:
                    referenced.add(image.image.name)
            store = StoreSettings.get_solo()
            if store.logo:
                referenced.add(store.logo.name)

            orphans: list[Path] = []
            for subdir in ("product-images", "branding"):
                root = media_root / subdir
                if not root.is_dir():
                    continue
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(media_root).as_posix()
                    if rel not in referenced:
                        orphans.append(path)

            if not orphans:
                self.stdout.write("No orphan media files found.")
                return

            for path in orphans:
                rel = path.relative_to(media_root)
                if options["dry_run"]:
                    self.stdout.write(f"would delete {rel}")
                else:
                    path.unlink(missing_ok=True)
                    self.stdout.write(f"deleted {rel}")

            action = "Would remove" if options["dry_run"] else "Removed"
            self.stdout.write(self.style.SUCCESS(f"{action} {len(orphans)} orphan media file(s)."))
