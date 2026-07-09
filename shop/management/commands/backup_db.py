# Runs pg_dump to write a compressed Postgres logical backup to disk
import os
import subprocess  # noqa: S404 - invoked with a fixed argv, never a shell string
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


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

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        engine = db.get("ENGINE", "")
        if "postgresql" not in engine:
            raise CommandError(
                f"backup_db targets Postgres; DATABASES['default'] uses {engine!r}. "
                "In production the app runs on Postgres — run this against the prod/staging DB."
            )

        out_dir = Path(options["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = out_dir / f"{db.get('NAME', 'db')}-{stamp}.dump"

        # Build a libpq environment from Django's DB config (or DATABASE_URL) so no
        # credentials land on the command line / process table.
        env = os.environ.copy()
        url = os.environ.get("DATABASE_URL")
        if url:
            parsed = urlparse(url)
            host, port, name = parsed.hostname, parsed.port, parsed.path.lstrip("/")
            user, password = parsed.username, parsed.password
        else:
            host, port, name = db.get("HOST"), db.get("PORT"), db.get("NAME")
            user, password = db.get("USER"), db.get("PASSWORD")
        if password:
            env["PGPASSWORD"] = unquote(password)

        argv = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(target)]
        if host:
            argv += ["--host", str(host)]
        if port:
            argv += ["--port", str(port)]
        if user:
            argv += ["--username", str(unquote(user))]
        argv += [str(name)]

        try:
            subprocess.run(argv, env=env, check=True)  # noqa: S603 - fixed argv, no shell
        except FileNotFoundError as exc:
            raise CommandError("pg_dump not found on PATH. Install the Postgres client tools.") from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"pg_dump failed with exit code {exc.returncode}.") from exc

        size_mb = target.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f"Backup written: {target} ({size_mb:.1f} MiB)"))
