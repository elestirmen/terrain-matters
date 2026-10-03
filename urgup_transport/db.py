"""PostgreSQL/PostGIS connection handling and a minimal forward-only migrator.

Faz 1 moves the system of record off single JSON files. This module owns the
connection and the schema; the stores that read and write rows sit on top of it.

Migrations are ordered `.sql` files under `migrations/`. Each runs in its own
transaction, is recorded with a checksum, and never runs twice. There is no
downgrade path on purpose: rolling a schema back in production is a restore,
not a script.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

DATABASE_URL_ENV = "DATABASE_URL"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# Serializes migration runs across workers and deploys.
MIGRATION_ADVISORY_LOCK = 0x55524755  # "URGU"

_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a database operation is attempted without DATABASE_URL."""


class MigrationError(RuntimeError):
    """Raised when the migration set on disk disagrees with what was applied."""


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV, "").strip()


def database_configured() -> bool:
    return bool(database_url())


def require_database_url() -> str:
    url = database_url()
    if not url:
        raise DatabaseNotConfiguredError(
            f"{DATABASE_URL_ENV} tanımlı değil. Örnek: "
            f"postgresql://urgup:parola@localhost:5432/urgup"
        )
    return url


@contextmanager
def connection(*, autocommit: bool = False):
    """Yield a psycopg connection, committing on success and rolling back on error."""
    import psycopg

    conn = psycopg.connect(require_database_url(), autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """Return `(version, path)` ordered by version, rejecting ambiguous names."""
    if not directory.is_dir():
        return []
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(
                f"Migration adı NNN_ad.sql biçiminde olmalı: {path.name}"
            )
        version = match.group(1)
        if version in found:
            raise MigrationError(
                f"{version} sürümü iki dosyada var: {found[version].name}, {path.name}"
            )
        found[version] = path
    return sorted(found.items())


def _ensure_migration_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version     text PRIMARY KEY,
            name        text NOT NULL,
            checksum    text NOT NULL,
            applied_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied_migrations(cursor) -> dict[str, dict[str, Any]]:
    _ensure_migration_table(cursor)
    cursor.execute("SELECT version, name, checksum FROM schema_migration")
    return {
        row[0]: {"version": row[0], "name": row[1], "checksum": row[2]}
        for row in cursor.fetchall()
    }


def migrate(*, directory: Path = MIGRATIONS_DIR, dry_run: bool = False) -> list[str]:
    """Apply every pending migration in order and return the versions applied."""
    migrations = discover_migrations(directory)
    if not migrations:
        return []

    applied_versions: list[str] = []
    with connection(autocommit=True) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK,))
        try:
            already = applied_migrations(cursor)
            for version, path in migrations:
                sql = path.read_text(encoding="utf-8")
                checksum = _checksum(sql)
                record = already.get(version)
                if record is not None:
                    if record["checksum"] != checksum:
                        raise MigrationError(
                            f"{path.name} uygulandıktan sonra değiştirilmiş. "
                            f"Uygulanmış migration düzenlenmez; yeni bir migration ekleyin."
                        )
                    continue
                if dry_run:
                    applied_versions.append(version)
                    continue
                log.info("Migration uygulanıyor: %s", path.name)
                cursor.execute("BEGIN")
                try:
                    cursor.execute(sql)
                    cursor.execute(
                        "INSERT INTO schema_migration (version, name, checksum) VALUES (%s, %s, %s)",
                        (version, path.name, checksum),
                    )
                    cursor.execute("COMMIT")
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
                applied_versions.append(version)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK,))
    return applied_versions


def pending_migrations(*, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Return versions on disk that the database has not applied yet."""
    with connection(autocommit=True) as conn, conn.cursor() as cursor:
        already = applied_migrations(cursor)
    return [version for version, _ in discover_migrations(directory) if version not in already]


def healthcheck() -> dict[str, Any]:
    """Report connectivity, PostGIS availability, and schema freshness."""
    try:
        with connection(autocommit=True) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            database, server_version = cursor.fetchone()
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
            postgis_row = cursor.fetchone()
            already = applied_migrations(cursor)
        pending = [
            version for version, _ in discover_migrations() if version not in already
        ]
        return {
            "connected": True,
            "database": database,
            "server_version": server_version.split(" ")[1] if " " in server_version else server_version,
            "postgis": postgis_row[0] if postgis_row else None,
            "applied_migrations": len(already),
            "pending_migrations": pending,
        }
    except DatabaseNotConfiguredError:
        return {"connected": False, "reason": "not_configured"}
    except Exception as exc:
        log.error("Veritabanı sağlık kontrolü başarısız: %s", exc)
        return {"connected": False, "reason": "unavailable"}
