"""The apply stage and the staging functions against a real database.

The simulated publisher first serves power_mini.osm, then power_mini_v2.osm (n1 re-tagged, n2
deleted, n20 added, w10 reshaped), so the staging history can be checked row by row.

Run with:  CABLER_ENV=dev pytest -m integration   (after `python -m cabler.migrate up dev`)
"""

import dataclasses
import os
from pathlib import Path

import httpx
import pytest

from cabler import apply, db, diff, fetch, load, remote, sources
from cabler.apply import ApplyError

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


def test_the_first_snapshot_fills_staging_and_the_next_one_keeps_history(tmp_path: Path) -> None:
    spec = make_spec()
    publisher = Publisher()
    with db.connect("app") as conn, conn.transaction(force_rollback=True):
        sources.register(conn, spec)
        first = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)
        diff.diff_source(conn, spec)

        baseline = apply.apply_source(conn, spec)
        assert baseline.status == "applied"
        assert baseline.version_id == first.version_id
        (base,) = baseline.steps
        assert (base.opened, base.closed, base.current) == (4, 0, 4)
        assert base.kinds == {"line": 1, "substation": 2, "wind_turbine": 1}
        assert base.flags == {}

        current = conn.execute(
            "SELECT osm_key, kind, voltage_v, voltage_class, output_w, ST_SRID(geom),"
            " ST_SRID(location), GeometryType(location)"
            " FROM staging.osm_power_current WHERE source_id = %s ORDER BY osm_key",
            (spec.source_id,),
        ).fetchall()
        assert current == [
            ("n1", "substation", 110000, "high", None, 25832, 25832, "POINT"),
            ("n2", "wind_turbine", None, None, 3000000, 25832, 25832, "POINT"),
            ("w10", "line", 110000, "high", None, 25832, 25832, "POINT"),
            ("w12", "substation", 380000, "extra_high", None, 25832, 25832, "POINT"),
        ]

        assert apply.apply_source(conn, spec).status == "skipped"

        publisher.release("power_mini_v2.osm", '"r2"')
        second = fetch.fetch_source(conn, publisher.client, spec, tmp_path)
        load.load_source(conn, spec, tmp_path)
        diff.diff_source(conn, spec)
        result = apply.apply_source(conn, spec)
        (step,) = result.steps
        assert step.version_id == second.version_id
        assert (step.opened, step.closed, step.current) == (3, 3, 4)

        history = conn.execute(
            "SELECT osm_key, valid_from_version_id = %(first)s,"
            " coalesce(valid_to_version_id = %(second)s, false),"
            " valid_to_version_id IS NULL, voltage_v"
            " FROM staging.osm_power_feature WHERE source_id = %(source)s"
            " ORDER BY osm_key, feature_id",
            {"first": first.version_id, "second": second.version_id, "source": spec.source_id},
        ).fetchall()
        assert history == [
            ("n1", True, True, False, 110000),
            ("n1", False, False, True, 220000),
            ("n2", True, True, False, None),
            ("n20", False, False, True, 110000),
            ("w10", True, True, False, 110000),
            ("w10", False, False, True, 110000),
            ("w12", True, False, True, 380000),
        ]

        as_of_first = conn.execute(
            "SELECT osm_key, voltage_v FROM staging.osm_power_as_of(%s, %s) ORDER BY osm_key",
            (spec.source_id, first.version_id),
        ).fetchall()
        assert as_of_first == [("n1", 110000), ("n2", None), ("w10", 110000), ("w12", 380000)]
        as_of_second = conn.execute(
            "SELECT osm_key, voltage_v FROM staging.osm_power_as_of(%s, %s) ORDER BY osm_key",
            (spec.source_id, second.version_id),
        ).fetchall()
        assert as_of_second == [("n1", 220000), ("n20", 110000), ("w10", 110000), ("w12", 380000)]

        statuses = conn.execute(
            "SELECT status FROM meta.source_version WHERE source_id = %s ORDER BY version_id",
            (spec.source_id,),
        ).fetchall()
        assert statuses == [("applied",), ("applied",)]
        assert load.load_source(conn, spec, tmp_path).status == "skipped"
        assert diff.diff_source(conn, spec).status == "skipped"


