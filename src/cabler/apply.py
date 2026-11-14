"""The *apply* stage: turn a diffed snapshot into the current, classified state in ``staging``.

Only the objects named in the snapshot's change set are touched. A changed or removed object has
its current row closed (``valid_to_version_id``), and an added or changed object gets a new current
row: reprojected to EPSG:25832, classified (substation, line, wind turbine, ...), with its voltage
and output parsed and data-quality findings recorded in ``qa_flags``. Nothing is ever deleted, so
the state at any earlier snapshot can be read back.

Four kinds of source have an applier: OpenStreetMap power data (``staging.osm_power_feature``),
MaStR wind units (``staging.wind_unit``, catalogue codes decoded, coordinates checked), CORINE land
cover (``staging.land_cover``) and the BfN protected areas (``staging.protected_area``).

Each snapshot is applied in its own transaction, oldest first, and then marked ``applied``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from cabler import polygon_sql, runs
from cabler.sources import SourceSpec

PENDING_SQL = """
SELECT version_id
FROM meta.source_version
WHERE source_id = %s AND status = 'diffed'
ORDER BY created_at, version_id
"""
LATEST_SQL = """
SELECT version_id
FROM meta.source_version
WHERE source_id = %s AND status = 'applied'
ORDER BY created_at DESC, version_id DESC
LIMIT 1
"""

CLOSE_SQL = """
UPDATE staging.osm_power_feature f
SET valid_to_version_id = %(version)s
FROM raw.osm_change c
WHERE c.version_id = %(version)s
  AND c.change IN ('changed', 'removed')
  AND f.source_id = %(source)s
  AND f.layer = c.layer
  AND f.osm_key = c.osm_key
  AND f.valid_to_version_id IS NULL
"""
OPEN_SQL = """
INSERT INTO staging.osm_power_feature
    (source_id, layer, osm_key, valid_from_version_id, kind, power, name, ref, operator,
     substation_type, voltage_raw, voltage_v, voltage_class, generator_source, output_raw,
     output_w, qa_flags, osm_version, osm_timestamp, geom, location)
SELECT %(source)s, o.layer, o.osm_key, %(version)s, k.kind, o.power, o.name, o.ref, o.operator,
       staging.osm_tag(o.other_tags, 'substation'), o.voltage, k.voltage_v,
       staging.voltage_class(k.kind, k.voltage_v), o.generator_source,
       o.generator_output_electricity, k.output_w,
       array_remove(ARRAY[
           CASE WHEN k.kind IN ('substation', 'line', 'cable') AND o.voltage IS NULL
                THEN 'voltage_missing' END,
           CASE WHEN o.voltage IS NOT NULL AND k.voltage_v IS NULL THEN 'voltage_unparsable' END,
           CASE WHEN k.kind = 'wind_turbine' AND o.generator_output_electricity IS NULL
                THEN 'output_missing' END,
           CASE WHEN o.generator_output_electricity IS NOT NULL AND k.output_w IS NULL
                THEN 'output_unparsable' END,
           CASE WHEN NOT g.valid THEN 'geometry_repaired' END
       ], NULL),
       o.osm_version, o.osm_timestamp, g.geom, ST_PointOnSurface(g.geom)
FROM raw.osm_change c
JOIN raw.osm_object o
  ON o.version_id = c.version_id AND o.layer = c.layer AND o.osm_key = c.osm_key
CROSS JOIN LATERAL (
    SELECT staging.power_kind(o.power, o.generator_source, o.generator_method) AS kind,
           staging.parse_voltage_v(o.voltage) AS voltage_v,
           staging.parse_output_w(o.generator_output_electricity) AS output_w
) k
CROSS JOIN LATERAL (
    SELECT ST_IsValid(t) AS valid, CASE WHEN ST_IsValid(t) THEN t ELSE ST_MakeValid(t) END AS geom
    FROM (SELECT ST_Transform(o.geom, 25832) AS t) s
) g
WHERE c.version_id = %(version)s
  AND c.change IN ('added', 'changed')
  AND o.power IS NOT NULL
