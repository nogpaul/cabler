"""Fetch, load, diff and apply polygon sources (CORINE land cover, protected areas) end to end.

Tiny GeoPackages stand in for the publishers' files; they are served zipped by a simulated host.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import io
import os
import struct
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psycopg
import pyogrio.raw
import pytest

from cabler import apply, db, diff, fetch, load, remote, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)


def ring_wkb(points: list[tuple[float, float]]) -> bytes:
    """A little-endian WKB Polygon with one ring."""
    header = struct.pack("<BIII", 1, 3, 1, len(points))
    return header + b"".join(struct.pack("<dd", *p) for p in points)


def multi(points: list[tuple[float, float]]) -> bytes:
    """A MultiPolygon with one polygon whose outer ring is `points` (closed by the caller)."""
    return struct.pack("<BII", 1, 6, 1) + ring_wkb(points)


def square(x: float, y: float, size: float = 100.0) -> bytes:
    return multi([(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)])


# a bow-tie: its ring crosses itself, so it is invalid and has to be repaired
BOW_TIE = multi([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)])


def make_zip(member: str, layer: str, fields: list[str], rows: list[tuple[Any, ...]]) -> bytes:
    """A zip holding one GeoPackage member; each row is (geometry, *attribute values)."""
    scratch = io.BytesIO()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "data.gpkg"
        pyogrio.raw.write(
            path,
            geometry=np.array([r[0] for r in rows], dtype=object),
            field_data=[
                np.array([r[1 + i] for r in rows], dtype=object) for i in range(len(fields))
            ],
            fields=fields,
            geometry_type="MultiPolygon",
            crs="EPSG:25832",
            driver="GPKG",
            layer=layer,
        )
        with zipfile.ZipFile(scratch, "w") as archive:
            archive.write(path, member)
    return scratch.getvalue()


def land_cover_zip(rows: list[tuple[Any, ...]]) -> bytes:
    return make_zip("clc5_2021/CLC5ha_2021.gpkg", "clc5ha_2021", ["CLC21"], rows)


def reserve_zip(rows: list[tuple[Any, ...]]) -> bytes:
    fields = ["gml_id", "NAME", "BL", "LEG_DATE", "LEG_TITEL"]
    return make_zip("Naturschutzgebiete.xml", "Naturschutzgebiete", fields, rows)


class Publisher:
    def __init__(self, body: bytes) -> None:
        self.body, self.etag = body, '"r1"'
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def release(self, body: bytes, etag: str) -> None:
        self.body, self.etag = body, etag

    def handle(self, request: httpx.Request) -> httpx.Response:
        headers = {"ETag": self.etag, "Content-Length": str(len(self.body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(self.body))


def spec_of(source: str, source_id: str) -> sources.SourceSpec:
    return dataclasses.replace(sources.load_all()[source], source_id=source_id)


def analysed(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute(
        "SELECT reltuples FROM pg_class WHERE oid = %s::regclass", (table,)
    ).fetchone()
    return row is not None and row[0] >= 0


def test_land_cover_is_loaded_repaired_flagged_and_kept_with_history(tmp_path: Path) -> None:
    spec = spec_of("corine_clc5_2021", "itest_corine")
    release1 = land_cover_zip(
        [
            (square(0, 0), "231"),
            (square(200, 0), "111"),
            (BOW_TIE, "312"),
            (square(400, 0), "ABC"),
        ]
    )
    release2 = land_cover_zip(
        [
            (square(0, 50), "231"),  # moved
            (square(200, 0), "112"),  # reclassified
            (BOW_TIE, "312"),
            (square(400, 0), "ABC"),
        ]
    )
    publisher = Publisher(release1)
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        loaded = load.load_source(conn, spec, tmp_path)
        assert loaded.counts == {"land_cover": 4}
        assert analysed(conn, "raw.feature")
        (base,) = diff.diff_source(conn, spec).steps
        assert (base.added, base.total_after) == (4, 4)

        (step,) = apply.apply_source(conn, spec).steps
        assert (step.opened, step.closed, step.current) == (4, 0, 4)
        assert step.kinds == {
            "Agricultural areas": 1,
            "Artificial surfaces": 1,
            "Forest and semi-natural areas": 1,
            "unknown": 1,
        }
        assert step.flags == {"clc_code_unknown": 1, "geometry_repaired": 1}
        repaired = conn.execute(
            "SELECT ST_IsValid(geom), ST_SRID(geom), GeometryType(geom) FROM staging.land_cover"
            " WHERE source_id = %s AND 'geometry_repaired' = ANY (qa_flags)",
            (spec.source_id,),
        ).fetchone()
        assert repaired == (True, 25832, "MULTIPOLYGON")

        publisher.release(release2, '"r2"')
        fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)
        (change,) = diff.diff_source(conn, spec).steps
        assert (change.added, change.changed, change.removed) == (0, 2, 0)
        assert change.changed_fields == {"CLC21": 1, "geometry": 1}

        (step2,) = apply.apply_source(conn, spec).steps
        assert (step2.opened, step2.closed, step2.current) == (2, 2, 4)
        now = dict(
            conn.execute(
                "SELECT feature_key, clc_code FROM staging.land_cover_current"
                " WHERE source_id = %s ORDER BY feature_key",
                (spec.source_id,),
            ).fetchall()
        )
        assert now == {"1": "231", "2": "112", "3": "312", "4": "ABC"}
        then = dict(
            conn.execute(
                "SELECT feature_key, clc_code FROM staging.land_cover_as_of(%s, %s)"
                " ORDER BY feature_key",
                (spec.source_id, first.version_id),
            ).fetchall()
        )
        assert then == {"1": "231", "2": "111", "3": "312", "4": "ABC"}


def test_protected_areas_are_staged_with_their_kind_state_and_computed_area(
    tmp_path: Path,
) -> None:
    spec = spec_of("bfn_naturschutzgebiete", "itest_reserves")
    release1 = reserve_zip(
        [
            (
                square(0, 0, 100),
                "Naturschutzgebiete.1",
                "Heath",
                "NW",
                "2001-05-17T00:00:00",
                "Law A",
            ),
            (square(500, 0, 200), "Naturschutzgebiete.2", "Moor", "HH", None, None),
            (
                square(900, 0, 100),
                "Naturschutzgebiete.3",
                "Fen",
                "NI",
                "1999-01-01T00:00:00",
                "Law C",
            ),
        ]
    )
    release2 = reserve_zip(
        [
            (
                square(0, 0, 100),
                "Naturschutzgebiete.1",
                "Heath",
                "NW",
                "2001-05-17T00:00:00",
                "Law A",
            ),
            (square(500, 0, 300), "Naturschutzgebiete.2", "Moor", "HH", None, None),  # grew
            (square(1300, 0, 100), "Naturschutzgebiete.4", "Dune", "SH", None, None),  # new
        ]
    )
    publisher = Publisher(release1)
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)
        diff.diff_source(conn, spec)
        (step,) = apply.apply_source(conn, spec).steps
        assert step.kinds == {"nature_reserve": 3}
        assert step.flags == {}
        row = conn.execute(
            "SELECT kind, name, state_code, designated, legal_title, area_ha"
            " FROM staging.protected_area_current WHERE source_id = %s AND feature_key = %s",
            (spec.source_id, "Naturschutzgebiete.1"),
        ).fetchone()
        assert row is not None
        assert (row[0], row[1], row[2], str(row[3]), row[4]) == (
            "nature_reserve",
            "Heath",
            "NW",
            "2001-05-17",
            "Law A",
        )
        assert float(row[5]) == 1.0  # 100 m x 100 m = 1 ha

        publisher.release(release2, '"r2"')
        fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)
        (change,) = diff.diff_source(conn, spec).steps
        assert (change.added, change.changed, change.removed) == (1, 1, 1)
        assert change.changed_fields == {"geometry": 1}
        (step2,) = apply.apply_source(conn, spec).steps
        assert (step2.opened, step2.closed, step2.current) == (2, 2, 3)
        areas = dict(
            conn.execute(
                "SELECT feature_key, area_ha FROM staging.protected_area_current"
                " WHERE source_id = %s ORDER BY feature_key",
                (spec.source_id,),
            ).fetchall()
        )
        assert {k: float(v) for k, v in areas.items()} == {
            "Naturschutzgebiete.1": 1.0,
            "Naturschutzgebiete.2": 9.0,
            "Naturschutzgebiete.4": 1.0,
        }


def test_a_polygon_source_with_an_unknown_theme_has_no_applier() -> None:
    spec = dataclasses.replace(
        spec_of("corine_clc5_2021", "itest_x"), load_themes=("land_cover", "bird_area")
    )
    with db.connect("app") as conn, pytest.raises(apply.ApplyError, match="no applier"):
        apply.apply_source(conn, spec)
