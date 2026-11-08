"""The *validate* stage: judge an applied snapshot, and collect evidence for untagged substations.

Two kinds of checks run for every applied snapshot, oldest first, each in its own transaction:

* **Snapshot rules** decide the verdict. If an *error* rule fires the snapshot is *quarantined*:
  it stays in staging, so the history stays complete, but it is never promoted. The rules catch
  implausible deliveries: an empty state, too many objects vanishing at once, features outside
  Germany, too many repaired geometries.
* **Voltage hints** are evidence for substations that carry no usable voltage tag: the voltage of a
  line running within a short distance, and the OSM ``substation=*`` type. A hint never overwrites
  anything; contradicting hints are reported as a conflict. The review view turns them into a
  suggestion that still needs a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cabler import runs
from cabler.sources import SourceSpec

MAX_REMOVED_SHARE = 0.20
OUTSIDE_ERROR_SHARE = 0.01
REPAIRED_WARN_SHARE = 0.01
REPAIRED_ERROR_SHARE = 0.10
LINE_HINT_RADIUS_M = 50
# EPSG:25832 envelope around Germany with a margin; features outside it are certainly wrong.
GERMANY_ENVELOPE = (270_000, 5_200_000, 940_000, 6_120_000)

PENDING_SQL = """
SELECT v.version_id
FROM meta.source_version v
WHERE v.source_id = %s AND v.status = 'applied'
  AND NOT EXISTS (SELECT 1 FROM staging.snapshot_validation s WHERE s.version_id = v.version_id)
ORDER BY v.created_at, v.version_id
"""
LATEST_SQL = """
SELECT version_id
FROM staging.snapshot_validation
WHERE source_id = %s
ORDER BY version_id DESC
LIMIT 1
"""

TOTAL_SQL = "SELECT count(*) FROM staging.osm_power_as_of(%s, %s)"
BEFORE_SQL = """
SELECT count(*) FROM staging.osm_power_feature
WHERE source_id = %s AND valid_from_version_id < %s
  AND (valid_to_version_id IS NULL OR valid_to_version_id >= %s)
"""
REMOVED_SQL = """
SELECT count(*) FROM staging.osm_power_feature f
WHERE f.source_id = %s AND f.valid_to_version_id = %s
  AND NOT EXISTS (
      SELECT 1 FROM staging.osm_power_feature g
      WHERE g.source_id = f.source_id AND g.layer = f.layer AND g.osm_key = f.osm_key
        AND g.valid_from_version_id = %s)
"""
OUTSIDE_SQL = """
SELECT count(*), coalesce((array_agg(osm_key ORDER BY osm_key))[1:5], ARRAY[]::text[])
FROM staging.osm_power_as_of(%s, %s)
WHERE NOT (location && ST_MakeEnvelope(%s, %s, %s, %s, 25832))
"""
REPAIRED_SQL = """
SELECT count(*) FROM staging.osm_power_as_of(%s, %s) WHERE 'geometry_repaired' = ANY(qa_flags)
"""

LINE_HINT_SQL = """
INSERT INTO staging.validation_finding (version_id, rule, severity, feature_id, detail)
SELECT %(version)s, 'voltage_hint_line', 'info', u.feature_id,
       jsonb_build_object('hint_voltage_v', n.voltage_v, 'line_osm_key', n.osm_key,
                          'distance_m', round(n.distance::numeric, 1),
                          'substation_type', u.substation_type)
FROM staging.osm_power_as_of(%(source)s, %(version)s) u
CROSS JOIN LATERAL (
    SELECT l.osm_key, l.voltage_v, ST_Distance(l.geom, u.geom) AS distance
    FROM staging.osm_power_as_of(%(source)s, %(version)s) l
    WHERE l.kind IN ('line', 'cable') AND l.voltage_v IS NOT NULL
      AND ST_DWithin(l.geom, u.geom, %(radius)s)
    ORDER BY distance, l.voltage_v DESC
    LIMIT 1
) n
WHERE u.kind = 'substation' AND u.voltage_v IS NULL
"""
TYPE_HINT_SQL = """
INSERT INTO staging.validation_finding (version_id, rule, severity, feature_id, detail)
SELECT %(version)s, 'voltage_hint_type', 'info', u.feature_id,
       jsonb_build_object(
           'hint_class',
           CASE u.substation_type WHEN 'minor_distribution' THEN 'medium_low' ELSE 'high_plus' END,
           'substation_type', u.substation_type)
