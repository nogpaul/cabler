"""Hamburg parcels from an ALKIS exchange file: fetch, load, diff and apply, then a new release.

A tiny export (one XML part with three parcels) is served by a simulated host. The second release
moves one parcel, drops another and keeps the third.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest

from cabler import apply, db, diff, fetch, load, remote, sources

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<AX_Bestandsdatenauszug xmlns="http://www.adv-online.de/namespaces/adv/gid/7.1"'
    ' xmlns:gml="http://www.opengis.net/gml/3.2"><enthaelt><gml:FeatureCollection>'
)
TAIL = "</gml:FeatureCollection></enthaelt></AX_Bestandsdatenauszug>"


def parcel(key: str, x: float, y: float, size: float, area: str) -> str:
    ring = f"{x} {y} {x + size} {y} {x + size} {y + size} {x} {y + size} {x} {y}"
    return (
        f'<member><AX_Flurstueck gml:id="ID{key}">'
        f'<gml:identifier codeSpace="x">urn:adv:oid:ID{key}</gml:identifier>'
        "<lebenszeitintervall><AA_Lebenszeitintervall><beginnt>2023-10-25T08:05:02Z</beginnt>"
        "</AA_Lebenszeitintervall></lebenszeitintervall>"
        '<position><gml:Surface gml:id="S" srsName="urn:adv:crs:ETRS89_UTM32"><gml:patches>'
        "<gml:PolygonPatch><gml:exterior><gml:Ring><gml:curveMember><gml:Curve><gml:segments>"
        f"<gml:LineStringSegment><gml:posList>{ring}</gml:posList></gml:LineStringSegment>"
        "</gml:segments></gml:Curve></gml:curveMember></gml:Ring></gml:exterior>"
        "</gml:PolygonPatch></gml:patches></gml:Surface></position>"
        "<gemarkung><AX_Gemarkung_Schluessel><land>02</land>"
        "<gemarkungsnummer>0607</gemarkungsnummer></AX_Gemarkung_Schluessel></gemarkung>"
        "<flurstuecksnummer><AX_Flurstuecksnummer><zaehler>8752</zaehler><nenner>3</nenner>"
        "</AX_Flurstuecksnummer></flurstuecksnummer>"
        f"<flurstueckskennzeichen>{key}</flurstueckskennzeichen>"
        f'<amtlicheFlaeche uom="urn:adv:uom:m2">{area}</amtlicheFlaeche>'
        "<zeitpunktDerEntstehung>1998-10-16</zeitpunktDerEntstehung>"
        "<gemeindezugehoerigkeit><AX_Gemeindekennzeichen><land>02</land>"
        "<regierungsbezirk>0</regierungsbezirk><kreis>00</kreis><gemeinde>000</gemeinde>"
        "</AX_Gemeindekennzeichen></gemeindezugehoerigkeit>"
        "</AX_Flurstueck></member>"
    )


def export(*members: str) -> bytes:
    part = (HEAD + "".join(members) + TAIL).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("part_1.xml", part)
    return buffer.getvalue()


RELEASE_1 = export(
    parcel("P1", 100, 200, 10, "100"),  # 100 m2 and says 100
    parcel("P2", 300, 200, 10, "500"),  # 100 m2 but says 500
    parcel("P3", 500, 200, 10, ""),  # no register area
)
RELEASE_2 = export(
    parcel("P1", 100, 210, 10, "100"),  # moved
    parcel("P3", 500, 200, 10, ""),  # unchanged; P2 is gone
)


def test_hamburg_parcels_are_staged_with_history(tmp_path: Path) -> None:
    base = sources.load_all()["alkis_hamburg"]
    spec = dataclasses.replace(base, source_id="itest_alkis_hh", max_bytes=1_000_000)
    state = {"body": RELEASE_1}

    def handle(request: httpx.Request) -> httpx.Response:
        body = state["body"]
        headers = {"ETag": f'"{len(body)}-{hash(body)}"', "Content-Length": str(len(body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    client = remote.make_client(httpx.MockTransport(handle))
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        fetch.fetch_source(conn, client, spec, tmp_path)
        loaded = load.load_source(conn, spec, tmp_path)
        assert (loaded.status, loaded.counts) == ("loaded", {"parcel_nas": 3})
        first = loaded.version_id
        assert first is not None
        diff.diff_source(conn, spec)
        (step,) = apply.apply_source(conn, spec).steps
        assert (step.opened, step.closed, step.current) == (3, 0, 3)
        assert step.kinds == {"02": 3}
        assert step.flags == {"area_mismatch_large": 1, "area_missing": 1}
        row = conn.execute(
            "SELECT state_key, municipality_key, cadastral_district_key, number_numerator,"
            " number_denominator, area_official_m2, area_geometry_m2, updated::text, qa_flags,"
            " ST_SRID(geom) FROM staging.parcel_current"
            " WHERE source_id = %s AND feature_key = 'P1'",
            (spec.source_id,),
        ).fetchone()
        assert row == ("02", "000", "0607", "8752", "3", 100, 100, "2023-10-25", [], 25832)

        state["body"] = RELEASE_2
        fetch.fetch_source(conn, client, spec, tmp_path)
        assert load.load_source(conn, spec, tmp_path).status == "loaded"
        (change,) = diff.diff_source(conn, spec).steps
        assert (change.added, change.changed, change.removed) == (0, 1, 1)
        (step2,) = apply.apply_source(conn, spec).steps
        assert (step2.opened, step2.closed, step2.current) == (1, 2, 2)
        then = [
            r[0]
            for r in conn.execute(
                "SELECT feature_key FROM staging.parcel_as_of(%s, %s) ORDER BY 1",
                (spec.source_id, first),
            ).fetchall()
        ]
        assert then == ["P1", "P2", "P3"]
