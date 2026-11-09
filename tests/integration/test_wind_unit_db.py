"""Fetch, load, diff and apply two releases of a MaStR export against a real database.

Release 1 has four wind units, each with a different kind of problem (a placeholder position, no
position and unreadable numbers, a position outside its state). Release 2 changes the status and
position of one unit, removes one and adds one.

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
from cabler.apply import ApplyError

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

CATALOGUE = {
    "31": "In Planung",
    "35": "In Betrieb",
    "38": "Endgültig stillgelegt",
    "1645": "Senvion SE",
    "888": "Windenergie an Land",
}
HAMBURG = {"Gemeindeschluessel": "02000000", "Gemeinde": "Hamburg"}
NRW = {"Gemeindeschluessel": "05914000", "Gemeinde": "Witten"}


def unit(key: str, **fields: str) -> str:
    body = "".join(
        f"<{name}>{value}</{name}>" for name, value in {**fields, "EinheitMastrNummer": key}.items()
    )
    return f"<EinheitWind>{body}</EinheitWind>"


def export(units: list[str]) -> bytes:
    xml = "<?xml version='1.0' encoding='UTF-16'?>"
    values = "".join(
        f"<Katalogwert><Id>{i}</Id><Wert>{w}</Wert><KatalogKategorieId>4</KatalogKategorieId></Katalogwert>"
        for i, w in CATALOGUE.items()
    )
    categories = "<Katalogkategorie><Id>4</Id><Name>Betriebsstatus</Name></Katalogkategorie>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "EinheitenWind.xml",
            (xml + "<EinheitenWind>" + "".join(units) + "</EinheitenWind>").encode("utf-16"),
        )
        archive.writestr(
            "Katalogwerte.xml",
            (xml + "<Katalogwerte>" + values + "</Katalogwerte>").encode("utf-16"),
        )
        archive.writestr(
            "Katalogkategorien.xml",
            (xml + "<Katalogkategorien>" + categories + "</Katalogkategorien>").encode("utf-16"),
        )
    return buffer.getvalue()


def fields(extra: dict[str, str], **more: str) -> dict[str, str]:
    return {**extra, **more}


RELEASE_1 = export(
    [
        unit(
            "SEE1",
            **fields(
                HAMBURG,
                EinheitBetriebsstatus="35",
                Bruttoleistung="2000.000",
                Hersteller="1645",
                WindAnLandOderAufSee="888",
                Laengengrad="10.0",
                Breitengrad="53.55",
                Inbetriebnahmedatum="2015-02-04",
            ),
        ),
        unit(
            "SEE2",
            **fields(
                NRW,
                EinheitBetriebsstatus="31",
                Bruttoleistung="3000.000",
                Laengengrad="6.0",
                Breitengrad="50.0",
            ),
        ),
        unit(
            "SEE3",
            **fields(
                NRW,
                EinheitBetriebsstatus="35",
                Bruttoleistung="many",
                Inbetriebnahmedatum="2015-02-31",
            ),
        ),
        unit(
            "SEE4",
            **fields(
                NRW,
                EinheitBetriebsstatus="31",
                Bruttoleistung="4000.000",
                Laengengrad="11.0",
                Breitengrad="52.0",
            ),
        ),
    ]
)
RELEASE_2 = export(
    [
        unit(
            "SEE1",
            **fields(
                HAMBURG,
                EinheitBetriebsstatus="38",
                Bruttoleistung="2000.000",
                Hersteller="1645",
                WindAnLandOderAufSee="888",
                Laengengrad="10.01",
                Breitengrad="53.55",
                Inbetriebnahmedatum="2015-02-04",
            ),
        ),
        unit(
            "SEE3",
            **fields(
                NRW,
                EinheitBetriebsstatus="35",
                Bruttoleistung="many",
                Inbetriebnahmedatum="2015-02-31",
            ),
        ),
        unit(
            "SEE4",
            **fields(
                NRW,
                EinheitBetriebsstatus="31",
                Bruttoleistung="4000.000",
                Laengengrad="11.0",
                Breitengrad="52.0",
            ),
        ),
        unit(
            "SEE5",
            **fields(
                NRW,
                EinheitBetriebsstatus="35",
                Bruttoleistung="25000.000",
                Laengengrad="7.5",
                Breitengrad="51.5",
            ),
        ),
    ]
)


def make_spec() -> sources.SourceSpec:
    base = sources.load_all()["mastr_bulk"]
    return dataclasses.replace(
        base,
        source_id="itest_mastr",
        url="https://download.marktstammdatenregister.de/test/export.zip",
    )


class Publisher:
    """Serves one release at a time; call :meth:`release` to publish the next one."""

    def __init__(self) -> None:
        self.body = RELEASE_1
        self.etag = '"r1"'
        self.client = remote.make_client(httpx.MockTransport(self.handle))

    def release(self, body: bytes, etag: str) -> None:
        self.body, self.etag = body, etag

    def handle(self, request: httpx.Request) -> httpx.Response:
        headers = {"ETag": self.etag, "Content-Length": str(len(self.body))}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(self.body))


def test_wind_units_are_diffed_decoded_checked_and_kept_with_history(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)

        (baseline,) = diff.diff_source(conn, spec).steps
        assert baseline.baseline
        # four units, five catalogue values and one catalogue category
        assert (baseline.added, baseline.total_after) == (10, 10)

        applied = apply.apply_source(conn, spec)
        (step,) = applied.steps
        assert (step.opened, step.closed, step.current) == (4, 0, 4)
        assert step.kinds == {"In Betrieb": 2, "In Planung": 2}
        assert step.flags == {
            "capacity_missing": 1,
            "coordinate_missing": 1,
            "coordinate_outside_state": 1,
            "coordinate_placeholder": 1,
            "date_unparsable": 1,
        }
        row = conn.execute(
            "SELECT state_key, status, manufacturer, location_type, gross_kw, commissioned,"
            " ST_SRID(geom), qa_flags FROM staging.wind_unit_current WHERE unit_key = 'SEE1'"
        ).fetchone()
        assert row is not None
        assert row[:6] == ("02", "In Betrieb", "Senvion SE", "Windenergie an Land", 2000, row[5])
        assert str(row[5]) == "2015-02-04"
        assert (row[6], row[7]) == (25832, [])

        publisher.release(RELEASE_2, '"r2"')
        second = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        assert second.status == "fetched"
        load.load_source(conn, spec, tmp_path)

        (change,) = diff.diff_source(conn, spec).steps
        assert (change.added, change.changed, change.removed) == (1, 1, 1)
        assert change.changed_fields == {
            "EinheitBetriebsstatus": 1,
            "Laengengrad": 1,
            "geometry": 1,
        }

        (step2,) = apply.apply_source(conn, spec).steps
        assert (step2.opened, step2.closed, step2.current) == (2, 2, 4)
        assert step2.kinds == {"Endgültig stillgelegt": 1, "In Betrieb": 2, "In Planung": 1}
        assert step2.flags["capacity_implausible"] == 1

        now = dict(
            conn.execute(
                "SELECT unit_key, status FROM staging.wind_unit_current"
                " WHERE source_id = %s ORDER BY unit_key",
                (spec.source_id,),
            ).fetchall()
        )
        assert now == {
            "SEE1": "Endgültig stillgelegt",
            "SEE3": "In Betrieb",
            "SEE4": "In Planung",
            "SEE5": "In Betrieb",
        }
        then = dict(
            conn.execute(
                "SELECT unit_key, status FROM staging.wind_unit_as_of(%s, %s) ORDER BY unit_key",
                (spec.source_id, first.version_id),
            ).fetchall()
        )
        assert then == {
            "SEE1": "In Betrieb",
            "SEE2": "In Planung",
            "SEE3": "In Betrieb",
            "SEE4": "In Planung",
        }


def test_a_source_without_an_applier_is_refused() -> None:
    spec = dataclasses.replace(make_spec(), load_themes=())
    with db.connect("app") as conn, pytest.raises(ApplyError, match="no applier"):
        apply.apply_source(conn, spec)
