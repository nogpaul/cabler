"""The *diff* stage: compare a loaded snapshot with the one before it, object by object.

Objects are matched on ``(layer, osm_key)``. An object is *added* when only the new snapshot has
it, *removed* when only the old one has it, and *changed* when any stored column or the geometry
differs. The geometry is compared as well as the tags because an OSM way keeps its version number
when one of its nodes moves.

Sources that are not OpenStreetMap store their features in ``raw.feature``. They are matched on
``(layer, feature_key)``, the publisher's own identifier, and a feature is *changed* when its
content hash differs; the names of the attributes whose values differ (and ``geometry``) are
recorded.

Each snapshot is diffed in its own transaction, oldest first, and then marked ``diffed``.
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
WHERE source_id = %s AND status = 'loaded'
ORDER BY created_at, version_id
"""
PREVIOUS_SQL = """
SELECT version_id
FROM meta.source_version
WHERE source_id = %s AND status IN ('diffed', 'applied') AND version_id < %s
ORDER BY created_at DESC, version_id DESC
LIMIT 1
"""
LATEST_SQL = """
SELECT version_id
FROM meta.source_version
WHERE source_id = %s AND status IN ('diffed', 'applied')
ORDER BY created_at DESC, version_id DESC
LIMIT 1
"""

BASELINE_SQL = """
INSERT INTO raw.osm_change
    (version_id, previous_version_id, layer, osm_key, change, changed_fields, new_osm_version)
SELECT version_id, NULL, layer, osm_key, 'added', ARRAY[]::text[], osm_version
FROM raw.osm_object
WHERE version_id = %(new)s
"""
COMPARE_SQL = """
WITH new AS (
    SELECT layer, osm_key, osm_version,
           to_jsonb(o) - 'version_id' - 'geom' AS attrs, ST_AsEWKB(geom) AS shape
    FROM raw.osm_object o WHERE version_id = %(new)s
), old AS (
    SELECT layer, osm_key, osm_version,
           to_jsonb(o) - 'version_id' - 'geom' AS attrs, ST_AsEWKB(geom) AS shape
    FROM raw.osm_object o WHERE version_id = %(old)s
)
INSERT INTO raw.osm_change
    (version_id, previous_version_id, layer, osm_key, change, changed_fields,
     old_osm_version, new_osm_version)
SELECT %(new)s, %(old)s,
       coalesce(n.layer, o.layer), coalesce(n.osm_key, o.osm_key),
       CASE WHEN o.osm_key IS NULL THEN 'added'
            WHEN n.osm_key IS NULL THEN 'removed'
            ELSE 'changed' END,
       CASE WHEN o.osm_key IS NULL OR n.osm_key IS NULL THEN ARRAY[]::text[]
            ELSE array(SELECT k FROM jsonb_object_keys(n.attrs || o.attrs) AS k
                       WHERE n.attrs -> k IS DISTINCT FROM o.attrs -> k ORDER BY k)
                 || CASE WHEN n.shape IS DISTINCT FROM o.shape
                         THEN ARRAY['geometry'] ELSE ARRAY[]::text[] END
       END,
       o.osm_version, n.osm_version
FROM new n
FULL OUTER JOIN old o ON n.layer = o.layer AND n.osm_key = o.osm_key
WHERE o.osm_key IS NULL OR n.osm_key IS NULL
   OR n.attrs IS DISTINCT FROM o.attrs OR n.shape IS DISTINCT FROM o.shape
"""
KINDS_SQL = "SELECT change, count(*) FROM raw.osm_change WHERE version_id = %s GROUP BY change"
FIELDS_SQL = """
SELECT field, count(*)
FROM raw.osm_change, unnest(changed_fields) AS field
WHERE version_id = %s AND change = 'changed'
GROUP BY field
ORDER BY count(*) DESC, field
"""
TOTAL_SQL = "SELECT count(*) FROM raw.osm_object WHERE version_id = %s"

FEATURE_BASELINE_SQL = """
INSERT INTO raw.feature_change
    (version_id, previous_version_id, layer, feature_key, change, changed_fields)
SELECT version_id, NULL, layer, feature_key, 'added', ARRAY[]::text[]
FROM raw.feature
WHERE version_id = %(new)s
"""
FEATURE_COMPARE_SQL = """
INSERT INTO raw.feature_change
    (version_id, previous_version_id, layer, feature_key, change, changed_fields)
SELECT %(new)s, %(old)s,
       coalesce(n.layer, o.layer), coalesce(n.feature_key, o.feature_key),
       CASE WHEN o.feature_key IS NULL THEN 'added'
            WHEN n.feature_key IS NULL THEN 'removed'
            ELSE 'changed' END,
       CASE WHEN o.feature_key IS NULL OR n.feature_key IS NULL THEN ARRAY[]::text[]
            ELSE array(SELECT k FROM jsonb_object_keys(n.attributes || o.attributes) AS k
                       WHERE n.attributes -> k IS DISTINCT FROM o.attributes -> k ORDER BY k)
                 || CASE WHEN ST_AsEWKB(n.geom) IS DISTINCT FROM ST_AsEWKB(o.geom)
                         THEN ARRAY['geometry'] ELSE ARRAY[]::text[] END
       END
FROM (SELECT * FROM raw.feature WHERE version_id = %(new)s) n
FULL OUTER JOIN (SELECT * FROM raw.feature WHERE version_id = %(old)s) o
  ON n.layer = o.layer AND n.feature_key = o.feature_key
WHERE o.feature_key IS NULL OR n.feature_key IS NULL OR n.content_hash <> o.content_hash
"""
FEATURE_KINDS_SQL = (
    "SELECT change, count(*) FROM raw.feature_change WHERE version_id = %s GROUP BY change"
)
FEATURE_FIELDS_SQL = """
SELECT field, count(*)
FROM raw.feature_change, unnest(changed_fields) AS field
WHERE version_id = %s AND change = 'changed'
GROUP BY field
ORDER BY count(*) DESC, field
"""
FEATURE_TOTAL_SQL = "SELECT count(*) FROM raw.feature WHERE version_id = %s"


