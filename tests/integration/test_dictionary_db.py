"""The data dictionary against the real catalogue: nothing undocumented, and the document current.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import os

import pytest

from cabler import db, dictionary

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)


def test_every_schema_table_view_column_and_function_has_a_description() -> None:
    with db.connect("ro") as conn:
        found = dictionary.read(conn)
    assert dictionary.missing(found) == []
    assert len(found.relations) > 40  # the catalogue was read, not an empty schema


def test_the_written_document_is_the_one_the_database_gives() -> None:
    with db.connect("ro") as conn:
        text = dictionary.render(dictionary.read(conn))
    written = (db.REPO_ROOT / "docs" / "data-dictionary.md").read_text(encoding="utf-8")
    assert written == text, "docs/data-dictionary.md is out of date: run make dictionary"


def test_a_new_column_without_a_description_is_found() -> None:
    with db.connect("owner") as conn, conn.transaction(force_rollback=True):
        conn.execute("ALTER TABLE meta.source ADD COLUMN itest_undocumented text")
        found = dictionary.read(conn)
    assert dictionary.missing(found) == ["column meta.source.itest_undocumented"]
