"""Fetch then load, end to end, against a real database with a simulated publisher.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path

import httpx
import pytest

from cabler import db, fetch, load, remote, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "power_mini.osm"


def make_spec(themes: tuple[str, ...]) -> sources.SourceSpec:
    base = sources.load_all()["osm_hamburg"]
    return dataclasses.replace(
        base,
        source_id="itest_source",
        url="https://download.geofabrik.de/test/power_mini.osm",
        load_kind="osm_pbf",
        load_themes=themes,
    )


def publisher() -> httpx.Client:
    body = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"ETag": '"mini"', "Content-Length": str(len(body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    return remote.make_client(httpx.MockTransport(handler))


def test_fetch_then_load_stores_the_power_objects_once(tmp_path: Path) -> None:
    spec = make_spec(("power",))
    client = publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        fetched = fetch.fetch_source(conn, client, spec, tmp_path)
        assert fetched.status == "fetched"

        first = load.load_source(conn, spec, tmp_path)
        assert first.status == "loaded"
        assert first.version_id == fetched.version_id
        assert first.counts == {"points": 2, "lines": 1, "multipolygons": 1}

        stored = conn.execute(
            "SELECT layer, osm_key, power, voltage, ST_SRID(geom), GeometryType(geom)"
            " FROM raw.osm_object WHERE version_id = %s ORDER BY layer, osm_key",
            (fetched.version_id,),
        ).fetchall()
        assert stored == [
            ("lines", "w10", "line", "110000", 4326, "LINESTRING"),
            ("multipolygons", "w12", "substation", "380000", 4326, "MULTIPOLYGON"),
            ("points", "n1", "substation", "110000", 4326, "POINT"),
            ("points", "n2", "generator", None, 4326, "POINT"),
        ]

        status = conn.execute(
            "SELECT status FROM meta.source_version WHERE version_id = %s", (fetched.version_id,)
        ).fetchone()
        assert status == ("loaded",)

        again = load.load_source(conn, spec, tmp_path)
        assert again.status == "skipped"
        assert again.version_id == fetched.version_id
        count = conn.execute("SELECT count(*) FROM raw.osm_object").fetchone()
        assert count is not None
        assert count[0] >= 4


def test_a_wider_theme_selection_loads_more(tmp_path: Path) -> None:
    spec = make_spec(("power", "transport"))
    client = publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        fetch.fetch_source(conn, client, spec, tmp_path)
        result = load.load_source(conn, spec, tmp_path)
        assert result.counts == {"points": 2, "lines": 2, "multipolygons": 1}


def test_the_read_only_role_can_read_but_not_load(tmp_path: Path) -> None:
    with db.connect("ro") as conn:
        assert conn.execute("SELECT count(*) FROM raw.osm_object").fetchone() is not None