FROM staging.osm_power_as_of(%(source)s, %(version)s) u
WHERE u.kind = 'substation' AND u.voltage_v IS NULL
  AND u.substation_type IN ('minor_distribution', 'transmission')
"""
CONFLICT_SQL = """
INSERT INTO staging.validation_finding (version_id, rule, severity, feature_id, detail)
SELECT %(version)s, 'voltage_hint_conflict', 'warning', l.feature_id,
       jsonb_build_object('line_hint_v', (l.detail ->> 'hint_voltage_v')::integer,
                          'type_hint_class', t.detail ->> 'hint_class')
FROM staging.validation_finding l
JOIN staging.validation_finding t
  ON t.version_id = l.version_id AND t.feature_id = l.feature_id AND t.rule = 'voltage_hint_type'
WHERE l.version_id = %(version)s AND l.rule = 'voltage_hint_line'
  AND (((l.detail ->> 'hint_voltage_v')::integer >= 110000
        AND t.detail ->> 'hint_class' = 'medium_low')
       OR ((l.detail ->> 'hint_voltage_v')::integer < 110000
           AND t.detail ->> 'hint_class' = 'high_plus'))
"""
FINDING_SQL = """
INSERT INTO staging.validation_finding (version_id, rule, severity, detail)
VALUES (%s, %s, %s, %s)
"""
SUMMARY_SQL = """
SELECT rule, severity, count(*) FROM staging.validation_finding
WHERE version_id = %s GROUP BY rule, severity
"""
VERDICT_SQL = """
INSERT INTO staging.snapshot_validation (version_id, source_id, outcome, errors, warnings)
VALUES (%s, %s, %s, %s, %s)
"""
REVIEW_COUNTS_SQL = """
SELECT suggestion, count(*) FROM staging.substation_review WHERE source_id = %s GROUP BY suggestion
"""
REVIEW_ROWS_SQL = """
SELECT osm_key, suggestion, coalesce(name, ''), coalesce(operator, ''),
       coalesce(substation_type, ''), line_hint_v, line_hint_distance_m, type_hint_class,
       round(lon::numeric, 4), round(lat::numeric, 4)
