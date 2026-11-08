"""The fetch stage against a real database, with a simulated publisher and a temporary data folder.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import hashlib
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from cabler import checks, db, fetch, remote, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)


def test_fetch_then_check_then_refetch_follows_the_publisher(
    tmp_path: Path, itest_spec: sources.SourceSpec
) -> None:
    spec = itest_spec
    publisher: dict[str, Any] = {"etag": '"v1"', "body": b"first release" * 50}

    def handler(request: httpx.Request) -> httpx.Response:
        body: bytes = publisher["body"]
        headers = {"ETag": publisher["etag"], "Content-Length": str(len(body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    client = remote.make_client(httpx.MockTransport(handler))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)

        first = fetch.fetch_source(conn, client, spec, tmp_path)
        assert first.status == "fetched"
        assert first.sha256 == hashlib.sha256(publisher["body"]).hexdigest()
        assert checks.check_source(conn, client, spec).outcome == "unchanged"

        assert fetch.fetch_source(conn, client, spec, tmp_path).status == "skipped"

        publisher["etag"] = '"v2"'
        publisher["body"] = b"second release" * 50
        assert checks.check_source(conn, client, spec).outcome == "changed"
        second = fetch.fetch_source(conn, client, spec, tmp_path)
        assert second.status == "fetched"
        assert second.version_id != first.version_id

        publisher["etag"] = '"v3"'
        third = fetch.fetch_source(conn, client, spec, tmp_path)
        assert third.status == "duplicate"
        assert third.version_id == second.version_id
        assert checks.check_source(conn, client, spec).outcome == "unchanged"

        rows = conn.execute(
            "SELECT status, local_path IS NOT NULL FROM meta.source_version WHERE source_id = %s"
            " ORDER BY version_id",
            (spec.source_id,),
        ).fetchall()
        assert rows == [("fetched", True), ("fetched", True)]
        assert len(list((tmp_path / spec.source_id).iterdir())) == 2