"""
CURRENT_SQL = """
SELECT count(*) FROM staging.osm_power_feature
WHERE source_id = %s AND valid_to_version_id IS NULL
"""
KINDS_SQL = """
SELECT kind, count(*) FROM staging.osm_power_feature
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY kind
"""
FLAGS_SQL = """
SELECT flag, count(*)
FROM staging.osm_power_feature, unnest(qa_flags) AS flag
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY flag
"""


WIND_CLOSE_SQL = """
UPDATE staging.wind_unit u
SET valid_to_version_id = %(version)s
FROM raw.feature_change c
WHERE c.version_id = %(version)s
  AND c.layer = 'wind_unit'
  AND c.change IN ('changed', 'removed')
  AND u.source_id = %(source)s
  AND u.unit_key = c.feature_key
  AND u.valid_to_version_id IS NULL
"""
# Coordinates: a unit gets at most one coordinate finding, the most specific one. The box of
# Germany is wide on purpose, so that offshore units are not flagged. 6.0 / 50.0 is the placeholder
# that appears in the register where no position was entered.
WIND_OPEN_SQL = """
WITH catalogue AS (
    SELECT coalesce(jsonb_object_agg(feature_key, attributes ->> 'Wert'), '{}'::jsonb) AS m
    FROM raw.feature
    WHERE version_id = %(version)s AND layer = 'catalogue_value'
)
INSERT INTO staging.wind_unit
    (source_id, unit_key, valid_from_version_id, state_key, status_code, status, name, wind_farm,
     municipality_key, municipality, district, postcode, manufacturer, type_designation,
     location_type, gross_kw, net_kw, hub_height_m, rotor_diameter_m, commissioned,
     planned_commissioning, decommissioned, grid_location_key, qa_flags, geom)
SELECT %(source)s, f.feature_key, %(version)s,
       left(a ->> 'Gemeindeschluessel', 2),
       CASE WHEN a ->> 'EinheitBetriebsstatus' ~ '^[0-9]{1,9}$'
            THEN (a ->> 'EinheitBetriebsstatus')::integer END,
       cat.m ->> (a ->> 'EinheitBetriebsstatus'),
       a ->> 'NameStromerzeugungseinheit', a ->> 'NameWindpark',
       a ->> 'Gemeindeschluessel', a ->> 'Gemeinde', a ->> 'Landkreis', a ->> 'Postleitzahl',
       cat.m ->> (a ->> 'Hersteller'), a ->> 'Typenbezeichnung',
       cat.m ->> (a ->> 'WindAnLandOderAufSee'),
       gross.v, staging.parse_number(a ->> 'Nettonennleistung'),
       staging.parse_number(a ->> 'Nabenhoehe'), staging.parse_number(a ->> 'Rotordurchmesser'),
       staging.parse_date(a ->> 'Inbetriebnahmedatum'),
       staging.parse_date(a ->> 'GeplantesInbetriebnahmedatum'),
       staging.parse_date(a ->> 'DatumEndgueltigeStilllegung'),
       a ->> 'LokationMaStRNummer',
       array_remove(ARRAY[
           CASE WHEN f.geom IS NULL THEN 'coordinate_missing'
                WHEN abs(ST_X(f.geom) - 6.0) < 1e-9 AND abs(ST_Y(f.geom) - 50.0) < 1e-9
                THEN 'coordinate_placeholder'
                WHEN NOT (ST_X(f.geom) BETWEEN 3.0 AND 15.5 AND ST_Y(f.geom) BETWEEN 47.0 AND 56.0)
                THEN 'coordinate_outside_germany'
                WHEN sb.state_key IS NOT NULL
                 AND NOT (ST_X(f.geom) BETWEEN sb.lon_min AND sb.lon_max
                          AND ST_Y(f.geom) BETWEEN sb.lat_min AND sb.lat_max)
                THEN 'coordinate_outside_state' END,
           CASE WHEN gross.v IS NULL OR gross.v <= 0 THEN 'capacity_missing' END,
           CASE WHEN gross.v > 20000 THEN 'capacity_implausible' END,
           CASE WHEN a ->> 'Inbetriebnahmedatum' IS NOT NULL
                 AND staging.parse_date(a ->> 'Inbetriebnahmedatum') IS NULL
                THEN 'date_unparsable' END
       ], NULL),
       CASE WHEN f.geom IS NOT NULL THEN ST_Transform(f.geom, 25832) END
