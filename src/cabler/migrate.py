"""Apply versioned SQL migrations and record them in the database.

Migrations are plain ``.sql`` files in ``db/migrations`` named ``NNNN_description.sql``. They run in
order, each inside its own transaction, as the owner role, and are recorded in
``meta.schema_migration`` with a checksum so that an edited, already-applied file is detected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from cabler import db

MIGRATIONS_DIR = db.REPO_ROOT / "db" / "migrations"
FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    """Migration files or the recorded history are inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read every migration file; versions must run 1..N with no gaps or duplicates."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if match is None:
            msg = f"bad migration file name: {path.name} (expected NNNN_description.sql)"
            raise MigrationError(msg)
        migrations.append(Migration(int(match[1]), match[2], path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        msg = f"versions must run 1..N without gaps or duplicates, found {versions}"
        raise MigrationError(msg)
    return migrations