def test_a_source_without_the_power_theme_cannot_be_applied() -> None:
    spec = dataclasses.replace(make_spec(), load_themes=("water",))
    with (
        db.connect("app") as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(ApplyError, match="power theme"),
    ):
        apply.apply_source(conn, spec)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("110000", 110000),
        ("380000;110000", 380000),
        ("110000;medium", 110000),
        ("10000;400", 10000),
        (" 110000 ; 220000 ", 220000),
        ("400", 400),
        ("medium", None),
        ("110 kV", None),
        ("", None),
        (None, None),
    ],
)
def test_voltage_is_the_highest_plain_number_in_the_tag(
    raw: str | None, expected: int | None
) -> None:
    with db.connect("app") as conn:
        row = conn.execute("SELECT staging.parse_voltage_v(%s)", (raw,)).fetchone()
    assert row == (expected,)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3 MW", 3_000_000),
        ("2.4 MW", 2_400_000),
        ("3,3 MW", 3_300_000),
        ("600 kW", 600_000),
        ("2 mw", 2_000_000),
        ("500", 500),
        ("1 GW", 1_000_000_000),
        ("yes", None),
        ("3 MW;1 MW", None),
        ("", None),
        (None, None),
    ],
)
def test_output_is_converted_to_watts(raw: str | None, expected: int | None) -> None:
    with db.connect("app") as conn:
        row = conn.execute("SELECT staging.parse_output_w(%s)", (raw,)).fetchone()
    assert row == (expected,)


@pytest.mark.parametrize(
    ("power", "source", "method", "expected"),
    [
        ("substation", None, None, "substation"),
        ("line", None, None, "line"),
        ("cable", None, None, "cable"),
        ("minor_line", None, None, "minor_line"),
        ("generator", "wind", None, "wind_turbine"),
        ("generator", None, "wind_turbine", "wind_turbine"),
        ("generator", "solar", "photovoltaic", "generator"),
        ("tower", None, None, "support"),
        ("catenary_mast", None, None, "support"),
        ("transformer", None, None, "other"),
    ],
)
def test_power_objects_are_classified(
    power: str, source: str | None, method: str | None, expected: str
) -> None:
    with db.connect("app") as conn:
        row = conn.execute(
            "SELECT staging.power_kind(%s, %s, %s)", (power, source, method)
        ).fetchone()
    assert row == (expected,)


@pytest.mark.parametrize(
    ("kind", "volts", "expected"),
    [
        ("substation", 380000, "extra_high"),
        ("line", 220000, "extra_high"),
        ("substation", 110000, "high"),
        ("cable", 20000, "medium_low"),
        ("substation", None, "unknown"),
        ("wind_turbine", 110000, None),
        ("support", None, None),
    ],
)
def test_voltage_classes(kind: str, volts: int | None, expected: str | None) -> None:
    with db.connect("app") as conn:
        row = conn.execute("SELECT staging.voltage_class(%s, %s)", (kind, volts)).fetchone()
    assert row == (expected,)


def test_a_tag_is_read_from_the_hstore_text() -> None:
    tags = '"substation"=>"transmission","operator:type"=>"public"'
    with db.connect("app") as conn:
        found = conn.execute("SELECT staging.osm_tag(%s, 'substation')", (tags,)).fetchone()
        missing = conn.execute("SELECT staging.osm_tag(%s, 'other')", (tags,)).fetchone()
        nothing = conn.execute("SELECT staging.osm_tag(NULL, 'other')").fetchone()
    assert found == ("transmission",)
    assert missing == (None,)
    assert nothing == (None,)


def test_the_staging_table_is_readable_by_the_read_only_role() -> None:
    with db.connect("ro") as conn:
        assert conn.execute("SELECT count(*) FROM staging.osm_power_feature").fetchone() is not None
