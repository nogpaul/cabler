"""The validate stage against a real database: verdicts, quarantine and the substation review queue.

The simulated publisher serves fixtures from tests/fixtures. power_hints.osm holds a 110 kV line and
six substations, five of them without a tagged voltage; the other files are bad follow-up
deliveries (almost everything gone, nothing at all, a feature far outside Germany).

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from cabler import apply, db, diff, fetch, load, remote, sources, validate
from cabler.validate import ValidateError

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_spec() -> sources.SourceSpec:
    base = sources.load_all()["osm_hamburg"]
    return dataclasses.replace(
        base,
        source_id="itest_source",
        url="https://download.geofabrik.de/test/power_hints.osm",
        load_kind="osm_pbf",
        load_themes=("power",),
    )


class Publisher:
    """Serves one release at a time; call :meth:`release` to publish the next one."""

    def __init__(self) -> None:
        self.name = "power_hints.osm"
        self.etag = '"r1"'
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def release(self, name: str, etag: str) -> None:
        self.name, self.etag = name, etag

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = (FIXTURES / self.name).read_bytes()
        headers = {"ETag": self.etag, "Content-Length": str(len(body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


def ingest(
    conn: psycopg.Connection[Any], publisher: Publisher, spec: sources.SourceSpec, raw: Path
) -> int:
    """Fetch, load, diff and apply whatever the publisher serves now; return the version id."""
    fetched = fetch.fetch_source(conn, publisher.client, spec, raw)
    assert fetched.version_id is not None
    load.load_source(conn, spec, raw)
    diff.diff_source(conn, spec)
    apply.apply_source(conn, spec)
    return fetched.version_id


def test_hints_are_collected_and_the_review_queue_makes_suggestions(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        version = ingest(conn, publisher, spec, tmp_path)

        result = validate.validate_source(conn, spec)
        assert result.status == "validated"
        assert result.version_id == version
        (step,) = result.steps
        assert step.outcome == "passed"
        assert (step.errors, step.warnings, step.total) == (0, 1, 7)
        assert step.rules == {
            "voltage_hint_conflict": 1,
            "voltage_hint_line": 2,
            "voltage_hint_type": 3,
        }
        assert validate.validate_source(conn, spec).status == "skipped"

        assert validate.review_counts(conn, spec.source_id) == {
            "conflict": 1,
            "hv_candidate": 2,
            "likely_low_voltage": 1,
            "no_evidence": 1,
        }
        queue = conn.execute(
            "SELECT osm_key, suggestion, line_hint_v, line_osm_key, type_hint_class, conflict"
            " FROM staging.substation_review WHERE source_id = %s ORDER BY osm_key",
            (spec.source_id,),
        ).fetchall()
        assert queue == [
            ("n101", "hv_candidate", 110000, "w100", None, False),
            ("n102", "conflict", 110000, "w100", "medium_low", True),
            ("n103", "likely_low_voltage", None, None, "medium_low", False),
            ("n104", "hv_candidate", None, None, "high_plus", False),
            ("n105", "no_evidence", None, None, None, False),
        ]
        only = validate.review_rows(conn, spec.source_id, "conflict", 10)
        assert [row[0] for row in only] == ["n102"]


def test_a_delivery_that_loses_most_objects_is_quarantined(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        ingest(conn, publisher, spec, tmp_path)
        assert validate.validate_source(conn, spec).steps[0].outcome == "passed"

        publisher.release("power_hints_cut.osm", '"r2"')
        second = ingest(conn, publisher, spec, tmp_path)
        result = validate.validate_source(conn, spec)
        (step,) = result.steps
        assert result.quarantined
        assert (step.outcome, step.errors, step.total) == ("quarantined", 1, 1)
        assert step.problems[0].startswith("removal_share: 6 of 7 objects disappeared")

        verdicts = conn.execute(
            "SELECT outcome FROM staging.snapshot_validation WHERE source_id = %s"
            " ORDER BY version_id",
            (spec.source_id,),
        ).fetchall()
        assert verdicts == [("passed",), ("quarantined",)]
        status = conn.execute(
            "SELECT status FROM meta.source_version WHERE version_id = %s", (second,)
        ).fetchone()
        assert status == ("applied",)


def test_an_empty_delivery_is_quarantined_twice_over(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        ingest(conn, publisher, spec, tmp_path)
        validate.validate_source(conn, spec)

        publisher.release("power_empty.osm", '"r2"')
        ingest(conn, publisher, spec, tmp_path)
        (step,) = validate.validate_source(conn, spec).steps
        assert step.outcome == "quarantined"
        assert (step.errors, step.total) == (2, 0)
        assert set(step.rules) == {"empty_state", "removal_share"}


def test_a_feature_outside_germany_is_an_error(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        ingest(conn, publisher, spec, tmp_path)
        validate.validate_source(conn, spec)

        publisher.release("power_hints_outside.osm", '"r2"')
        ingest(conn, publisher, spec, tmp_path)
        (step,) = validate.validate_source(conn, spec).steps
        assert step.outcome == "quarantined"
        assert step.rules["outside_germany"] == 1
        assert "n107" in step.problems[0]


def test_a_source_without_the_power_theme_cannot_be_validated() -> None:
    spec = dataclasses.replace(make_spec(), load_themes=("water",))
    with (
        db.connect("app") as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(ValidateError, match="no validator"),
    ):
        validate.validate_source(conn, spec)


def test_the_validation_tables_are_readable_by_the_read_only_role() -> None:
    with db.connect("ro") as conn:
        for table in ("snapshot_validation", "validation_finding", "substation_review"):
            assert conn.execute(f"SELECT count(*) FROM staging.{table}").fetchone() is not None  # noqa: S608
