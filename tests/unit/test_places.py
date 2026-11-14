"""The report and the flow of the place checks, without a database.

The SQL itself is tested against a database in tests/integration/test_core_db.py.
"""

from typing import Any

import pytest

from cabler import places
from cabler.core import CoreError, RuleSet
from cabler.places import Places, SubstationPlaces, WindAgreement


def sample() -> Places:
    return Places(
        11_094,
        [
            SubstationPlaces("osm_hamburg", 433, 433, 19),
            SubstationPlaces("osm_nrw", 19_659, 19_644, 19),
        ],
        [
            WindAgreement("02", 71, 71, 71, 0, (0, 0, 0, 0, 0)),
            WindAgreement("05", 6_636, 6_629, 5_377, 0, (57, 121, 237, 631, 213)),
        ],
    )


def test_substations_know_how_many_have_no_place() -> None:
    nrw = sample().substations[1]
    assert nrw.unplaced == 15


def test_a_wind_state_knows_its_share_and_has_none_without_units() -> None:
    assert sample().wind[1].share == pytest.approx(5_377 / 6_636)
    assert WindAgreement("99", 0, 0, 0, 0, (0, 0, 0, 0, 0)).share is None


def test_the_report_gives_counts_shares_and_the_distance_bands() -> None:
    lines = places.format_report(sample())
    text = "\n".join(lines)
    assert "places from 11,094 municipalities" in text
    nrw = next(line for line in lines if line.strip().startswith("osm_nrw"))
    assert nrw.split() == ["osm_nrw", "19,659", "19,644", "15", "19"]
    wind = next(line for line in lines if line.strip().startswith("05"))
    assert wind.split()[:4] == ["05", "6,636", "5,377", "81.0%"]
    assert wind.split()[4:9] == ["57", "121", "237", "631", "213"]
    for band in ("<= 100 m", "<= 500 m", "<= 2 km", "<= 10 km", "> 10 km"):
        assert band in text
    assert "generalised" in text


def test_an_empty_state_shows_a_dash_not_a_made_up_share() -> None:
    result = Places(5, [], [WindAgreement("99", 0, 0, 0, 0, (0, 0, 0, 0, 0))])
    line = next(x for x in places.format_report(result) if x.strip().startswith("99"))
    assert line.split()[:4] == ["99", "0", "0", "-"]


class Rows:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class Database:
    """Answers the three queries of the check from memory."""

    def __init__(self, municipalities: int = 11_094) -> None:
        self.municipalities = municipalities
        self.seen: list[Any] = []

    def execute(self, query: str, params: Any = None) -> Rows:
        if query == places.ADMIN_SQL:
            return Rows((self.municipalities,))
        if query == places.SUBSTATION_SQL:
            self.seen.append(params)
            return Rows((100, 99, 2))
        if query == places.WIND_SQL:
            self.seen.append(params)
            return Rows((50, 48, 40, 1, 2, 3, 2, 1, 0))
        raise AssertionError(query)


RULES = RuleSet(version="9.9.9", states={"osm_nrw": "05"})


def test_the_check_reads_substations_and_units_per_source() -> None:
    db = Database()
    result = places.check_places(db, RULES)  # type: ignore[arg-type]
    assert result.municipalities == 11_094
    assert result.substations == [SubstationPlaces("osm_nrw", 100, 99, 2)]
    assert result.wind == [WindAgreement("05", 50, 48, 40, 1, (2, 3, 2, 1, 0))]
    substations, wind = db.seen
    assert substations == {"source": "osm_nrw", "state": "05"}
    assert wind["admin"] == "bkg_vg250"
    assert wind["state"] == "05"
    assert [wind[k] for k in ("b1", "b2", "b3", "b4")] == list(places.BAND_LIMITS)
    assert wind["statuses"] == ["In Betrieb", "In Planung"]


def test_without_boundaries_nothing_is_measured() -> None:
    with pytest.raises(CoreError, match="no boundaries are applied"):
        places.check_places(Database(municipalities=0), RULES)  # type: ignore[arg-type]
