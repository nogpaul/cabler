from pathlib import Path

import pytest

from cabler import migrate
from cabler.migrate import Migration, MigrationError


def write(directory: Path, name: str, body: str = "SELECT 1;") -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_discover_reads_files_in_version_order(tmp_path: Path) -> None:
    write(tmp_path, "0002_second.sql")
    write(tmp_path, "0001_first.sql")
    found = migrate.discover(tmp_path)
    assert [(m.version, m.name) for m in found] == [(1, "first"), (2, "second")]


def test_discover_rejects_gaps_and_duplicates(tmp_path: Path) -> None:
    write(tmp_path, "0001_a.sql")
    write(tmp_path, "0003_c.sql")
    with pytest.raises(MigrationError, match="without gaps"):
        migrate.discover(tmp_path)


def test_discover_rejects_badly_named_files(tmp_path: Path) -> None:
    write(tmp_path, "1_oops.sql")
    with pytest.raises(MigrationError, match="bad migration file name"):
        migrate.discover(tmp_path)


def test_the_real_migrations_directory_is_valid() -> None:
    assert migrate.discover(), "no migrations found in db/migrations"


def test_checksum_depends_on_content() -> None:
    assert Migration(1, "a", "SELECT 1;").checksum != Migration(1, "a", "SELECT 2;").checksum
    assert Migration(1, "a", "SELECT 1;").checksum == Migration(9, "b", "SELECT 1;").checksum
