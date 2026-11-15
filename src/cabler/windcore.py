"""The *core* layer for wind units: which units may start a route, and how sure we are of each.

A build reads the current wind units of the register (MaStR), the newest validated OSM snapshots and
the municipal boundaries. It never moves a position and never drops a unit: every unit of the
states of the rule set is stored with the quality of its position and with the reason.

The **position class** says how far the registered position can be trusted:

* ``confirmed``: an OSM turbine stands within ``confirm_m`` of it, an independent source agrees;
* ``plausible``: nothing speaks against it (it may still be wrong, nobody checked it);
* ``doubtful``: it lies farther than ``registered_far_m`` from the municipality that the register
  names for the unit, or farther than ``farm_gap_m`` from every other unit of the same MaStR
  location (wind farm), and no OSM turbine confirms it;
* ``invalid``: the register flags the coordinate as impossible (placeholder, outside the state);
* ``unlocated``: the register has none.

The three nested scenarios are the ones of the substations: **strict** holds the existing units
(in operation, or temporarily shut down) with a confirmed or plausible position, **likely** adds the
planned units with such a position, **inclusive** adds every existing or planned unit whose position
is doubtful. Retired units, invalid and unlocated positions are in no scenario.

A build is a function of the staging tables and of the rules, and its content hash leaves out every
database-generated number, so a rebuild from the raw data must give the same hash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg

from . import core
from .core import CoreError

CLASSES = ("confirmed", "plausible", "doubtful", "invalid", "unlocated")


@dataclass(frozen=True)
class WindRules:
    """The parameters of one build. A version is never changed; a change is a new version."""

    version: str
    states: dict[str, str] = field(default_factory=dict)  # OSM source id -> state key of MaStR
    wind_states: tuple[str, ...] = ()  # more states, built without an OSM extract
    wind_source: str = "mastr_bulk"
    existing_statuses: tuple[str, ...] = ("In Betrieb", "Vorübergehend stillgelegt")
    planned_statuses: tuple[str, ...] = ("In Planung",)
    bad_flags: tuple[str, ...] = (
        "coordinate_outside_state",
        "coordinate_outside_germany",
        "coordinate_placeholder",
    )
    match_m: int = 100  # an OSM turbine this close is stored as evidence
    confirm_m: int = 25  # an OSM turbine this close confirms the position
    registered_far_m: int = 2000  # farther from the registered municipality: doubtful
    farm_gap_m: int = 5000  # farther from every unit of the same wind farm: doubtful
    admin_source: str = "bkg_vg250"

    def __post_init__(self) -> None:
        if not 0 < self.confirm_m <= self.match_m:
            msg = "confirm_m must be positive and not larger than match_m"
            raise CoreError(msg)
        if self.registered_far_m <= 0 or self.farm_gap_m <= 0:
            msg = "registered_far_m and farm_gap_m must be positive"
            raise CoreError(msg)

    @property
    def all_states(self) -> list[str]:
        """Every state of the build: those of the OSM sources and the extra ones."""
        return sorted(set(self.states.values()) | set(self.wind_states))

    def parameters(self) -> dict[str, Any]:
        """Every field except the version, so that no parameter can go unrecorded."""
        values = dataclasses.asdict(self)
        del values["version"]
        values["states"] = dict(sorted(self.states.items()))
        return {k: list(v) if isinstance(v, tuple) else v for k, v in values.items()}


# the OSM sources and their states are the ones of the substations: one place to add a state
DEFAULT_WIND_RULES = WindRules(version="1.1.0", states=dict(core.DEFAULT_RULES.states))


@contextmanager
def repeatable_read(conn: psycopg.Connection[Any]) -> Iterator[None]:
    """Make the next transaction REPEATABLE READ, so that every query of a build sees one snapshot.

    Inside a transaction that already exists the level cannot be changed, and whoever opened that
    transaction decides.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        yield
        return
    before = conn.isolation_level
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    try:
        yield
    finally:
        conn.isolation_level = before


@dataclass(frozen=True)
class UnitClass:
    status_group: str
    position_class: str
    reason: str
    in_strict: bool
    in_likely: bool
    in_inclusive: bool


def status_group(status: str | None, rules: WindRules) -> str:
    if status in rules.existing_statuses:
        return "existing"
    if status in rules.planned_statuses:
        return "planned"
    return "retired"


