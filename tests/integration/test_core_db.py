"""The core substations against a real database: scenarios, tiers, decisions and the rebuild.

The data is tests/fixtures/power_hints.osm (a 110 kV line and six substations) and four wind units
placed by hand: one near the first two substations, one at the tagged 110 kV substation, and two
near the third that must be ignored (decommissioned, and a placeholder coordinate). With a reach of
3 km the expected tiers are written out in full below.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import io
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psycopg
import pyogrio.raw
import pytest

from cabler import apply, core, db, diff, evaluate, fetch, load, places, remote, sources, validate
from cabler.core import CoreError, RuleSet
from cabler.evaluate import Cell
from cabler.places import SubstationPlaces, WindAgreement

pytestmark = pytest.mark.integration

if "CABLER_ENV" not in os.environ:
    pytest.skip("CABLER_ENV not set", allow_module_level=True)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
# The boundaries come from a source that only the place test creates, so that no other test sees
# the real municipalities that may be loaded into the database.
RULES = RuleSet(
    version="0.0.1",
    states={"itest_source": "99"},
    reach_m=3000,
    audit_min=1,
    audit_share=0.5,
    admin_source="itest_admin",
)


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


SUBSTATIONS_SQL = """
SELECT osm_key, in_strict, in_likely, in_inclusive, reason, tier, wind_units_in_reach, impact
FROM core.substation_current ORDER BY osm_key
"""


def test_a_build_puts_every_substation_into_its_scenarios_and_tier(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        prepared(conn, tmp_path)
        result = core.build_substations(conn, RULES)
        assert result.counts == {
            "substations": 6,
            "strict": 1,
            "likely": 3,
            "inclusive": 6,
            "audit_sample": 3,
            "with_municipality": 0,
            "tier_A": 3,
            "tier_B": 0,
            "tier_C": 1,
            "tier_D": 1,
        }
        assert conn.execute(SUBSTATIONS_SQL).fetchall() == [
            ("n101", False, True, True, "hint_candidate", "A", 1, 1),
            ("n102", False, False, True, "hint_conflict", "A", 1, 1),
            # in reach of w3, but n106 is nearer to w3, so n103 cannot change any route
            ("n103", False, False, True, "hint_low", "D", 1, 0),
            ("n104", False, True, True, "hint_candidate", "A", 2, 1),
            ("n105", False, False, True, "unknown", "C", 2, 1),
            ("n106", True, True, True, "tag_high", None, 2, 0),
        ]
        audited = conn.execute(
            "SELECT osm_key FROM core.substation_current WHERE audit_sample ORDER BY osm_key"
        ).fetchall()
        assert ("n102",) in audited  # the only one of its stratum
        assert ("n103",) in audited  # no impact, but in reach: still audited
        assert len(audited) == 3


def test_the_same_inputs_give_the_same_hash_and_the_build_is_recorded(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        _, version = prepared(conn, tmp_path)
        first = core.build_substations(conn, RULES)
        second = core.build_substations(conn, RULES)
        assert first.content_hash == second.content_hash
        assert second.build_id > first.build_id
        row = conn.execute(
            "SELECT source_versions, decisions_through, counts ->> 'substations'"
            " FROM core.build WHERE build_id = %s",
            (second.build_id,),
        ).fetchone()
        # no boundaries were loaded: the build says so with version 0 and gives no place
        assert row == ({"itest_source": version, "itest_admin": 0, "wind_units": version}, 0, "6")
        assert conn.execute(
            "SELECT count(*) FROM core.substation"
            " WHERE build_id = %s AND municipality_key IS NOT NULL",
            (second.build_id,),
        ).fetchone() == (0,)
        ours = [first.build_id, second.build_id]
        assert conn.execute(
            "SELECT count(DISTINCT build_id) FROM core.substation WHERE build_id = ANY(%s)", (ours,)
        ).fetchone() == (2,)


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


def location_of(conn: psycopg.Connection[Any], osm_key: str) -> tuple[float, float]:
    row = conn.execute(
        "SELECT ST_X(location), ST_Y(location) FROM staging.osm_power_current"
        " WHERE source_id = 'itest_source' AND osm_key = %s",
        (osm_key,),
    ).fetchone()
    assert row is not None
    return float(row[0]), float(row[1])


def test_every_substation_gets_the_place_that_covers_it(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        prepared(conn, tmp_path)
        # n105 lies exactly on the edge that Gamma and Delta share; n106 lies in the land part and
        # in a water part of the same municipality; n104 lies in no municipality at all.
        x5, y5 = location_of(conn, "n105")
        x6, y6 = location_of(conn, "n106")
        around_n106 = utm_box(conn, x6 - 500, y6 - 500, x6 + 500, y6 + 500)
        municipalities = [
            (box(conn, 9.95, 53.50, 10.05, 53.58), "02000001", "Alpha", "OBJ-a", 4),
            (box(conn, 10.05, 53.58, 10.105, 53.605), "02000002", "Beta", "OBJ-b", 4),
            (utm_box(conn, x5 - 900, y5 - 900, x5, y5 + 900), "02000004", "Gamma", "OBJ-g", 4),
            (utm_box(conn, x5, y5 - 900, x5 + 900, y5 + 900), "02000003", "Delta", "OBJ-d", 4),
            # the water part is listed first and is the one with the smaller identifier
            (around_n106, "02000005", "EpsilonWater", "OBJ-0", 2),
            (around_n106, "02000005", "Epsilon", "OBJ-land", 4),
        ]
        districts = [(box(conn, 9.9, 53.4, 10.3, 53.8), "02000", "Kreis Eins")]
        admin = dataclasses.replace(sources.load_all()["bkg_vg250"], source_id="itest_admin")
        sources.register(conn, admin)
        publisher = BodyPublisher(boundaries_zip(municipalities, districts))
        fetch.fetch_source(conn, publisher.client, admin, tmp_path)
        load.load_source(conn, admin, tmp_path)
        diff.diff_source(conn, admin)
        (step,) = apply.apply_source(conn, admin).steps
        assert step.kinds == {"district": 1, "municipality": 6}

        result = core.build_substations(conn, RULES)
        assert result.counts["with_municipality"] == 5
        assert result.source_versions["itest_admin"] == step.version_id
        placed = conn.execute(
            "SELECT osm_key, municipality_key, municipality, district"
            " FROM core.substation_current ORDER BY osm_key"
        ).fetchall()
        assert placed == [
            ("n101", "02000001", "Alpha", "Kreis Eins"),
            ("n102", "02000001", "Alpha", "Kreis Eins"),
            ("n103", "02000002", "Beta", "Kreis Eins"),
            ("n104", None, None, None),
            ("n105", "02000003", "Delta", "Kreis Eins"),  # on the border: the lower key wins
            ("n106", "02000005", "Epsilon", "Kreis Eins"),  # land comes before water
        ]
        # the place is part of the content, so the same inputs give the same hash again
        assert core.build_substations(conn, RULES).content_hash == result.content_hash
        queue = conn.execute(
            "SELECT osm_key FROM core.review_queue WHERE municipality = 'Alpha' ORDER BY osm_key"
        ).fetchall()
        assert queue == [("n101",), ("n102",)]

        # The register names a municipality for each unit: w1 agrees with its coordinate, w2 names
        # Beta, about 3 km away, and w3 names a key that the boundaries do not have.
        for unit, key in (("w1", "02000001"), ("w2", "02000002"), ("w3", "02000009")):
            conn.execute(
                "UPDATE staging.wind_unit SET municipality_key = %s"
                " WHERE source_id = 'itest_source' AND unit_key = %s",
                (key, unit),
            )
        measured = places.check_places(conn, RULES)
        assert measured.municipalities == 6
        assert measured.substations == [SubstationPlaces("itest_source", 6, 5, 5)]
        # the state of the test source is 99, so every place found counts as "other state"
        assert measured.wind == [WindAgreement("99", 3, 3, 1, 1, (0, 0, 0, 1, 0))]


def test_the_queue_lists_the_decision_relevant_cases_first(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        prepared(conn, tmp_path)
        core.build_substations(conn, RULES)
        keys = [row[2] for row in core.queue_rows(conn, None, 10)]
        # the audit sample is in the queue too, after the cases that matter
        assert keys == ["n101", "n102", "n104", "n105", "n103"]
        assert [row[2] for row in core.queue_rows(conn, "A", 10)] == ["n101", "n102", "n104"]
        assert [row[2] for row in core.queue_rows(conn, None, 2)] == ["n101", "n102"]


def test_a_decision_changes_the_next_build_and_leaves_the_queue(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        core.build_substations(conn, RULES)
        first = core.record_decision(
            conn,
            spec.source_id,
            "points",
            "n101",
            "not_high_voltage",
            reason="a small station next to the line",
            reviewer="reviewer-1",
            rules=RULES,
        )
        left = ["n102", "n104", "n105", "n103"]
        assert [row[2] for row in core.queue_rows(conn, None, 10)] == left
        second = core.record_decision(
            conn,
            spec.source_id,
            "points",
            "n101",
            "high_voltage",
            reason="named 110 kV by the operator",
            reviewer="reviewer-2",
            rules=RULES,
            evidence={"url": "https://example.org/map"},
        )
        chain = conn.execute(
            "SELECT decision_id, supersedes_decision_id, decision FROM core.review_decision"
            " WHERE source_id = %s ORDER BY decision_id",
            (spec.source_id,),
        ).fetchall()
        assert chain == [(first, None, "not_high_voltage"), (second, first, "high_voltage")]

        result = core.build_substations(conn, RULES)
        assert result.counts["strict"] == 2
        rows = {r[0]: r for r in conn.execute(SUBSTATIONS_SQL).fetchall()}
        # it is now its own wind unit's nearest strict substation, so its impact is 0
        assert rows["n101"] == ("n101", True, True, True, "decision_high", None, 1, 0)
        # with n101 high voltage, n102 can no longer change the nearest substation of w1
        assert rows["n102"][5:] == ("D", 1, 0)
        decided = conn.execute(
            "SELECT decision, decision_id FROM core.substation_current WHERE osm_key = 'n101'"
        ).fetchone()
        assert decided == ("high_voltage", second)
        through = conn.execute(
            "SELECT decisions_through FROM core.build WHERE build_id = %s", (result.build_id,)
        ).fetchone()
        assert through == (second,)


def test_a_decision_does_not_move_the_audit_sample(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        audited = "SELECT osm_key FROM core.substation_current WHERE audit_sample ORDER BY osm_key"
        core.build_substations(conn, RULES)
        before = conn.execute(audited).fetchall()
        assert ("n102",) in before
        core.record_decision(
            conn,
            spec.source_id,
            "points",
            "n102",
            "not_high_voltage",
            reason="a small station",
            reviewer="p",
            rules=RULES,
        )
        core.build_substations(conn, RULES)
        assert conn.execute(audited).fetchall() == before


def test_the_wind_unit_version_changes_when_units_are_retired(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        publisher = Publisher()
        spec, version = prepared(conn, tmp_path, publisher)
        assert conn.execute(core.WIND_VERSION_SQL, (["99"],)).fetchone() == (version,)
        publisher.release("power_hints_cut.osm", '"r2"')
        later = ingest(conn, publisher, spec, tmp_path)
        conn.execute(
            "UPDATE staging.wind_unit SET valid_to_version_id = %s"
            " WHERE source_id = %s AND unit_key = 'w1'",
            (later, spec.source_id),
        )
        assert conn.execute(core.WIND_VERSION_SQL, (["99"],)).fetchone() == (later,)


def test_a_decision_about_something_else_or_without_a_reason_is_refused(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        args = (conn, spec.source_id, "points")
        with pytest.raises(CoreError, match="not a current substation"):
            core.record_decision(
                *args, "n999", "high_voltage", reason="r", reviewer="p", rules=RULES
            )
        with pytest.raises(CoreError, match="decision must be one of"):
            core.record_decision(*args, "n101", "maybe", reason="r", reviewer="p", rules=RULES)
        with pytest.raises(CoreError, match="needs a reason"):
            core.record_decision(
                *args, "n101", "high_voltage", reason=" ", reviewer="p", rules=RULES
            )


def test_the_decisions_cannot_be_changed_or_removed_by_the_application(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        core.record_decision(
            conn,
            spec.source_id,
            "points",
            "n101",
            "high_voltage",
            reason="r",
            reviewer="p",
            rules=RULES,
        )
        for statement in (
            "UPDATE core.review_decision SET reason = 'other'",
            "DELETE FROM core.review_decision",
            "TRUNCATE core.review_decision",
            "UPDATE core.rule_set SET parameters = '{}'",
            "DELETE FROM core.build",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(statement)  # type: ignore[arg-type]


def test_the_owner_is_refused_too_by_a_trigger() -> None:
    with db.connect("owner") as conn, conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO meta.source"
            " (source_id, title, publisher, licence_id, licence_url, attribution)"
            " VALUES ('itest_owner', 't', 'p', 'l', 'https://example.org', 'a')"
        )
        conn.execute("INSERT INTO core.rule_set (version, parameters) VALUES ('0.0.2', '{}')")
        conn.execute(
            "INSERT INTO core.review_decision"
            " (source_id, layer, osm_key, decision, reason, reviewer, rule_set_id)"
            " SELECT 'itest_owner', 'points', 'n1', 'high_voltage', 'r', 'p', rule_set_id"
            " FROM core.rule_set WHERE version = '0.0.2'"
        )
        for statement in (
            "UPDATE core.review_decision SET reason = 'other'",
            "DELETE FROM core.review_decision",
            "TRUNCATE core.review_decision CASCADE",  # plain TRUNCATE is stopped by the foreign key
            "UPDATE core.rule_set SET parameters = '{\"a\": 1}'",
            "DELETE FROM core.rule_set",
        ):
            with (
                pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
                conn.transaction(),
            ):
                conn.execute(statement)  # type: ignore[arg-type]


def test_a_decision_must_replace_the_newest_one(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        rule_set_id = core.register_rules(conn, RULES)
        insert = (
            "INSERT INTO core.review_decision (source_id, layer, osm_key, decision, reason,"
            " reviewer, rule_set_id, supersedes_decision_id) VALUES (%s, 'points', 'n101',"
            " 'high_voltage', 'r', 'p', %s, %s) RETURNING decision_id"
        )

        def refused() -> Any:
            return pytest.raises(psycopg.errors.CheckViolation, match="must replace the newest")

        with refused(), conn.transaction():
            conn.execute(insert, (spec.source_id, rule_set_id, 12345))
        first = conn.execute(insert, (spec.source_id, rule_set_id, None)).fetchone()
        assert first is not None
        with refused(), conn.transaction():
            conn.execute(insert, (spec.source_id, rule_set_id, None))
        conn.execute(insert, (spec.source_id, rule_set_id, first[0]))


def test_a_decision_that_loses_the_race_is_refused_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        args = (conn, spec.source_id, "points", "n101")
        core.record_decision(*args, "high_voltage", reason="r", reviewer="p", rules=RULES)
        # what a second reviewer sees when the first one decides between the read and the insert
        stale = "SELECT NULL::bigint WHERE %s <> '' AND %s <> '' AND %s <> ''"
        monkeypatch.setattr(core, "NEWEST_SQL", stale)
        with pytest.raises(CoreError, match="recorded first"):
            core.record_decision(*args, "not_high_voltage", reason="r", reviewer="q", rules=RULES)


def test_a_rebuild_from_the_raw_data_gives_the_same_content_hash(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, version = prepared(conn, tmp_path)
        core.record_decision(
            conn,
            spec.source_id,
            "points",
            "n105",
            "not_a_substation",
            reason="a transformer kiosk",
            reviewer="p",
            rules=RULES,
        )
        first = core.build_substations(conn, RULES)
        old_ids = {
            r[0]
            for r in conn.execute(
                "SELECT feature_id FROM core.substation WHERE build_id = %s", (first.build_id,)
            ).fetchall()
        }

        # throw the staging state of the source away and rebuild it from the raw changes
        conn.execute("DELETE FROM core.substation WHERE source_id = %s", (spec.source_id,))
        conn.execute("DELETE FROM staging.validation_finding WHERE version_id = %s", (version,))
        conn.execute(
            "DELETE FROM staging.snapshot_validation WHERE source_id = %s", (spec.source_id,)
        )
        conn.execute(
            "DELETE FROM staging.osm_power_feature WHERE source_id = %s", (spec.source_id,)
        )
        conn.execute(
            "UPDATE meta.source_version SET status = 'diffed' WHERE version_id = %s", (version,)
        )
        assert apply.apply_source(conn, spec).status == "applied"
        assert validate.validate_source(conn, spec).status == "validated"

        second = core.build_substations(conn, RULES)
        new_ids = {
            r[0]
            for r in conn.execute(
                "SELECT feature_id FROM core.substation WHERE build_id = %s", (second.build_id,)
            ).fetchall()
        }
        assert old_ids.isdisjoint(new_ids)  # every database-generated number is new
        assert second.content_hash == first.content_hash
        assert second.counts == first.counts


def test_a_source_that_is_not_validated_or_was_quarantined_cannot_be_built(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec = make_spec()
        sources.register(conn, spec)
        with pytest.raises(CoreError, match="no validated snapshot"):
            core.build_substations(conn, RULES)
        publisher = Publisher()
        ingest(conn, publisher, spec, tmp_path)
        validate.validate_source(conn, spec)
        publisher.release("power_hints_cut.osm", '"r2"')
        ingest(conn, publisher, spec, tmp_path)
        assert validate.validate_source(conn, spec).quarantined
        with pytest.raises(CoreError, match="was quarantined"):
            core.build_substations(conn, RULES)


def test_a_rule_set_version_cannot_be_reused_with_other_parameters() -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        first = core.register_rules(conn, RULES)
        assert core.register_rules(conn, RULES) == first
        with pytest.raises(CoreError, match="exists with other parameters"):
            core.register_rules(conn, dataclasses.replace(RULES, reach_m=5000))
        with pytest.raises(CoreError, match="names no source"):
            core.build_substations(conn, RuleSet(version="0.0.3"))


def test_the_rules_are_measured_on_tagged_substations_with_their_tag_left_out(
    tmp_path: Path,
) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        publisher = Publisher()
        publisher.release("power_tagged.osm", '"r1"')
        spec = make_spec()
        sources.register(conn, spec)
        ingest(conn, publisher, spec, tmp_path)
        assert validate.validate_source(conn, spec).status == "validated"
        result = evaluate.evaluate_rules(conn, RULES)
        ours = [c for c in result.leave_out if c.source_id == spec.source_id]
        assert ours == [
            Cell(spec.source_id, "hv_candidate", 3, 2),  # t1, t2 high; t5 is 20 kV
            Cell(spec.source_id, "conflict", 1, 1),
            Cell(spec.source_id, "likely_low_voltage", 2, 0),
            Cell(spec.source_id, "no_evidence", 1, 1),
        ]


def test_the_evaluation_applies_the_same_rules_as_the_review_view(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, version = prepared(conn, tmp_path)
        by_rules = evaluate.hints(conn, spec.source_id, version)
        in_view = dict(
            conn.execute(
                "SELECT osm_key, suggestion FROM staging.substation_review WHERE source_id = %s",
                (spec.source_id,),
            ).fetchall()
        )
        assert len(in_view) == 5
        untagged = {key: hint for (_, key), (hint, tag) in by_rules.items() if tag is None}
        assert untagged == in_view
        assert by_rules[("points", "n106")] == ("no_evidence", 110_000)


def test_audit_labels_are_counted_per_hint(tmp_path: Path) -> None:
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        spec, _ = prepared(conn, tmp_path)
        core.build_substations(conn, RULES)
        drawn = dict(
            conn.execute(
                "SELECT hint, osm_key FROM core.substation_current"
                " WHERE audit_sample AND source_id = %s",
                (spec.source_id,),
            ).fetchall()
        )
        assert set(drawn) == {"hv_candidate", "conflict", "likely_low_voltage"}

        def cells() -> dict[str, Cell]:
            audit = evaluate.evaluate_rules(conn, RULES).audit
            return {c.suggestion: c for c in audit if c.source_id == spec.source_id}

        assert {k: (c.n, c.n_unlabelled) for k, c in cells().items()} == {
            "hv_candidate": (0, 1),
            "conflict": (0, 1),
            "likely_low_voltage": (0, 1),
        }
        decisions = (
            ("conflict", "high_voltage"),
            ("likely_low_voltage", "not_a_substation"),
            ("hv_candidate", "not_high_voltage"),
        )
        for hint, decision in decisions:
            core.record_decision(
                conn,
                spec.source_id,
                "points",
                drawn[hint],
                decision,
                reason="checked on the map",
                reviewer="p",
                rules=RULES,
            )
        after = cells()
        assert after["conflict"] == Cell(spec.source_id, "conflict", 1, 1)
        assert after["hv_candidate"] == Cell(spec.source_id, "hv_candidate", 1, 0)
        assert after["likely_low_voltage"] == Cell(
            spec.source_id, "likely_low_voltage", 0, 0, n_not_a_substation=1
        )


def test_the_core_views_are_readable_by_the_read_only_role() -> None:
    with db.connect("ro") as conn:
        for view in ("substation_current", "review_queue", "review_decision_current"):
            assert conn.execute(f"SELECT count(*) FROM core.{view}").fetchone() is not None  # noqa: S608
