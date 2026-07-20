"""Shared Postgres backup/restore helpers for backup_db and verify_backup_restore."""
from __future__ import annotations

import os
import subprocess  # noqa: S404
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.core.management.base import CommandError


@dataclass(frozen=True)
class PgConnection:
    host: str
    port: str
    name: str
    user: str
    password: str

    def libpq_env(self, base: dict | None = None) -> dict:
        env = dict(base or os.environ)
        if self.password:
            env["PGPASSWORD"] = unquote(self.password)
        return env

    def admin_connection(self) -> PgConnection:
        """Connect to the maintenance DB for CREATE/DROP DATABASE."""
        return PgConnection(
            host=self.host,
            port=self.port,
            name="postgres",
            user=self.user,
            password=self.password,
        )


def resolve_pg_connection() -> PgConnection:
    db = settings.DATABASES["default"]
    engine = db.get("ENGINE", "")
    if "postgresql" not in engine:
        raise CommandError(f"Postgres backup/restore requires PostgreSQL; DATABASES['default'] uses {engine!r}.")
    url = os.environ.get("DATABASE_URL")
    if url:
        parsed = urlparse(url)
        return PgConnection(
            host=parsed.hostname or "localhost",
            port=str(parsed.port or 5432),
            name=parsed.path.lstrip("/"),
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
        )
    return PgConnection(
        host=str(db.get("HOST") or "localhost"),
        port=str(db.get("PORT") or 5432),
        name=str(db.get("NAME") or ""),
        user=str(db.get("USER") or ""),
        password=str(db.get("PASSWORD") or ""),
    )


def pg_dump(target: Path, *, connection: PgConnection | None = None) -> Path:
    connection = connection or resolve_pg_connection()
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(target),
        "--host",
        connection.host,
        "--port",
        connection.port,
        "--username",
        connection.user,
        connection.name,
    ]
    try:
        subprocess.run(argv, env=connection.libpq_env(), check=True)  # noqa: S603
    except FileNotFoundError as exc:
        raise CommandError("pg_dump not found on PATH. Install the Postgres client tools.") from exc
    except subprocess.CalledProcessError as exc:
        raise CommandError(f"pg_dump failed with exit code {exc.returncode}.") from exc
    return target


def pg_restore(dump_path: Path, *, target: PgConnection) -> None:
    argv = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--clean",
        "--if-exists",
        "--dbname",
        target.name,
        "--host",
        target.host,
        "--port",
        target.port,
        "--username",
        target.user,
        str(dump_path),
    ]
    try:
        subprocess.run(argv, env=target.libpq_env(), check=True)  # noqa: S603
    except FileNotFoundError as exc:
        raise CommandError("pg_restore not found on PATH. Install the Postgres client tools.") from exc
    except subprocess.CalledProcessError as exc:
        raise CommandError(f"pg_restore failed with exit code {exc.returncode}.") from exc


def create_database(name: str, *, admin: PgConnection) -> None:
    argv = [
        "createdb",
        "--host",
        admin.host,
        "--port",
        admin.port,
        "--username",
        admin.user,
        name,
    ]
    try:
        subprocess.run(argv, env=admin.libpq_env(), check=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        raise CommandError(f"createdb failed with exit code {exc.returncode}.") from exc


def drop_database(name: str, *, admin: PgConnection) -> None:
    argv = [
        "dropdb",
        "--if-exists",
        "--host",
        admin.host,
        "--port",
        admin.port,
        "--username",
        admin.user,
        name,
    ]
    subprocess.run(argv, env=admin.libpq_env(), check=True)  # noqa: S603


def verify_restored_schema(name: str, *, connection: PgConnection) -> int:
    """Return migration count from a restored database."""
    argv = [
        "psql",
        "--host",
        connection.host,
        "--port",
        connection.port,
        "--username",
        connection.user,
        "--dbname",
        name,
        "-tAc",
        "SELECT COUNT(*) FROM django_migrations",
    ]
    try:
        result = subprocess.run(  # noqa: S603
            argv,
            env=connection.libpq_env(),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise CommandError(f"psql verification failed with exit code {exc.returncode}.") from exc
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise CommandError("Could not read migration count from restored database.") from exc