def classify_unit(
    status: str | None,
    *,
    has_position: bool,
    flagged: bool,
    osm_distance_m: float | None,
    registered_distance_m: float | None,
    farm_gap_m: float | None,
    rules: WindRules,
) -> UnitClass:
    """Quality of the position and the scenarios of one unit. The truth table is in the tests."""
    group = status_group(status, rules)
    if not has_position:
        position, reason = "unlocated", "no_coordinate"
    elif flagged:
        position, reason = "invalid", "coordinate_flagged"
    elif osm_distance_m is not None and osm_distance_m <= rules.confirm_m:
        position, reason = "confirmed", "osm_turbine"
    else:
        far = registered_distance_m is not None and registered_distance_m > rules.registered_far_m
        isolated = farm_gap_m is not None and farm_gap_m > rules.farm_gap_m
        if far and isolated:
            position, reason = "doubtful", "far_from_municipality_and_isolated_in_farm"
        elif far:
            position, reason = "doubtful", "far_from_municipality"
        elif isolated:
            position, reason = "doubtful", "isolated_in_farm"
        else:
            position, reason = "plausible", "register_only"
    if group == "retired":
        return UnitClass(group, position, "retired", False, False, False)
    if position in ("unlocated", "invalid"):
        return UnitClass(group, position, reason, False, False, False)
    if position == "doubtful":
        return UnitClass(group, position, reason, False, False, True)
    existing = group == "existing"
    return UnitClass(group, position, reason, existing, True, True)


@dataclass(frozen=True)
class Row:
    """One wind unit of a build, with everything the core table stores."""

    unit_id: int
    source_id: str
    unit_key: str
    state_key: str | None
    status: str | None
    name: str | None
    wind_farm: str | None
    grid_location_key: str | None
    municipality_key: str | None
    municipality: str | None
    place_key: str | None
    gross_kw: float | None
    hub_height_m: float | None
    rotor_diameter_m: float | None
    commissioned: str | None
    qa_flags: list[str]
    osm_source_id: str | None
    osm_key: str | None
    osm_distance_m: float | None
    registered_distance_m: float | None
    farm_gap_m: float | None
    classification: UnitClass
    geom_ewkb: bytes | None
    geom_wkt: str | None

    def stable(self) -> list[Any]:
        """The content of the row without any number the database generated."""
        c = self.classification
        return [
            self.source_id,
            self.unit_key,
            self.state_key,
            self.status,
            self.name,
            self.wind_farm,
            self.grid_location_key,
            self.municipality_key,
            self.municipality,
            self.place_key,
            self.gross_kw,
            self.hub_height_m,
            self.rotor_diameter_m,
            self.commissioned,
            self.qa_flags,
            self.osm_source_id,
            self.osm_key,
            self.osm_distance_m,
            self.registered_distance_m,
            self.farm_gap_m,
            c.status_group,
            c.position_class,
            c.reason,
            c.in_strict,
            c.in_likely,
            c.in_inclusive,
            self.geom_wkt,
        ]


def content_hash(rows: list[Row]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: (r.source_id, r.unit_key)):
        digest.update(json.dumps(row.stable(), ensure_ascii=True).encode())
        digest.update(b"\n")
    return digest.hexdigest()


