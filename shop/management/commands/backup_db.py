"""Runs pg_dump to write a compressed Postgres logical backup to disk"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management.base import BaseCommand

from shop.locks import single_instance
from shop.services.db_backup import pg_dump, resolve_pg_connection


class Command(BaseCommand):
    help = (
        "Take a compressed logical backup of the Postgres database via pg_dump "
        "(spec §28: backup/restore). See docs/runbooks/backup-restore.md."
    )

    def add_arguments(self, parser):

        parser.add_argument(
            "--out-dir",
            default=os.environ.get("BACKUP_DIR", "backups"),
            help="Directory to write the dump into (created if missing).",
        )

        parser.add_argument(
            "--retention-days",
            type=int,
            default=30,
            help="Delete dump files older than this many days after a successful backup.",
        )

    def handle(self, *args, **options):

        with single_instance("backup_db") as acquired:
            if not acquired:
                self.stdout.write("Another worker is running backup_db; skipping.")

                return

            connection = resolve_pg_connection()

            out_dir = Path(options["out_dir"])

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

            target = out_dir / f"{connection.name}-{stamp}.dump"

            pg_dump(target, connection=connection)

            size_mb = target.stat().st_size / (1024 * 1024)

            self.stdout.write(self.style.SUCCESS(f"Backup written: {target} ({size_mb:.1f} MiB)"))

            self._upload_to_s3(target)

            pruned = self._prune_old_dumps(out_dir, options["retention_days"])

            if pruned:
                self.stdout.write(
                    self.style.SUCCESS(f"Pruned {pruned} dump(s) older than {options['retention_days']} days.")
                )

    def _prune_old_dumps(self, out_dir: Path, retention_days: int) -> int:

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        pruned = 0

        for path in out_dir.glob("*.dump"):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

            if mtime < cutoff:
                path.unlink()

                pruned += 1

        return pruned

    def _upload_to_s3(self, target: Path) -> None:

        import subprocess  # noqa: S404

        from django.core.management.base import CommandError

        bucket = os.environ.get("BACKUP_S3_BUCKET", "").strip()

        if not bucket:
            return

        prefix = os.environ.get("BACKUP_S3_PREFIX", "db").strip("/")

        key = f"{prefix}/{target.name}" if prefix else target.name

        argv = ["aws", "s3", "cp", str(target), f"s3://{bucket}/{key}"]

        try:
            subprocess.run(argv, check=True)  # noqa: S603

        except FileNotFoundError as exc:
            raise CommandError("BACKUP_S3_BUCKET is set but the aws CLI was not found on PATH.") from exc

        except subprocess.CalledProcessError as exc:
            raise CommandError(f"S3 upload failed with exit code {exc.returncode}.") from exc

        self.stdout.write(self.style.SUCCESS(f"Backup uploaded to s3://{bucket}/{key}"))