class DiffError(Exception):
    """A snapshot cannot be diffed: the source has no loader, so nothing was ever loaded."""


@dataclass(frozen=True)
class DiffStep:
    """The comparison of one snapshot with its predecessor (``None`` for a first snapshot)."""

    version_id: int
    previous_version_id: int | None
    added: int
    changed: int
    removed: int
    total_before: int
    total_after: int
    changed_fields: dict[str, int]

    @property
    def baseline(self) -> bool:
        return self.previous_version_id is None

    def to_json(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "previous_version_id": self.previous_version_id,
            "baseline": self.baseline,
            "added": self.added,
            "changed": self.changed,
            "removed": self.removed,
            "total_before": self.total_before,
            "total_after": self.total_after,
            "changed_fields": self.changed_fields,
        }


@dataclass(frozen=True)
class DiffResult:
    source_id: str
    status: str  # diffed | skipped
    version_id: int | None
    steps: tuple[DiffStep, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "version_id": self.version_id,
            "steps": [step.to_json() for step in self.steps],
        }


def _count(conn: psycopg.Connection[Any], query: str, version_id: int) -> int:
    row = conn.execute(query, (version_id,)).fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class _Statements:
    """The SQL of the diff for one storage layout (OSM objects, or generic features)."""

    baseline: str
    compare: str
    kinds: str
    fields: str
    total: str


OSM_STATEMENTS = _Statements(BASELINE_SQL, COMPARE_SQL, KINDS_SQL, FIELDS_SQL, TOTAL_SQL)
FEATURE_STATEMENTS = _Statements(
    FEATURE_BASELINE_SQL,
    FEATURE_COMPARE_SQL,
    FEATURE_KINDS_SQL,
    FEATURE_FIELDS_SQL,
    FEATURE_TOTAL_SQL,
)


def diff_version(
    conn: psycopg.Connection[Any],
    version_id: int,
    previous_id: int | None,
    load_kind: str = "osm_pbf",
) -> DiffStep:
    """Store the change set of one snapshot and describe it. The caller owns the transaction."""
    sql = OSM_STATEMENTS if load_kind == "osm_pbf" else FEATURE_STATEMENTS
    if previous_id is None:
        conn.execute(sql.baseline, {"new": version_id})
    else:
        conn.execute(sql.compare, {"new": version_id, "old": previous_id})
    kinds = {str(k): int(n) for k, n in conn.execute(sql.kinds, (version_id,)).fetchall()}
    fields = {str(f): int(n) for f, n in conn.execute(sql.fields, (version_id,)).fetchall()}
    return DiffStep(
        version_id=version_id,
        previous_version_id=previous_id,
        added=kinds.get("added", 0),
        changed=kinds.get("changed", 0),
        removed=kinds.get("removed", 0),
        total_before=0 if previous_id is None else _count(conn, sql.total, previous_id),
        total_after=_count(conn, sql.total, version_id),
        changed_fields=fields,
    )


def diff_source(conn: psycopg.Connection[Any], spec: SourceSpec) -> DiffResult:
    """Run the diff stage for one source: every loaded snapshot not yet diffed, oldest first."""
    if spec.load_kind is None:
        msg = f"{spec.source_id}: no [load] section, so nothing is ever loaded to diff"
        raise DiffError(msg)
    with runs.recorded(conn, "diff", spec.source_id) as run:
        pending = [int(r[0]) for r in conn.execute(PENDING_SQL, (spec.source_id,)).fetchall()]
        steps: list[DiffStep] = []
        for version_id in pending:
            with conn.transaction():
                row = conn.execute(PREVIOUS_SQL, (spec.source_id, version_id)).fetchone()
                previous_id = None if row is None else int(row[0])
                steps.append(diff_version(conn, version_id, previous_id, spec.load_kind))
                conn.execute(
                    "UPDATE meta.source_version SET status = 'diffed' WHERE version_id = %s",
                    (version_id,),
                )
        if steps:
            result = DiffResult(spec.source_id, "diffed", steps[-1].version_id, tuple(steps))
        else:
            latest = conn.execute(LATEST_SQL, (spec.source_id,)).fetchone()
            result = DiffResult(
                spec.source_id, "skipped", None if latest is None else int(latest[0]), ()
            )
        run.version_id = result.version_id
        run.result = result.to_json()
    return result