# The OSM turbines of the newest validated snapshots. MATERIALIZED on purpose: the application role
# may not create temporary tables, and an inlined CTE would be evaluated again for every unit.
OSM_CTE = """
osm AS MATERIALIZED (
    SELECT p.source_id, p.osm_key, p.location
    FROM unnest(%(osm_sources)s::text[], %(osm_versions)s::bigint[]) AS s(source_id, version_id)
    CROSS JOIN LATERAL staging.osm_power_as_of(s.source_id, s.version_id) p
    WHERE p.kind = 'wind_turbine'
)
"""
FACTS_SQL = (
    "WITH "  # noqa: S608 (constants only, every value is a bound parameter)
    + OSM_CTE
    + """,
u AS MATERIALIZED (
    SELECT * FROM staging.wind_unit_current
    WHERE source_id = %(wind_source)s AND state_key = ANY(%(states)s)
), usable AS MATERIALIZED (
    SELECT unit_id, geom, grid_location_key FROM u
    WHERE geom IS NOT NULL AND NOT (qa_flags && %(bad_flags)s::text[])
      AND status = ANY(%(active)s) AND grid_location_key IS NOT NULL
), farm AS MATERIALIZED (
    SELECT a.unit_id, min(ST_Distance(a.geom, b.geom)) AS gap
    FROM usable a JOIN usable b
      ON a.grid_location_key = b.grid_location_key AND a.unit_id <> b.unit_id
    GROUP BY a.unit_id
)
SELECT u.unit_id, u.source_id, u.unit_key, u.state_key, u.status, u.name, u.wind_farm,
       u.grid_location_key, u.municipality_key, u.municipality, u.gross_kw, u.hub_height_m,
       u.rotor_diameter_m, u.commissioned::text, u.qa_flags,
       ST_AsEWKB(u.geom), ST_AsText(u.geom, 3),
       o.source_id, o.osm_key, round(o.dist::numeric, 1),
       pl.area_key, round(reg.dist::numeric, 1), reg.found, round(f.gap::numeric, 1)
FROM u
LEFT JOIN LATERAL (
    SELECT t.source_id, t.osm_key, ST_Distance(t.location, u.geom) AS dist
    FROM osm t
    WHERE u.geom IS NOT NULL AND ST_DWithin(t.location, u.geom, %(match)s)
    ORDER BY ST_Distance(t.location, u.geom), t.source_id, t.osm_key
    LIMIT 1
) o ON true
-- The place that covers the point: land before water, the lowest key on a tie (as for substations).
LEFT JOIN LATERAL (
    SELECT a.area_key
    FROM staging.admin_area_as_of(%(admin_source)s, %(admin_version)s) a
    WHERE u.geom IS NOT NULL AND a.level = 'municipality' AND a.geom IS NOT NULL
      AND ST_Covers(a.geom, u.geom)
    ORDER BY (a.land_flag IS NOT DISTINCT FROM 4) DESC, a.area_key
    LIMIT 1
) pl ON true
-- The distance to the municipality that the register names; 0 inside it.
LEFT JOIN LATERAL (
    SELECT min(ST_Distance(a.geom, u.geom)) AS dist, count(*) AS found
    FROM staging.admin_area_as_of(%(admin_source)s, %(admin_version)s) a
    WHERE u.geom IS NOT NULL AND u.municipality_key IS NOT NULL
      AND a.level = 'municipality' AND a.geom IS NOT NULL AND a.area_key = u.municipality_key
) reg ON true
LEFT JOIN farm f ON f.unit_id = u.unit_id
ORDER BY u.unit_key
"""
)
# The extracts overlap their neighbours, so one OSM object can be in two of them: it is counted
# once, in the source with the lowest identifier.
TURBINE_SQL = (
    "WITH "  # noqa: S608 (constants only, every value is a bound parameter)
    + OSM_CTE
    + """,
single AS MATERIALIZED (
    SELECT o.source_id, o.osm_key, o.location FROM osm o
    WHERE NOT EXISTS (
        SELECT 1 FROM osm x WHERE x.osm_key = o.osm_key AND x.source_id < o.source_id)
)
SELECT count(*),
       count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM staging.wind_unit_current w
           WHERE w.source_id = %(wind_source)s AND w.geom IS NOT NULL AND w.status = ANY(%(active)s)
             AND ST_DWithin(w.geom, single.location, %(match)s)))
FROM single
"""
)
BUILD_SQL = """
INSERT INTO core.wind_build (rules_version, parameters, source_versions, counts, content_hash)
VALUES (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s) RETURNING wind_build_id
"""
INSERT_SQL = """
INSERT INTO core.wind_unit
    (wind_build_id, source_id, unit_key, unit_id, state_key, status, status_group, name,
     wind_farm, grid_location_key, municipality_key, municipality, place_key, gross_kw,
     hub_height_m, rotor_diameter_m, commissioned, qa_flags, position_class, reason,
     osm_source_id, osm_key, osm_distance_m, registered_distance_m, farm_gap_m, in_strict,
     in_likely, in_inclusive, geom)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, ST_GeomFromEWKB(%s))
"""


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _params(
    rules: WindRules, versions: dict[str, int], admin_version: int | None
) -> dict[str, Any]:
    sources = sorted(rules.states)
    return {
        "osm_sources": sources,
        "osm_versions": [versions[s] for s in sources],
        "wind_source": rules.wind_source,
        "states": rules.all_states,
        "bad_flags": list(rules.bad_flags),
        "active": list(rules.existing_statuses + rules.planned_statuses),
        "match": rules.match_m,
        "admin_source": rules.admin_source,
        "admin_version": admin_version,
    }


def _facts(conn: psycopg.Connection[Any], rules: WindRules, params: dict[str, Any]) -> list[Row]:
    rows = []
    for f in conn.execute(FACTS_SQL, params).fetchall():
        osm_distance, found, gap = _number(f[19]), int(f[22]), _number(f[23])
        registered = _number(f[21]) if found else None  # a key the boundaries lack proves nothing
        classification = classify_unit(
            f[4],
            has_position=f[15] is not None,
            flagged=bool(set(f[14]) & set(rules.bad_flags)),
            osm_distance_m=osm_distance,
            registered_distance_m=registered,
            farm_gap_m=gap,
            rules=rules,
        )
        rows.append(
            Row(
                unit_id=int(f[0]),
                source_id=str(f[1]),
                unit_key=str(f[2]),
                state_key=f[3],
                status=f[4],
                name=f[5],
                wind_farm=f[6],
                grid_location_key=f[7],
                municipality_key=f[8],
                municipality=f[9],
                place_key=f[20],
                gross_kw=_number(f[10]),
                hub_height_m=_number(f[11]),
                rotor_diameter_m=_number(f[12]),
                commissioned=f[13],
                qa_flags=list(f[14]),
                osm_source_id=f[17],
                osm_key=f[18],
                osm_distance_m=osm_distance,
                registered_distance_m=registered,
                farm_gap_m=gap,
                classification=classification,
                geom_ewkb=None if f[15] is None else bytes(f[15]),
                geom_wkt=f[16],
            )
        )
    return rows


