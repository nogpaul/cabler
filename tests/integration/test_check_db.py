"""The check stage against a real database, with a simulated publisher.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import os
from typing import Any

import httpx
import psycopg
import pytest

from cabler import checks, db, remote, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)


def last_run_id(conn: psycopg.Connection[Any]) -> int:
    """The newest run id so far, so a test only looks at the runs it created itself."""
    row = conn.execute("SELECT coalesce(max(run_id), 0) FROM meta.pipeline_run").fetchone()
    return int(row[0]) if row else 0


def test_check_compares_with_the_latest_fetched_version_and_records_runs() -> None:
    spec = sources.load_all()["osm_hamburg"]
    publisher = {"etag": '"v1"'}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": publisher["etag"], "Content-Length": "123"})

    client = remote.make_client(httpx.MockTransport(handler))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        baseline = last_run_id(conn)

        assert checks.check_source(conn, client, spec).outcome == "new"

        conn.execute(
            "INSERT INTO meta.source_version"
            " (source_id, remote_url, remote_etag, content_length, sha256, status)"
            " VALUES (%s, %s, %s, %s, %s, 'fetched')",
            (spec.source_id, spec.url, '"v1"', 123, "a" * 64),
        )
        assert checks.check_source(conn, client, spec).outcome == "unchanged"

        publisher["etag"] = '"v2"'
        assert checks.check_source(conn, client, spec).outcome == "changed"

        rows = conn.execute(
            "SELECT stage, status, result->>'outcome' FROM meta.pipeline_run"
            " WHERE source_id = %s AND run_id > %s ORDER BY run_id",
            (spec.source_id, baseline),
        ).fetchall()
        assert rows == [
            ("check", "succeeded", "new"),
            ("check", "succeeded", "unchanged"),
            ("check", "succeeded", "changed"),
        ]


def test_a_failed_check_is_recorded_with_its_error() -> None:
    spec = sources.load_all()["osm_hamburg"]
    client = remote.make_client(httpx.MockTransport(lambda request: httpx.Response(503)))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        baseline = last_run_id(conn)
        with pytest.raises(remote.RemoteStatusError):
            checks.check_source(conn, client, spec)
        row = conn.execute(
            "SELECT status, error FROM meta.pipeline_run WHERE source_id = %s AND run_id > %s",
            (spec.source_id, baseline),
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert "503" in row[1]
