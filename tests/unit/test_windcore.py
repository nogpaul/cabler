"""The core wind units: the classification of one unit, the rules, the hash and the build flow.

The database is replaced by a small fake that answers each query of the build by its text, so these
tests need no database; tests/integration/test_windcore_db.py runs the same build against real data.
"""

import json
from contextlib import contextmanager
from typing import Any

import pytest

from cabler import core, windcore
from cabler.core import CoreError
from cabler.windcore import UnitClass, WindRules, classify_unit

RULES = WindRules(version="0.0.1", states={"osm_nrw": "05"})


def classify(status: str = "In Betrieb", **kwargs: Any) -> UnitClass:
    args: dict[str, Any] = {
        "has_position": True,
        "flagged": False,
        "osm_distance_m": None,
        "registered_distance_m": None,
        "farm_gap_m": None,
        "rules": RULES,
    }
    args.update(kwargs)
    return classify_unit(status, **args)


def scenarios(unit: UnitClass) -> tuple[bool, bool, bool]:
    return unit.in_strict, unit.in_likely, unit.in_inclusive


def test_an_existing_unit_with_nothing_against_its_position_is_in_every_scenario() -> None:
    unit = classify()
    assert (unit.status_group, unit.position_class, unit.reason) == (
        "existing",
        "plausible",
        "register_only",
    )
    assert scenarios(unit) == (True, True, True)


def test_a_temporarily_shut_down_unit_counts_as_existing() -> None:
    assert scenarios(classify("Vorübergehend stillgelegt")) == (True, True, True)


def test_a_planned_unit_is_in_likely_and_inclusive_only() -> None:
    unit = classify("In Planung")
    assert unit.status_group == "planned"
    assert scenarios(unit) == (False, True, True)


@pytest.mark.parametrize("status", ["Endgültig stillgelegt", None, "Something new"])
def test_a_retired_or_unknown_status_is_in_no_scenario(status: str | None) -> None:
    unit = classify(status, osm_distance_m=3.0)
    assert (unit.status_group, unit.reason) == ("retired", "retired")
    assert unit.position_class == "confirmed"  # the position is judged all the same
    assert scenarios(unit) == (False, False, False)


def test_an_osm_turbine_within_the_confirm_distance_confirms_the_position() -> None:
    unit = classify(osm_distance_m=25.0, registered_distance_m=9000.0, farm_gap_m=9000.0)
    assert (unit.position_class, unit.reason) == ("confirmed", "osm_turbine")
    assert scenarios(unit) == (True, True, True)


def test_an_osm_turbine_only_near_is_evidence_not_confirmation() -> None:
    unit = classify(osm_distance_m=25.1)
    assert unit.position_class == "plausible"


@pytest.mark.parametrize(
    ("registered", "gap", "reason"),
    [
        (2000.1, None, "far_from_municipality"),
        (0.0, 5000.1, "isolated_in_farm"),
        (2500.0, 9000.0, "far_from_municipality_and_isolated_in_farm"),
    ],
)
def test_a_doubtful_position_is_only_in_inclusive(
    registered: float, gap: float | None, reason: str
) -> None:
    unit = classify(registered_distance_m=registered, farm_gap_m=gap)
    assert (unit.position_class, unit.reason) == ("doubtful", reason)
    assert scenarios(unit) == (False, False, True)


def test_the_limits_themselves_are_still_plausible() -> None:
    unit = classify(registered_distance_m=2000.0, farm_gap_m=5000.0)
    assert unit.position_class == "plausible"


def test_a_planned_unit_with_a_doubtful_position_is_in_inclusive_only() -> None:
    assert scenarios(classify("In Planung", registered_distance_m=7000.0)) == (False, False, True)


def test_a_flagged_coordinate_is_invalid_and_in_no_scenario_even_next_to_a_turbine() -> None:
    unit = classify(flagged=True, osm_distance_m=1.0)
    assert (unit.position_class, unit.reason) == ("invalid", "coordinate_flagged")
    assert scenarios(unit) == (False, False, False)


def test_a_unit_without_a_coordinate_is_unlocated_and_in_no_scenario() -> None:
    unit = classify(has_position=False, flagged=True)
    assert (unit.position_class, unit.reason) == ("unlocated", "no_coordinate")
    assert scenarios(unit) == (False, False, False)


def test_the_rules_refuse_a_confirm_distance_larger_than_the_match_distance() -> None:
    with pytest.raises(CoreError, match="confirm_m"):
        WindRules(version="0.0.1", confirm_m=200, match_m=100)
    with pytest.raises(CoreError, match="positive"):
        WindRules(version="0.0.1", farm_gap_m=0)


