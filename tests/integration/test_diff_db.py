"""Fetch, load and diff two releases of a source against a real database.

The simulated publisher first serves power_mini.osm, then power_mini_v2.osm, which re-tags n1,
deletes n2, adds n20 and reshapes w10 without touching its tags or version.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path

import httpx
import pytest

from cabler import db, diff, fetch, load, remote, sources
from cabler.diff import DiffError

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_spec() -> sources.SourceSpec:
    base = sources.load_all()["osm_hamburg"]
    return dataclasses.replace(
        base,
        source_id="itest_source",
        url="https://download.geofabrik.de/test/power_mini.osm",
        load_kind="osm_pbf",
        load_themes=("power",),
    )


class Publisher:
    """Serves one release at a time; call :meth:`release` to publish the next one."""

    def __init__(self) -> None:
        self.name = "power_mini.osm"
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


def test_the_first_snapshot_is_a_baseline_and_the_next_one_a_real_diff(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)

        baseline = diff.diff_source(conn, spec)
        assert baseline.status == "diffed"
        assert baseline.version_id == first.version_id
        (base,) = baseline.steps
        assert base.baseline
        assert (base.added, base.changed, base.removed) == (4, 0, 0)
        assert (base.total_before, base.total_after) == (0, 4)

        assert diff.diff_source(conn, spec).status == "skipped"
        assert load.load_source(conn, spec, tmp_path).status == "skipped"

        publisher.release("power_mini_v2.osm", '"r2"')
        second = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        assert second.status == "fetched"
        assert load.load_source(conn, spec, tmp_path).status == "loaded"

        result = diff.diff_source(conn, spec)
        assert result.version_id == second.version_id
        (step,) = result.steps
        assert step.previous_version_id == first.version_id
        assert (step.added, step.changed, step.removed) == (1, 2, 1)
        assert (step.total_before, step.total_after) == (4, 4)
        assert step.changed_fields == {
            "geometry": 1,
            "osm_timestamp": 1,
            "osm_version": 1,
            "voltage": 1,
        }

        rows = conn.execute(
            "SELECT layer, osm_key, change, changed_fields, old_osm_version, new_osm_version"
            " FROM raw.osm_change WHERE version_id = %s ORDER BY layer, osm_key",
            (second.version_id,),
        ).fetchall()
        assert rows == [
            ("lines", "w10", "changed", ["geometry"], 2, 2),
            ("points", "n1", "changed", ["osm_timestamp", "osm_version", "voltage"], 3, 4),
            ("points", "n2", "removed", [], 1, None),
            ("points", "n20", "added", [], None, 1),
        ]

        statuses = conn.execute(
            "SELECT status FROM meta.source_version WHERE source_id = %s ORDER BY version_id",
            (spec.source_id,),
        ).fetchall()
        assert statuses == [("diffed",), ("diffed",)]

        stages = conn.execute(
            "SELECT status, result->>'status' FROM meta.pipeline_run"
            " WHERE stage = 'diff' AND source_id = %s ORDER BY run_id",
            (spec.source_id,),
        ).fetchall()
        assert stages == [
            ("succeeded", "diffed"),
            ("succeeded", "skipped"),
            ("succeeded", "diffed"),
        ]


def test_several_loaded_snapshots_are_diffed_in_order_in_one_run(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)
        publisher.release("power_mini_v2.osm", '"r2"')
        second = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)

        result = diff.diff_source(conn, spec)
        assert [s.version_id for s in result.steps] == [first.version_id, second.version_id]
        assert [s.baseline for s in result.steps] == [True, False]
        assert result.version_id == second.version_id


def test_a_source_that_is_never_loaded_cannot_be_diffed() -> None:
    spec = dataclasses.replace(make_spec(), load_kind=None, load_themes=())
    with (
        db.connect("app") as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(DiffError, match="nothing is ever loaded"),
    ):
        diff.diff_source(conn, spec)


def test_the_change_table_is_readable_by_the_read_only_role() -> None:
    with db.connect("ro") as conn:
        assert conn.execute("SELECT count(*) FROM raw.osm_change").fetchone() is not None