def _counts(rows: list[Row], turbines: tuple[int, int]) -> dict[str, int]:
    counts = {
        "units": len(rows),
        "strict": sum(r.classification.in_strict for r in rows),
        "likely": sum(r.classification.in_likely for r in rows),
        "inclusive": sum(r.classification.in_inclusive for r in rows),
        "with_osm_turbine": sum(r.osm_key is not None for r in rows),
        "with_place": sum(r.place_key is not None for r in rows),
        "osm_turbines": turbines[0],
        "osm_turbines_without_unit": turbines[1],
    }
    for name in CLASSES:
        counts[f"class_{name}"] = sum(r.classification.position_class == name for r in rows)
    for group in ("existing", "planned", "retired"):
        counts[f"status_{group}"] = sum(r.classification.status_group == group for r in rows)
    return counts


def _insert_values(build_id: int, r: Row) -> tuple[Any, ...]:
    c = r.classification
    return (
        build_id,
        r.source_id,
        r.unit_key,
        r.unit_id,
        r.state_key,
        r.status,
        c.status_group,
        r.name,
        r.wind_farm,
        r.grid_location_key,
        r.municipality_key,
        r.municipality,
        r.place_key,
        r.gross_kw,
        r.hub_height_m,
        r.rotor_diameter_m,
        r.commissioned,
        r.qa_flags,
        c.position_class,
        c.reason,
        r.osm_source_id,
        r.osm_key,
        r.osm_distance_m,
        r.registered_distance_m,
        r.farm_gap_m,
        c.in_strict,
        c.in_likely,
        c.in_inclusive,
        r.geom_ewkb,
    )


@dataclass(frozen=True)
class WindBuildResult:
    build_id: int
    content_hash: str
    counts: dict[str, int]
    source_versions: dict[str, int]


def build_wind_units(
    conn: psycopg.Connection[Any], rules: WindRules = DEFAULT_WIND_RULES
) -> WindBuildResult:
    """Build the wind units of the core from the register and the newest validated OSM snapshots."""
    if not rules.all_states:
        msg = "the rule set names no state"
        raise CoreError(msg)
    with repeatable_read(conn), conn.transaction():
        # the sources are validated exactly as for the substations: no verdict or a quarantined
        # snapshot stops the build
        versions = core.validated_sources(conn, rules.states)
        boundaries = conn.execute(core.ADMIN_VERSION_SQL, (rules.admin_source,)).fetchone()
        admin_version = int(boundaries[0]) if boundaries and boundaries[0] is not None else None
        params = _params(rules, versions, admin_version)
        rows = _facts(conn, rules, params)
        turbines = conn.execute(TURBINE_SQL, params).fetchone() or (0, 0)
        versions[rules.admin_source] = admin_version or 0
        wind_version = conn.execute(core.WIND_VERSION_SQL, (rules.all_states,)).fetchone()
        versions["wind_units"] = int(wind_version[0]) if wind_version else 0
        counts = _counts(rows, (int(turbines[0]), int(turbines[1])))
        digest = content_hash(rows)
        built = conn.execute(
            BUILD_SQL,
            (
                rules.version,
                json.dumps(rules.parameters()),
                json.dumps(versions),
                json.dumps(counts),
                digest,
            ),
        ).fetchone()
        build_id = core.returned_id(built)
        with conn.cursor() as cursor:
            cursor.executemany(INSERT_SQL, [_insert_values(build_id, r) for r in rows])
    return WindBuildResult(build_id, digest, counts, versions)


def format_report(result: WindBuildResult) -> list[str]:
    c = result.counts
    lines = [
        f"wind units build {result.build_id}  hash {result.content_hash[:16]}",
        f"    sources: {', '.join(f'{k} {v}' for k, v in result.source_versions.items())}",
        f"    units {c['units']:,}: existing {c['status_existing']:,}, "
        f"planned {c['status_planned']:,}, retired {c['status_retired']:,}",
        f"    scenarios: strict {c['strict']:,}, likely {c['likely']:,}, "
        f"inclusive {c['inclusive']:,}",
        "    positions: " + ", ".join(f"{name} {c[f'class_{name}']:,}" for name in CLASSES),
        f"    OSM turbines {c['osm_turbines']:,}, without a unit within reach "
        f"{c['osm_turbines_without_unit']:,}; units with an OSM turbine {c['with_osm_turbine']:,}",
    ]
    return lines