def test_the_parameters_are_a_stable_description_of_the_rules() -> None:
    first = WindRules(version="1.0.0", states={"b": "02", "a": "05"}).parameters()
    second = WindRules(version="1.0.0", states={"a": "05", "b": "02"}).parameters()
    assert json.dumps(first) == json.dumps(second)
    assert first["existing_statuses"] == ["In Betrieb", "Vorübergehend stillgelegt"]
    assert first["confirm_m"] == 25


def row(key: str, **changes: Any) -> windcore.Row:
    values: dict[str, Any] = {
        "unit_id": 1,
        "source_id": "mastr_bulk",
        "unit_key": key,
        "state_key": "05",
        "status": "In Betrieb",
        "name": None,
        "wind_farm": None,
        "grid_location_key": None,
        "municipality_key": "05111000",
        "municipality": "Duesseldorf",
        "place_key": "05111000",
        "gross_kw": 3000.0,
        "hub_height_m": 120.0,
        "rotor_diameter_m": 100.0,
        "commissioned": "2015-02-04",
        "qa_flags": [],
        "osm_source_id": None,
        "osm_key": None,
        "osm_distance_m": None,
        "registered_distance_m": 0.0,
        "farm_gap_m": None,
        "classification": classify(),
        "geom_ewkb": b"\x01",
        "geom_wkt": "POINT(1 2)",
    }
    values.update(changes)
    return windcore.Row(**values)


def test_the_hash_does_not_depend_on_the_order_or_on_generated_numbers() -> None:
    a, b = row("SEE1", unit_id=10), row("SEE2", unit_id=11)
    renumbered = [row("SEE1", unit_id=99), row("SEE2", unit_id=98)]
    assert windcore.content_hash([a, b]) == windcore.content_hash([b, a])
    assert windcore.content_hash([a, b]) == windcore.content_hash(renumbered)


def test_the_hash_changes_with_the_quality_of_a_position() -> None:
    doubtful = classify(registered_distance_m=9000.0)
    plain = windcore.content_hash([row("SEE1")])
    assert windcore.content_hash([row("SEE1", classification=doubtful)]) != plain
    assert (
        windcore.content_hash([row("SEE1", osm_key="n1", osm_source_id="s", osm_distance_m=3.0)])
        != plain
    )


def test_the_counts_add_up() -> None:
    rows = [
        row("a"),
        row("b", classification=classify("In Planung"), osm_key="n1", place_key=None),
        row("c", classification=classify(registered_distance_m=9000.0)),
        row("d", classification=classify("Endgültig stillgelegt")),
        row("e", classification=classify(has_position=False)),
    ]
    counts = windcore._counts(rows, (10, 3))
    assert counts["units"] == 5
    assert (counts["strict"], counts["likely"], counts["inclusive"]) == (1, 2, 3)
    assert (counts["status_existing"], counts["status_planned"], counts["status_retired"]) == (
        3,
        1,
        1,
    )
    assert counts["class_plausible"] == 3
    assert counts["class_doubtful"] == 1
    assert counts["class_unlocated"] == 1
    assert counts["with_osm_turbine"] == 1
    assert counts["with_place"] == 4
    assert (counts["osm_turbines"], counts["osm_turbines_without_unit"]) == (10, 3)


class Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Cursor:
    def __init__(self, owner: "Database") -> None:
        self.owner = owner

    def executemany(self, query: str, rows: list[tuple[Any, ...]]) -> None:
        assert query == windcore.INSERT_SQL
        self.owner.inserted = rows


class Database:
    """Answers the queries of a build from canned rows."""

    def __init__(
        self, facts: list[tuple[Any, ...]], verdict: tuple[int, str] | None = (7, "passed")
    ):
        self.facts = facts
        self.verdict = verdict
        self.seen: dict[str, Any] = {}
        self.inserted: list[tuple[Any, ...]] = []

    @contextmanager
    def transaction(self) -> Any:
        yield

    @contextmanager
    def cursor(self) -> Any:
        yield Cursor(self)

    def execute(self, query: str, params: Any = None) -> Result:
        if query == core.VERDICT_SQL:
            return Result([self.verdict] if self.verdict else [])
        if query == core.ADMIN_VERSION_SQL:
            return Result([(5,)])
        if query == core.WIND_VERSION_SQL:
            return Result([(11,)])
        if query == windcore.FACTS_SQL:
            self.seen["facts"] = params
            return Result(self.facts)
        if query == windcore.TURBINE_SQL:
            return Result([(40, 4)])
        if query == windcore.BUILD_SQL:
            self.seen["build"] = params
            return Result([(3,)])
        raise AssertionError(query)