FROM staging.substation_review
WHERE source_id = %s AND (%s::text IS NULL OR suggestion = %s)
ORDER BY suggestion, osm_key
LIMIT %s
"""


class ValidateError(Exception):
    """A snapshot cannot be validated: no applier exists for this source."""


def removal_severity(before: int, removed: int) -> str | None:
    """Error when more than :data:`MAX_REMOVED_SHARE` of the objects vanish in one snapshot."""
    if before <= 0:
        return None
    return "error" if removed / before > MAX_REMOVED_SHARE else None


def share_severity(count: int, total: int, *, warn: float, error: float) -> str | None:
    """Warning above ``warn`` and error above ``error`` as a share of ``total``."""
    if total <= 0 or count <= 0:
        return None
    share = count / total
    if share > error:
        return "error"
    if share > warn:
        return "warning"
    return None


@dataclass(frozen=True)
class ValidateStep:
    """The verdict on one snapshot and what was found."""

    version_id: int
    outcome: str  # passed | quarantined
    errors: int
    warnings: int
    total: int
    rules: dict[str, int]
    problems: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "outcome": self.outcome,
            "errors": self.errors,
            "warnings": self.warnings,
            "total": self.total,
            "rules": self.rules,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class ValidateResult:
    source_id: str
    status: str  # validated | skipped
    version_id: int | None
    steps: tuple[ValidateStep, ...]

    @property
    def quarantined(self) -> bool:
        return any(step.outcome == "quarantined" for step in self.steps)

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "version_id": self.version_id,
            "steps": [step.to_json() for step in self.steps],
        }


def _scalar(conn: psycopg.Connection[Any], query: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def _finding(
    conn: psycopg.Connection[Any],
    version_id: int,
    rule: str,
    severity: str,
    message: str,
    **detail: Any,
) -> str:
    conn.execute(FINDING_SQL, (version_id, rule, severity, Jsonb({"message": message, **detail})))
    return f"{rule}: {message}"


def _snapshot_rules(
    conn: psycopg.Connection[Any], source_id: str, version_id: int
) -> tuple[int, list[str]]:
    """Run the snapshot-level rules; return the number of current objects and the problems."""
    problems: list[str] = []
    total = _scalar(conn, TOTAL_SQL, (source_id, version_id))
    if total == 0:
        problems.append(
            _finding(conn, version_id, "empty_state", "error", "no power objects in this snapshot")
        )

    before = _scalar(conn, BEFORE_SQL, (source_id, version_id, version_id))
    removed = _scalar(conn, REMOVED_SQL, (source_id, version_id, version_id))
    if removal_severity(before, removed):
        message = (
            f"{removed:,} of {before:,} objects disappeared "
            f"({removed / before:.0%}; limit {MAX_REMOVED_SHARE:.0%})"
        )
        problems.append(
            _finding(
                conn, version_id, "removal_share", "error", message, before=before, removed=removed
            )
        )

    row = conn.execute(OUTSIDE_SQL, (source_id, version_id, *GERMANY_ENVELOPE)).fetchone()
    outside, examples = (int(row[0]), list(row[1])) if row else (0, [])
    severity = share_severity(outside, total, warn=0.0, error=OUTSIDE_ERROR_SHARE)
    if severity:
        message = (
            f"{outside:,} of {total:,} objects lie outside Germany (e.g. {', '.join(examples)})"
        )
        problems.append(
            _finding(conn, version_id, "outside_germany", severity, message, outside=outside)
        )

    repaired = _scalar(conn, REPAIRED_SQL, (source_id, version_id))
    severity = share_severity(repaired, total, warn=REPAIRED_WARN_SHARE, error=REPAIRED_ERROR_SHARE)
    if severity:
        message = f"{repaired:,} of {total:,} geometries had to be repaired"
        problems.append(
            _finding(conn, version_id, "geometry_repaired", severity, message, repaired=repaired)
        )
    return total, problems


def validate_version(
    conn: psycopg.Connection[Any], source_id: str, version_id: int
) -> ValidateStep:
    """Judge one applied snapshot and store the verdict. The caller owns the transaction."""
    total, problems = _snapshot_rules(conn, source_id, version_id)
    params = {"version": version_id, "source": source_id, "radius": LINE_HINT_RADIUS_M}
    conn.execute(LINE_HINT_SQL, params)
    conn.execute(TYPE_HINT_SQL, params)
    conn.execute(CONFLICT_SQL, params)

    rules: dict[str, int] = {}
    errors = warnings = 0
    for rule, severity, count in conn.execute(SUMMARY_SQL, (version_id,)).fetchall():
        rules[str(rule)] = rules.get(str(rule), 0) + int(count)
        if severity == "error":
            errors += int(count)
        elif severity == "warning":
            warnings += int(count)
    outcome = "quarantined" if errors else "passed"
    conn.execute(VERDICT_SQL, (version_id, source_id, outcome, errors, warnings))
    return ValidateStep(
        version_id=version_id,
        outcome=outcome,
        errors=errors,
        warnings=warnings,
        total=total,
        rules=dict(sorted(rules.items())),
        problems=tuple(problems),
    )


def validate_source(conn: psycopg.Connection[Any], spec: SourceSpec) -> ValidateResult:
    """Run the validate stage for one source: every applied snapshot without a verdict."""
    if spec.load_kind != "osm_pbf" or "power" not in spec.load_themes:
        msg = (
            f"{spec.source_id}: no validator for this source; only OSM sources with the power theme"
        )
        raise ValidateError(msg)
    with runs.recorded(conn, "validate", spec.source_id) as run:
        pending = [int(r[0]) for r in conn.execute(PENDING_SQL, (spec.source_id,)).fetchall()]
        steps: list[ValidateStep] = []
        for version_id in pending:
            with conn.transaction():
                steps.append(validate_version(conn, spec.source_id, version_id))
        if steps:
            result = ValidateResult(spec.source_id, "validated", steps[-1].version_id, tuple(steps))
        else:
            latest = conn.execute(LATEST_SQL, (spec.source_id,)).fetchone()
            result = ValidateResult(
                spec.source_id, "skipped", None if latest is None else int(latest[0]), ()
            )
        run.version_id = result.version_id
        run.result = result.to_json()
    return result


def review_counts(conn: psycopg.Connection[Any], source_id: str) -> dict[str, int]:
    """How many untagged-voltage substations fall under each suggestion."""
    rows = conn.execute(REVIEW_COUNTS_SQL, (source_id,)).fetchall()
    return {str(suggestion): int(count) for suggestion, count in sorted(rows)}


def review_rows(
    conn: psycopg.Connection[Any], source_id: str, suggestion: str | None = None, limit: int = 20
) -> list[tuple[Any, ...]]:
    """Rows of the review queue, optionally for one suggestion."""
    return conn.execute(REVIEW_ROWS_SQL, (source_id, suggestion, suggestion, limit)).fetchall()
