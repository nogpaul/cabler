"""The *apply* stage: turn a diffed snapshot into the current, classified state in ``staging``.

Only the objects named in the snapshot's change set are touched. A changed or removed object has
its current row closed (``valid_to_version_id``), and an added or changed object gets a new current
row: reprojected to EPSG:25832, classified (substation, line, wind turbine, ...), with its voltage
and output parsed and data-quality findings recorded in ``qa_flags``. Nothing is ever deleted, so
the state at any earlier snapshot can be read back.

Each snapshot is applied in its own transaction, oldest first, and then marked ``applied``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from cabler import runs
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


class ApplyError(Exception):
    """A snapshot cannot be applied: nothing was loaded, or no applier knows its themes."""


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


def apply_version(conn: psycopg.Connection[Any], source_id: str, version_id: int) -> ApplyStep:
    """Close and open staging rows for one snapshot. The caller owns the transaction."""
    params = {"source": source_id, "version": version_id}
    closed = conn.execute(CLOSE_SQL, params).rowcount
    opened = conn.execute(OPEN_SQL, params).rowcount
    current = conn.execute(CURRENT_SQL, (source_id,)).fetchone()
    return ApplyStep(
        version_id=version_id,
        opened=opened,
        closed=closed,
        current=int(current[0]) if current else 0,
        kinds=_histogram(conn, KINDS_SQL, source_id),
        flags=_histogram(conn, FLAGS_SQL, source_id),
    )


def apply_source(conn: psycopg.Connection[Any], spec: SourceSpec) -> ApplyResult:
    """Run the apply stage for one source: every diffed snapshot not yet applied, oldest first."""
    if spec.load_kind != "osm_pbf" or "power" not in spec.load_themes:
        msg = f"{spec.source_id}: no applier for this source; only OSM sources with the power theme"
        raise ApplyError(msg)
    with runs.recorded(conn, "apply", spec.source_id) as run:
        pending = [int(r[0]) for r in conn.execute(PENDING_SQL, (spec.source_id,)).fetchall()]
        steps: list[ApplyStep] = []
        for version_id in pending:
            with conn.transaction():
                steps.append(apply_version(conn, spec.source_id, version_id))
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