def fact(key: str, status: str, wkt: str | None, **given: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "geom": b"\x01" if wkt else None,
        "flags": [],
        "osm": (None, None, None),
        "found": 1,
        "registered": 0.0,
        "gap": None,
    }
    values.update(given)
    return (
        1, "mastr_bulk", key, "05", status, None, None, None, "05111000", "Duesseldorf",
        3000, 120, 100, "2015-02-04", values["flags"],
        values["geom"], wkt,
        *values["osm"], "05111000", values["registered"], values["found"], values["gap"],
    )  # fmt: skip


def test_a_build_classifies_every_fact_and_records_rules_inputs_and_hash() -> None:
    db = Database(
        [
            fact("SEE1", "In Betrieb", "POINT(1 2)", osm=("osm_nrw", "n9", 4.0), registered=9000.0),
            fact("SEE2", "In Planung", "POINT(3 4)", registered=2500.5),
            fact("SEE3", "In Betrieb", None, flags=["coordinate_missing"]),
            fact("SEE4", "In Betrieb", "POINT(5 6)", registered=9000.0, found=0),
        ]
    )
    result = windcore.build_wind_units(db, RULES)  # type: ignore[arg-type]
    assert result.build_id == 3
    assert result.source_versions == {"osm_nrw": 7, "bkg_vg250": 5, "wind_units": 11}
    assert result.counts["units"] == 4
    assert (
        result.counts["class_confirmed"] == 1
    )  # the turbine beats the distance to the municipality
    assert result.counts["class_doubtful"] == 1
    assert result.counts["class_unlocated"] == 1
    # a key that the boundaries lack proves nothing: SEE4 has no distance and stays plausible
    assert result.counts["class_plausible"] == 1
    assert (result.counts["strict"], result.counts["likely"], result.counts["inclusive"]) == (
        2,
        2,
        3,
    )
    assert db.seen["facts"]["osm_sources"] == ["osm_nrw"]
    assert db.seen["facts"]["osm_versions"] == [7]
    assert db.seen["facts"]["states"] == ["05"]
    assert db.seen["facts"]["admin_version"] == 5
    version, parameters, versions, counts, digest = db.seen["build"]
    assert version == "0.0.1"
    assert json.loads(parameters)["confirm_m"] == 25
    assert json.loads(versions)["wind_units"] == 11
    assert json.loads(counts)["osm_turbines_without_unit"] == 4
    assert digest == result.content_hash
    assert [values[2] for values in db.inserted] == ["SEE1", "SEE2", "SEE3", "SEE4"]
    assert db.inserted[0][0] == 3  # every row belongs to the build


def test_a_build_without_a_validated_snapshot_is_refused() -> None:
    with pytest.raises(CoreError, match="no validated snapshot"):
        windcore.build_wind_units(Database([], verdict=None), RULES)  # type: ignore[arg-type]


def test_a_build_without_any_source_in_the_rules_is_refused() -> None:
    empty = WindRules(version="0.0.1")
    with pytest.raises(CoreError, match="no source"):
        windcore.build_wind_units(Database([]), empty)  # type: ignore[arg-type]


def test_the_report_shows_scenarios_positions_and_turbines() -> None:
    result = windcore.WindBuildResult(
        3,
        "ab" * 32,
        {
            "units": 100,
            "strict": 60,
            "likely": 70,
            "inclusive": 80,
            "with_osm_turbine": 50,
            "with_place": 90,
            "osm_turbines": 55,
            "osm_turbines_without_unit": 5,
            "class_confirmed": 50,
            "class_plausible": 20,
            "class_doubtful": 10,
            "class_invalid": 2,
            "class_unlocated": 18,
            "status_existing": 60,
            "status_planned": 10,
            "status_retired": 30,
        },
        {"osm_nrw": 7, "wind_units": 11},
    )
    text = "\n".join(windcore.format_report(result))
    assert "wind units build 3  hash abababababababab" in text
    assert "units 100: existing 60, planned 10, retired 30" in text
    assert "strict 60, likely 70, inclusive 80" in text
    assert "confirmed 50, plausible 20, doubtful 10, invalid 2, unlocated 18" in text
    assert "OSM turbines 55, without a unit within reach 5; units with an OSM turbine 50" in text
