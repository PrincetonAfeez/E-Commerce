"""Round-trip backup verification: pg_dump → pg_restore → schema smoke test."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from shop.locks import single_instance
from shop.services.db_backup import (
    create_database,
    drop_database,
    pg_dump,
    pg_restore,
    resolve_pg_connection,
    verify_restored_schema,
)


class Command(BaseCommand):
    help = (
        "Verify backups by restoring the latest dump into a throwaway database and "
        "checking django_migrations. See docs/runbooks/backup-restore.md."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out-dir",
            default=os.environ.get("BACKUP_DIR", "backups"),
            help="Directory for the temporary dump file.",
        )
        parser.add_argument(
            "--target-db",
            default="",
            help="Name of the throwaway restore database (auto-generated if omitted).",
        )
        parser.add_argument(
            "--keep-db",
            action="store_true",
            help="Leave the throwaway database in place (for manual inspection).",
        )

    def handle(self, *args, **options):
        with single_instance("verify_backup_restore") as acquired:
            if not acquired:
                self.stdout.write("Another verify_backup_restore run is in progress; skipping.")
                return

            source = resolve_pg_connection()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_dir = Path(options["out_dir"])
            dump_path = out_dir / f"{source.name}-verify-{stamp}.dump"
            target_name = options["target_db"] or f"{source.name}_restore_verify_{uuid.uuid4().hex[:8]}"
            admin = source.admin_connection()
            target = source.__class__(
                host=source.host,
                port=source.port,
                name=target_name,
                user=source.user,
                password=source.password,
            )

            self.stdout.write(f"Taking verification dump to {dump_path} ...")
            pg_dump(dump_path, connection=source)

            try:
                drop_database(target_name, admin=admin)
            except Exception:
                pass

            self.stdout.write(f"Creating throwaway database {target_name} ...")
            create_database(target_name, admin=admin)

            try:
                self.stdout.write("Restoring dump ...")
                pg_restore(dump_path, target=target)
                migration_count = verify_restored_schema(target_name, connection=target)
                if migration_count <= 0:
                    raise CommandError("Restored database has no django_migrations rows.")

                self.stdout.write("Running migrate --check against live database ...")
                call_command("migrate", "--check", verbosity=0)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Backup restore verified: {migration_count} migrations in restored DB; "
                        f"dump {dump_path} ({dump_path.stat().st_size / (1024 * 1024):.1f} MiB)."
                    )
                )
            finally:
                if not options["keep_db"]:
                    drop_database(target_name, admin=admin)
                if dump_path.exists() and not os.environ.get("KEEP_VERIFY_DUMP"):
                    dump_path.unlink(missing_ok=True)