FROM raw.feature_change c
JOIN raw.feature f
  ON f.version_id = c.version_id AND f.layer = c.layer AND f.feature_key = c.feature_key
CROSS JOIN catalogue cat
CROSS JOIN LATERAL (SELECT f.attributes AS a) x
CROSS JOIN LATERAL (SELECT staging.parse_number(a ->> 'Bruttoleistung') AS v) gross
LEFT JOIN staging.state_box sb ON sb.state_key = left(a ->> 'Gemeindeschluessel', 2)
WHERE c.version_id = %(version)s
  AND c.layer = 'wind_unit'
  AND c.change IN ('added', 'changed')
"""
WIND_CURRENT_SQL = """
SELECT count(*) FROM staging.wind_unit
WHERE source_id = %s AND valid_to_version_id IS NULL
"""
WIND_KINDS_SQL = """
SELECT coalesce(status, 'unknown'), count(*) FROM staging.wind_unit
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY 1
"""
WIND_FLAGS_SQL = """
SELECT flag, count(*)
FROM staging.wind_unit, unnest(qa_flags) AS flag
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY flag
"""


class ApplyError(Exception):
    """A snapshot cannot be applied: nothing was loaded, or no applier knows its themes."""


@dataclass(frozen=True)
class _Statements:
    """The SQL that applies one kind of source to its staging table."""

    close: str
    open: str
    current: str
    kinds: str
    flags: str
    analyze: str  # refresh statistics of the staging table for the stages that read it next


OSM_STATEMENTS = _Statements(
    CLOSE_SQL, OPEN_SQL, CURRENT_SQL, KINDS_SQL, FLAGS_SQL, "ANALYZE staging.osm_power_feature"
)
WIND_STATEMENTS = _Statements(
    WIND_CLOSE_SQL,
    WIND_OPEN_SQL,
    WIND_CURRENT_SQL,
    WIND_KINDS_SQL,
    WIND_FLAGS_SQL,
    "ANALYZE staging.wind_unit",
)
LAND_COVER_STATEMENTS = _Statements(
    polygon_sql.LAND_COVER_CLOSE_SQL,
    polygon_sql.LAND_COVER_OPEN_SQL,
    polygon_sql.LAND_COVER_CURRENT_SQL,
    polygon_sql.LAND_COVER_KINDS_SQL,
    polygon_sql.LAND_COVER_FLAGS_SQL,
    "ANALYZE staging.land_cover",
)
PROTECTED_STATEMENTS = _Statements(
    polygon_sql.PROTECTED_CLOSE_SQL,
    polygon_sql.PROTECTED_OPEN_SQL,
    polygon_sql.PROTECTED_CURRENT_SQL,
    polygon_sql.PROTECTED_KINDS_SQL,
    polygon_sql.PROTECTED_FLAGS_SQL,
    "ANALYZE staging.protected_area",
)
ADMIN_STATEMENTS = _Statements(
    polygon_sql.ADMIN_CLOSE_SQL,
    polygon_sql.ADMIN_OPEN_SQL,
    polygon_sql.ADMIN_CURRENT_SQL,
    polygon_sql.ADMIN_KINDS_SQL,
    polygon_sql.ADMIN_FLAGS_SQL,
    "ANALYZE staging.admin_area",
)
PARCEL_STATEMENTS = _Statements(
    polygon_sql.PARCEL_CLOSE_SQL,
    polygon_sql.PARCEL_OPEN_SQL,
    polygon_sql.PARCEL_CURRENT_SQL,
    polygon_sql.PARCEL_KINDS_SQL,
    polygon_sql.PARCEL_FLAGS_SQL,
    "ANALYZE staging.parcel",
)
PARCEL_NAS_STATEMENTS = _Statements(
    polygon_sql.PARCEL_NAS_CLOSE_SQL,
    polygon_sql.PARCEL_NAS_OPEN_SQL,
    polygon_sql.PARCEL_CURRENT_SQL,
    polygon_sql.PARCEL_NAS_KINDS_SQL,
    polygon_sql.PARCEL_FLAGS_SQL,
    "ANALYZE staging.parcel",
)


def _statements_for(spec: SourceSpec) -> _Statements:
    themes = set(spec.load_themes)
    if spec.load_kind == "osm_pbf" and "power" in themes:
        return OSM_STATEMENTS
    if spec.load_kind == "mastr_xml" and "wind" in themes:
        return WIND_STATEMENTS
    if spec.load_kind == "ogr" and themes == {"land_cover"}:
        return LAND_COVER_STATEMENTS
    if spec.load_kind == "ogr" and themes == {"parcel"}:
        return PARCEL_STATEMENTS
    if spec.load_kind == "nas_gml" and themes == {"parcel"}:
        return PARCEL_NAS_STATEMENTS
    if spec.load_kind == "ogr" and themes and themes <= set(polygon_sql.PROTECTED_KINDS):
        return PROTECTED_STATEMENTS
    if spec.load_kind == "ogr" and themes and themes <= set(polygon_sql.ADMIN_LEVELS):
        return ADMIN_STATEMENTS
    msg = (
        f"{spec.source_id}: no applier for this source; known: OSM sources with the power "
        "theme, MaStR sources with the wind theme, land cover, parcels, the protected-area "
        "themes and the administrative-area themes"
    )
    raise ApplyError(msg)


@dataclass(frozen=True)
class ApplyStep:
    """What applying one snapshot did to the staging state of its source."""

    version_id: int
    opened: int
    closed: int
    current: int
    kinds: dict[str, int]
    flags: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "opened": self.opened,
            "closed": self.closed,
            "current": self.current,
            "kinds": self.kinds,
            "flags": self.flags,
        }


@dataclass(frozen=True)
class ApplyResult:
    source_id: str
    status: str  # applied | skipped
    version_id: int | None
    steps: tuple[ApplyStep, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "version_id": self.version_id,
            "steps": [step.to_json() for step in self.steps],
        }


def _histogram(conn: psycopg.Connection[Any], query: str, source_id: str) -> dict[str, int]:
    rows = conn.execute(query, (source_id,)).fetchall()
    return {str(key): int(count) for key, count in sorted(rows)}


def apply_version(
    conn: psycopg.Connection[Any],
    source_id: str,
    version_id: int,
    sql: _Statements = OSM_STATEMENTS,
) -> ApplyStep:
    """Close and open staging rows for one snapshot. The caller owns the transaction."""
    params = {"source": source_id, "version": version_id}
    closed = conn.execute(sql.close, params).rowcount
    opened = conn.execute(sql.open, params).rowcount
    conn.execute(sql.analyze)
    current = conn.execute(sql.current, (source_id,)).fetchone()
    return ApplyStep(
        version_id=version_id,
        opened=opened,
        closed=closed,
        current=int(current[0]) if current else 0,
        kinds=_histogram(conn, sql.kinds, source_id),
        flags=_histogram(conn, sql.flags, source_id),
    )


def apply_source(conn: psycopg.Connection[Any], spec: SourceSpec) -> ApplyResult:
    """Run the apply stage for one source: every diffed snapshot not yet applied, oldest first."""
    sql = _statements_for(spec)
    with runs.recorded(conn, "apply", spec.source_id) as run:
        pending = [int(r[0]) for r in conn.execute(PENDING_SQL, (spec.source_id,)).fetchall()]
        steps: list[ApplyStep] = []
        for version_id in pending:
            with conn.transaction():
                steps.append(apply_version(conn, spec.source_id, version_id, sql))
                conn.execute(
                    "UPDATE meta.source_version SET status = 'applied' WHERE version_id = %s",
                    (version_id,),
                )
        if steps:
            result = ApplyResult(spec.source_id, "applied", steps[-1].version_id, tuple(steps))
        else:
            latest = conn.execute(LATEST_SQL, (spec.source_id,)).fetchone()
            result = ApplyResult(
                spec.source_id, "skipped", None if latest is None else int(latest[0]), ()
            )
        run.version_id = result.version_id
        run.result = result.to_json()
    return result
