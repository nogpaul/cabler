"""Where are the substations and the wind units? The places that the boundaries give, measured.

Two checks, both read-only:

* **Substations.** How many have a municipality, and how many lie in another state than the one
  of their OSM extract. A Geofabrik extract is cut along a polygon that reaches a little into the
  neighbouring states, so a few substations belong elsewhere; the ones without any place lie outside
  Germany.
* **Wind units.** The register (MaStR) names the municipality of every unit, and every unit has a
  coordinate that the operator typed in. Both say where the unit is. The report counts the units
  whose coordinate lies in the municipality the register names, and, for the others, how far the
  coordinate is from it. A unit just beside a border is a boundary effect (the boundaries are
  generalised at 1:250 000); a unit kilometres away has a coordinate that cannot be trusted as is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from cabler import core
from cabler.core import DEFAULT_RULES, RuleSet

# Upper limits in metres of the distance bands; everything above the last one is "farther".
BAND_LIMITS = (100, 500, 2_000, 10_000)

SUBSTATION_SQL = """
SELECT count(*), count(municipality_key),
       count(*) FILTER (
           WHERE municipality_key IS NOT NULL AND left(municipality_key, 2) <> %(state)s
       )
FROM core.substation_current
WHERE source_id = %(source)s
"""
# The place of a unit is looked up as the place of a substation is: land before water, the lowest
# key on a tie. A unit whose key is not in the boundaries at all has no distance (key_unknown).
WIND_SQL = """
WITH p AS (
    SELECT w.mk, w.geom,
           (SELECT a.area_key FROM staging.admin_area_current a
            WHERE a.source_id = %(admin)s AND a.level = 'municipality' AND ST_Covers(a.geom, w.geom)
            ORDER BY (a.land_flag IS NOT DISTINCT FROM 4) DESC, a.area_key LIMIT 1) AS pk
    FROM (SELECT municipality_key AS mk, geom FROM staging.wind_unit_current
          WHERE state_key = %(state)s AND geom IS NOT NULL AND status = ANY(%(statuses)s)
            AND NOT (qa_flags && %(bad_flags)s::text[])) w
), d AS (
    SELECT p.pk, p.mk,
           CASE WHEN p.pk IS NOT DISTINCT FROM p.mk THEN NULL ELSE
               (SELECT min(ST_Distance(a.geom, p.geom)) FROM staging.admin_area_current a
                WHERE a.source_id = %(admin)s AND a.level = 'municipality' AND a.area_key = p.mk)
           END AS dist
    FROM p
)
SELECT count(*), count(pk), count(*) FILTER (WHERE pk IS NOT DISTINCT FROM mk),
       count(*) FILTER (WHERE pk IS DISTINCT FROM mk AND dist IS NULL),
       count(*) FILTER (WHERE dist <= %(b1)s),
       count(*) FILTER (WHERE dist > %(b1)s AND dist <= %(b2)s),
       count(*) FILTER (WHERE dist > %(b2)s AND dist <= %(b3)s),
       count(*) FILTER (WHERE dist > %(b3)s AND dist <= %(b4)s),
       count(*) FILTER (WHERE dist > %(b4)s)
FROM d
"""
ADMIN_SQL = """
SELECT count(*) FROM staging.admin_area_current WHERE source_id = %s AND level = 'municipality'
"""


@dataclass(frozen=True)
class SubstationPlaces:
    source_id: str
    substations: int
    placed: int
    other_state: int  # placed, but in another state than the extract's

    @property
    def unplaced(self) -> int:
        return self.substations - self.placed


@dataclass(frozen=True)
class WindAgreement:
    state_key: str
    units: int
    placed: int  # the coordinate lies inside some municipality
    same: int  # ... and it is the one that the register names
    key_unknown: int  # the register names a municipality that the boundaries do not have
    bands: tuple[int, ...]  # the others, by distance to the named municipality

    @property
    def share(self) -> float | None:
        return self.same / self.units if self.units else None


@dataclass(frozen=True)
class Places:
    municipalities: int  # in the boundaries that were used
    substations: list[SubstationPlaces]
    wind: list[WindAgreement]


def check_places(conn: psycopg.Connection[Any], rules: RuleSet = DEFAULT_RULES) -> Places:
    """Measure the places of the substations of the newest build and of the wind units."""
    boundaries = conn.execute(ADMIN_SQL, (rules.admin_source,)).fetchone()
    municipalities = int(boundaries[0]) if boundaries else 0
    if municipalities == 0:
        msg = f"{rules.admin_source}: no boundaries are applied; run: fetch, load, diff, apply"
        raise core.CoreError(msg)
    substations: list[SubstationPlaces] = []
    wind: list[WindAgreement] = []
    limits = dict(zip(("b1", "b2", "b3", "b4"), BAND_LIMITS, strict=True))
    for source_id in sorted(rules.states):
        state = rules.states[source_id]
        row = conn.execute(SUBSTATION_SQL, {"source": source_id, "state": state}).fetchone()
        if row is not None:
            substations.append(SubstationPlaces(source_id, int(row[0]), int(row[1]), int(row[2])))
        params = {
            "admin": rules.admin_source,
            "state": state,
            "statuses": list(rules.wind_statuses),
            "bad_flags": list(rules.wind_bad_flags),
            **limits,
        }
        found = conn.execute(WIND_SQL, params).fetchone()
        if found is not None:
            counts = [int(x) for x in found]
            wind.append(
                WindAgreement(state, counts[0], counts[1], counts[2], counts[3], tuple(counts[4:]))
            )
    return Places(municipalities, substations, wind)


def _share(part: int, whole: int) -> str:
    return "-" if whole == 0 else f"{part / whole:.1%}"


def _metres(limit: int) -> str:
    return f"{limit / 1000:g} km" if limit >= 1000 else f"{limit} m"


def format_report(result: Places) -> list[str]:
    """The measurements as lines of text."""
    lines = [
        f"places from {result.municipalities:,} municipalities",
        "",
        "substations (newest core build)",
        f"  {'source':<14} {'substations':>11} {'with place':>11} {'no place':>9}"
        f" {'other state':>12}",
    ]
    for s in result.substations:
        lines.append(
            f"  {s.source_id:<14} {s.substations:>11,} {s.placed:>11,} {s.unplaced:>9,}"
            f" {s.other_state:>12,}"
        )
    lines += [
        "",
        "wind units: the municipality of the register against the one that covers the coordinate",
        "(units in the statuses of the rule set, without a coordinate finding)",
    ]
    names = [f"<= {_metres(x)}" for x in BAND_LIMITS] + [f"> {_metres(BAND_LIMITS[-1])}"]
    header = f"  {'state':<6} {'units':>7} {'same':>7} {'share':>7}  " + " ".join(
        f"{name:>9}" for name in names
    )
    lines.append(header + f" {'key unknown':>12}")
    for w in result.wind:
        lines.append(
            f"  {w.state_key:<6} {w.units:>7,} {w.same:>7,} {_share(w.same, w.units):>7}  "
            + " ".join(f"{n:>9,}" for n in w.bands)
            + f" {w.key_unknown:>12,}"
        )
    lines += [
        "",
        "The distance columns count the units that lie outside the named municipality, by how far",
        "their coordinate is from it. The boundaries are generalised (1:250 000): a few hundred",
        "metres at a border are not an error of the unit.",
    ]
    return lines
