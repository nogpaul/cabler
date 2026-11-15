"""Scenes that several integration tests build: a published OSM extract, wind units, boundaries.

pytest puts this folder on the import path because tests/conftest.py is in it.
"""

import dataclasses
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psycopg
import pyogrio.raw

from cabler import apply, diff, fetch, load, remote, sources, validate

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
    fetched = fetch.fetch_source(conn, publisher.client, spec, raw)
    assert fetched.version_id is not None
    load.load_source(conn, spec, raw)
    diff.diff_source(conn, spec)
    apply.apply_source(conn, spec)
    return fetched.version_id


WIND_SQL = """
INSERT INTO staging.wind_unit
    (source_id, unit_key, valid_from_version_id, state_key, status, qa_flags, geom)
VALUES (%s, %s, %s, '99', %s, %s, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25832))
"""


def wind_units(conn: psycopg.Connection[Any], source_id: str, version: int) -> None:
    units = [
        ("w1", 10.0005, 53.5505, "In Betrieb", []),  # next to n101, 1.2 km from n102
        ("w2", 10.1295, 53.6290, "In Planung", []),  # next to the tagged 110 kV substation n106
        ("w3", 10.1190, 53.6195, "In Betrieb", []),  # next to n105, n104 and n103 are farther
        ("gone", 10.1005, 53.6005, "Endgültig stillgelegt", []),  # would put n103 in reach
        ("placeholder", 10.1005, 53.6005, "In Betrieb", ["coordinate_placeholder"]),
    ]
    for key, lon, lat, status, flags in units:
        conn.execute(WIND_SQL, (source_id, key, version, status, flags, lon, lat))


def prepared(
    conn: psycopg.Connection[Any], tmp_path: Path, publisher: Publisher | None = None
) -> tuple[sources.SourceSpec, int]:
    spec = make_spec()
    sources.register(conn, spec)
    version = ingest(conn, publisher or Publisher(), spec, tmp_path)
    assert validate.validate_source(conn, spec).status == "validated"
    wind_units(conn, spec.source_id, version)
    return spec, version


PLACE_FIELDS = ["OBJID", "AGS", "ARS", "GEN", "BEZ", "GF", "SN_L"]


def box(
    conn: psycopg.Connection[Any], west: float, south: float, east: float, north: float
) -> bytes:
    """A MultiPolygon in EPSG:25832 whose corners are given in longitude and latitude."""
    row = conn.execute(
        "SELECT ST_AsBinary(ST_Multi(ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, 4326), 25832)))",
        (west, south, east, north),
    ).fetchone()
    assert row is not None
    return bytes(row[0])


def utm_box(conn: psycopg.Connection[Any], x1: float, y1: float, x2: float, y2: float) -> bytes:
    row = conn.execute(
        "SELECT ST_AsBinary(ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 25832)))", (x1, y1, x2, y2)
    ).fetchone()
    assert row is not None
    return bytes(row[0])


def boundaries_zip(
    municipalities: list[tuple[bytes, str, str, str, int]], districts: list[tuple[bytes, str, str]]
) -> bytes:
    """A VG250-like zip: one GeoPackage with the layers vg250_gem and vg250_krs.

    A municipality is (geometry, AGS, name, OBJID, GF), a district (geometry, AGS, name).
    """
    scratch = io.BytesIO()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "DE_VG250.gpkg"
        layers = [
            (
                "vg250_gem",
                [m[0] for m in municipalities],
                [(m[3], m[1], m[1] + "0000", m[2], "Gemeinde", m[4], "02") for m in municipalities],
            ),
            (
                "vg250_krs",
                [d[0] for d in districts],
                [(f"K{d[1]}", d[1], d[1] + "0000000", d[2], "Kreis", 4, "02") for d in districts],
            ),
        ]
        for number, (layer, geometry, rows) in enumerate(layers):
            columns = [
                np.array([r[i] for r in rows], dtype=np.int16 if name == "GF" else object)
                for i, name in enumerate(PLACE_FIELDS)
            ]
            pyogrio.raw.write(
                path,
                geometry=np.array(geometry, dtype=object),
                field_data=columns,
                fields=PLACE_FIELDS,
                geometry_type="MultiPolygon",
                crs="EPSG:25832",
                driver="GPKG",
                layer=layer,
                append=number > 0,
            )
        with zipfile.ZipFile(scratch, "w") as archive:
            archive.write(path, "vg250_ebenen_0101/DE_VG250.gpkg")
    return scratch.getvalue()


class BodyPublisher:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        headers = {"ETag": '"b1"', "Content-Length": str(len(self.body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(self.body))
