"""The data dictionary: what is read from the catalogue, what counts as undocumented, the document.

The system catalogue is replaced by canned rows, so these tests need no database.
"""

from pathlib import Path
from typing import Any

import pytest

from cabler import dictionary, pipeline
from cabler.dictionary import Column, Dictionary, Function, Relation


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Catalogue:
    """Answers the queries of :func:`dictionary.read` from canned rows."""

    def __init__(self, **answers: list[tuple[Any, ...]]) -> None:
        self.answers = answers

    def execute(self, query: str, params: Any = None) -> Result:
        names = {
            dictionary.MIGRATION_SQL: "migration",
            dictionary.SCHEMA_SQL: "schemas",
            dictionary.RELATION_SQL: "relations",
            dictionary.COLUMN_SQL: "columns",
            dictionary.PASSED_ON_SQL: "passed_on",
            dictionary.FUNCTION_SQL: "functions",
        }
        return Result(self.answers.get(names[query], []))


def test_a_view_column_takes_the_description_of_the_table_column_it_passes_on() -> None:
    comments = {("s", "t", "a"): "The a.", ("s", "v", "a"): None}
    sources = {("s", "v", "a"): [("s", "t")]}
    assert dictionary.passed_on(comments, sources) == {("s", "v", "a"): "The a."}


def test_the_description_is_followed_through_a_view_of_a_view() -> None:
    comments = {("s", "t", "a"): "The a.", ("s", "v", "a"): None, ("s", "w", "a"): None}
    sources = {("s", "v", "a"): [("s", "t")], ("s", "w", "a"): [("s", "v")]}
    assert dictionary.passed_on(comments, sources)[("s", "w", "a")] == "The a."


def test_two_sources_that_disagree_give_no_description_rather_than_a_guess() -> None:
    comments = {("s", "t", "k"): "Key of t.", ("s", "u", "k"): "Key of u.", ("s", "v", "k"): None}
    sources = {("s", "v", "k"): [("s", "t"), ("s", "u")]}  # the view joins t and u on k
    assert dictionary.passed_on(comments, sources) == {}


def test_two_sources_that_agree_give_their_description() -> None:
    comments = {("s", "t", "k"): "The key.", ("s", "u", "k"): "The key.", ("s", "v", "k"): None}
    sources = {("s", "v", "k"): [("s", "t"), ("s", "u")]}
    assert dictionary.passed_on(comments, sources) == {("s", "v", "k"): "The key."}


def test_an_own_comment_is_never_replaced_and_a_cycle_ends() -> None:
    comments = {("s", "v", "a"): "Own.", ("s", "w", "a"): None}
    sources = {("s", "v", "a"): [("s", "w")], ("s", "w", "a"): [("s", "w")]}
    assert dictionary.passed_on(comments, sources) == {}  # v has its own; w finds nothing


def catalogue() -> Catalogue:
    return Catalogue(
        migration=[(23,)],
        schemas=[("meta", "The catalogue."), ("core", None)],
        relations=[("meta", "source", "r", "The sources."), ("meta", "source_now", "v", None)],
        columns=[
            ("meta", "source", "source_id", "text", True, "Id | key."),
            ("meta", "source", "homepage", "text", False, None),
            ("meta", "source_now", "source_id", "text", False, None),
        ],
        passed_on=[("meta", "source_now", "source_id", "meta", "source")],
        functions=[("core", "refuse_change", "", "trigger", None)],
    )


def test_read_builds_the_dictionary_with_the_descriptions_passed_on() -> None:
    d = dictionary.read(catalogue())  # type: ignore[arg-type]
    assert d.migration == 23
    assert [r.kind for r in d.relations] == ["table", "view"]
    view_column = d.relations[1].columns[0]
    assert (view_column.comment, view_column.description) == (None, "Id | key.")
    assert d.functions == (Function("core", "refuse_change", "", "trigger", None),)


def test_missing_names_everything_without_a_description() -> None:
    d = dictionary.read(catalogue())  # type: ignore[arg-type]
    assert dictionary.missing(d) == [
        "schema core",
        "column meta.source.homepage",
        "view meta.source_now",  # its column is documented through the table
        "function core.refuse_change()",
    ]


def documented() -> Dictionary:
    column = Column("source_id", "text", True, "Id | key.")
    return Dictionary(
        migration=7,
        schemas=(("meta", "The catalogue."),),
        relations=(Relation("meta", "source", "table", "The sources.", (column,)),),
        functions=(Function("meta", "f", "x text", "integer", "Counts."),),
    )


def test_the_document_has_one_table_per_relation_and_escapes_what_breaks_a_table() -> None:
    text = dictionary.render(documented())
    assert text.startswith("# Data dictionary\n")
    assert "(migration 0007)" in text
    assert "### meta.source (table)" in text
    assert "| `source_id` | text | no | Id \\| key. |" in text  # a pipe would end the cell
    assert "| `meta.f(x text)` | integer | Counts. |" in text
    assert text.endswith("\n")
    assert dictionary.render(documented()) == text  # the same input gives the same document


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_connection: Any) -> Path:
    conn = fake_connection()
    monkeypatch.setattr(pipeline.db, "connect", lambda role, environ=None: conn)
    path = tmp_path / "data-dictionary.md"
    monkeypatch.setattr(pipeline, "DICTIONARY_PATH", path)
    return path


def test_the_command_writes_the_document(
    target: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.dictionary, "read", lambda conn: documented())
    assert pipeline.main(["dictionary", "dev"]) == 0
    assert target.read_text(encoding="utf-8") == dictionary.render(documented())
    assert "1 tables and views, 1 columns, 1 functions (migration 0007)" in capsys.readouterr().out


def test_the_check_fails_for_a_document_that_is_out_of_date(
    target: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipeline.dictionary, "read", lambda conn: documented())
    target.write_text("# Data dictionary\n\nold\n", encoding="utf-8")
    assert pipeline.main(["dictionary", "dev", "--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    pipeline.main(["dictionary", "dev"])
    assert pipeline.main(["dictionary", "dev", "--check"]) == 0


def test_nothing_is_written_while_something_has_no_description(
    target: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    incomplete = dictionary.read(catalogue())  # type: ignore[arg-type]
    monkeypatch.setattr(pipeline.dictionary, "read", lambda conn: incomplete)
    assert pipeline.main(["dictionary", "dev"]) == 1
    assert "no description for column meta.source.homepage" in capsys.readouterr().err
    assert not target.exists()
