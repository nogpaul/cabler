"""The *core* layer for substations: what routing may use, how sure we are, and who decided.

A build reads the newest validated snapshot of each OSM source, adds the current human decisions
(:mod:`core.review_decision`, append-only) and puts every substation into three nested scenarios:

* **strict**: a voltage tag of at least ``high_voltage_v``, or a person said ``high_voltage``;
* **likely**: strict, plus substations without a voltage tag whose hints point to high voltage;
* **inclusive**: likely, plus every substation whose voltage is unknown.

A person's decision beats a tag and a hint. A substation with a tag below the limit is in no
scenario.

Review effort follows the impact of a decision. The *impact* of a substation is the number of wind
units, within ``reach_m``, for which it lies closer than that unit's nearest strict substation:
calling it high voltage would change that unit's nearest substation (a straight-line proxy for
"would change a route"). Every undecided substation without a voltage tag gets a tier: **D** no
impact, so no decision about it can change a route; otherwise by evidence **A** the hints conflict
or point to high voltage, **B** the hints agree it is not high voltage, **C** no evidence. The
review queue lists A and C, most impact first.

The audit sample is a stratified sample of the untagged substations that have a hint and a wind
unit within reach, drawn by a fixed hash and marked. Nothing a person decides moves it (not the
tier, not a decision), so the people who check it label a fixed group, and that is how the
precision of the rules is measured without bias.

The core is rebuildable: a build is a function of the staging tables, the decisions and the rule
set, and its content hash leaves out every database-generated number, so a rebuild from the raw
data must give the same hash.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any

import psycopg

DECISIONS = ("high_voltage", "not_high_voltage", "not_a_substation")
AUDIT_HINTS = ("hv_candidate", "conflict", "likely_low_voltage")


class CoreError(Exception):
    """A core build or a decision that cannot be done."""


@dataclass(frozen=True)
class RuleSet:
    """The parameters of one build. A version is never changed; a change is a new version."""

    version: str
    states: dict[str, str] = field(default_factory=dict)  # OSM source id -> state key of MaStR
    high_voltage_v: int = 110_000
    reach_m: int = 30_000
    wind_statuses: tuple[str, ...] = ("In Betrieb", "In Planung")
    wind_bad_flags: tuple[str, ...] = (
        "coordinate_outside_state",
        "coordinate_outside_germany",
        "coordinate_placeholder",
    )
    audit_share: float = 0.05
    audit_min: int = 30
    audit_seed: str = "cabler-audit-1"

    def parameters(self) -> dict[str, Any]:
        return {
            "states": dict(sorted(self.states.items())),
            "high_voltage_v": self.high_voltage_v,
            "reach_m": self.reach_m,
            "wind_statuses": list(self.wind_statuses),
            "wind_bad_flags": list(self.wind_bad_flags),
            "audit_share": self.audit_share,
            "audit_min": self.audit_min,
            "audit_seed": self.audit_seed,
        }


DEFAULT_RULES = RuleSet(version="1.0.0", states={"osm_nrw": "05", "osm_hamburg": "02"})


@dataclass(frozen=True)
class Classification:
    in_strict: bool
    in_likely: bool
    in_inclusive: bool
    reason: str
    voltage_source: str


def classify(
    voltage_v: int | None, decision: str | None, hint: str | None, high_voltage_v: int
) -> Classification:
    """The scenarios of one substation. The truth table is in the tests."""
    if decision == "high_voltage":
        return Classification(True, True, True, "decision_high", "decision")
    if decision == "not_high_voltage":
        return Classification(False, False, False, "decision_not_high", "decision")
    if decision == "not_a_substation":
        return Classification(False, False, False, "decision_not_a_substation", "decision")
    if voltage_v is not None:
        high = voltage_v >= high_voltage_v
        return Classification(high, high, high, "tag_high" if high else "tag_low", "tag")
    if hint == "hv_candidate":
        return Classification(False, True, True, "hint_candidate", "none")
    if hint == "conflict":
        return Classification(False, False, True, "hint_conflict", "none")
    if hint == "likely_low_voltage":
        return Classification(False, False, True, "hint_low", "none")
    return Classification(False, False, True, "unknown", "none")


def tier_of(
    voltage_v: int | None, decision: str | None, hint: str | None, impact: int
) -> str | None:
    """The review tier of a substation, or None when a tag or a decision settles it.

    Relevance comes first: without impact no decision can change a route. Evidence sets the tier
    of the rest.
    """
    if decision is not None or voltage_v is not None:
        return None
    if impact == 0:
        return "D"
    if hint in ("conflict", "hv_candidate"):
        return "A"
    if hint == "likely_low_voltage":
        return "B"
    return "C"


@dataclass(frozen=True)
class Row:
    """One substation of a build, with everything the core table stores."""

    feature_id: int
    source_id: str
    layer: str
    osm_key: str
    name: str | None
    operator: str | None
    substation_type: str | None
    voltage_v: int | None
    hint: str | None
    decision: str | None
    decision_id: int | None
    classification: Classification
    tier: str | None
    in_reach: int
    impact: int
    audit_sample: bool
    location_ewkb: bytes
    location_wkt: str

    def stable(self) -> list[Any]:
        """The content of the row without any number the database generated."""
        c = self.classification
        return [
            self.source_id,
            self.layer,
            self.osm_key,
            self.name,
            self.operator,
            self.substation_type,
            self.voltage_v,
            c.voltage_source,
            self.hint,
            self.decision,
            c.in_strict,
            c.in_likely,
            c.in_inclusive,
            c.reason,
            self.tier,
            self.in_reach,
            self.impact,
            self.audit_sample,
            self.location_wkt,
        ]


def audit_sample(rows: list[Row], rules: RuleSet) -> set[tuple[str, str, str]]:
    """Keys of the substations drawn for audit: per source and hint, the first by a fixed hash.

    Eligible are the untagged substations that have a hint and a wind unit within reach. Neither
    the tier nor a decision takes part, so recording a decision does not move the sample.
    """
    strata: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        eligible = row.voltage_v is None and row.in_reach > 0 and row.hint in AUDIT_HINTS
        if eligible:
            strata.setdefault((row.source_id, str(row.hint)), []).append(row)

    def rank(row: Row) -> str:
        text = f"{rules.audit_seed}/{row.source_id}/{row.layer}/{row.osm_key}"
        return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()

    chosen: set[tuple[str, str, str]] = set()
    for members in strata.values():
        size = min(len(members), max(rules.audit_min, math.ceil(rules.audit_share * len(members))))
        for row in sorted(members, key=rank)[:size]:
            chosen.add((row.source_id, row.layer, row.osm_key))
    return chosen


def content_hash(rows: list[Row]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: (r.source_id, r.layer, r.osm_key)):
        digest.update(json.dumps(row.stable(), ensure_ascii=True).encode())
        digest.update(b"\n")
    return digest.hexdigest()


RULE_SQL = "SELECT rule_set_id, parameters FROM core.rule_set WHERE version = %s"
RULE_INSERT_SQL = (
    "INSERT INTO core.rule_set (version, parameters) VALUES (%s, %s::jsonb) RETURNING rule_set_id"
)
VERDICT_SQL = """
SELECT version_id, outcome FROM staging.snapshot_validation
WHERE source_id = %s ORDER BY version_id DESC LIMIT 1
"""
# One query per source. The CTEs are MATERIALIZED on purpose: the application role may not create
# temporary tables, and an inlined CTE would be evaluated again for every pair of rows.
FACTS_SQL = """
WITH sub AS MATERIALIZED (
    SELECT p.feature_id, p.source_id, p.layer, p.osm_key, p.name, p.operator, p.substation_type,
           p.voltage_v, h.suggestion AS hint, d.decision, d.decision_id, p.location
    FROM staging.osm_power_as_of(%(source)s, %(version)s) p
    LEFT JOIN staging.substation_review h ON h.feature_id = p.feature_id
    LEFT JOIN core.review_decision_current d
           ON d.source_id = p.source_id AND d.layer = p.layer AND d.osm_key = p.osm_key
    WHERE p.kind = 'substation'
), strict AS MATERIALIZED (
    SELECT location FROM sub
    WHERE decision = 'high_voltage' OR (decision IS NULL AND voltage_v >= %(high)s)
), nearest AS MATERIALIZED (
    SELECT w.geom, (SELECT min(ST_Distance(s.location, w.geom)) FROM strict s) AS d_strict
    FROM staging.wind_unit_current w
    WHERE w.state_key = %(state)s AND w.status = ANY(%(statuses)s) AND w.geom IS NOT NULL
      AND NOT (w.qa_flags && %(bad_flags)s::text[])
)
SELECT s.feature_id, s.source_id, s.layer, s.osm_key, s.name, s.operator, s.substation_type,
       s.voltage_v, s.hint, s.decision, s.decision_id,
       ST_AsEWKB(s.location), ST_AsText(s.location, 3), r.in_reach, r.impact
FROM sub s
CROSS JOIN LATERAL (
    SELECT count(*) AS in_reach,
           count(*) FILTER (
               WHERE n.d_strict IS NULL OR ST_Distance(s.location, n.geom) < n.d_strict
           ) AS impact
    FROM nearest n WHERE ST_DWithin(s.location, n.geom, %(reach)s)
) r
ORDER BY s.layer, s.osm_key
"""
# The newest version that opened or closed a unit, so that retiring units changes it too.
WIND_VERSION_SQL = """
SELECT coalesce(max(greatest(valid_from_version_id, coalesce(valid_to_version_id, 0))), 0)
FROM staging.wind_unit WHERE state_key = ANY(%s)
"""
BUILD_SQL = """
INSERT INTO core.build (rule_set_id, source_versions, decisions_through, counts, content_hash)
VALUES (%s, %s::jsonb, %s, %s::jsonb, %s) RETURNING build_id
"""
INSERT_SQL = """
INSERT INTO core.substation
    (build_id, source_id, layer, osm_key, feature_id, name, operator, substation_type, voltage_v,
     voltage_source, hint, decision, decision_id, in_strict, in_likely, in_inclusive, reason, tier,
     wind_units_in_reach, impact, audit_sample, location)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        ST_GeomFromEWKB(%s))
"""


def _returned_id(row: tuple[Any, ...] | None) -> int:
    """The id that an INSERT ... RETURNING must have given back."""
    if row is None:
        msg = "the database returned no row"
        raise CoreError(msg)
    return int(row[0])


def register_rules(conn: psycopg.Connection[Any], rules: RuleSet) -> int:
    """The id of the rule set; a version that exists with other parameters is refused."""
    wanted = rules.parameters()
    row = conn.execute(RULE_SQL, (rules.version,)).fetchone()
    if row is None:
        created = conn.execute(RULE_INSERT_SQL, (rules.version, json.dumps(wanted))).fetchone()
        return _returned_id(created)
    if row[1] != wanted:
        msg = f"rule set {rules.version} exists with other parameters; use a new version"
        raise CoreError(msg)
    return int(row[0])


def _facts(
    conn: psycopg.Connection[Any], rules: RuleSet, source_id: str, state: str, version: int
) -> list[Row]:
    params = {
        "source": source_id,
        "version": version,
        "high": rules.high_voltage_v,
        "state": state,
        "statuses": list(rules.wind_statuses),
        "bad_flags": list(rules.wind_bad_flags),
        "reach": rules.reach_m,
    }
    rows = []
    for f in conn.execute(FACTS_SQL, params).fetchall():
        voltage_v, hint, decision = f[7], f[8], f[9]
        in_reach, impact = int(f[13]), int(f[14])
        rows.append(
            Row(
                feature_id=int(f[0]),
                source_id=str(f[1]),
                layer=str(f[2]),
                osm_key=str(f[3]),
                name=f[4],
                operator=f[5],
                substation_type=f[6],
                voltage_v=voltage_v,
                hint=hint,
                decision=decision,
                decision_id=f[10],
                classification=classify(voltage_v, decision, hint, rules.high_voltage_v),
                tier=tier_of(voltage_v, decision, hint, impact),
                in_reach=in_reach,
                impact=impact,
                audit_sample=False,
                location_ewkb=bytes(f[11]),
                location_wkt=str(f[12]),
            )
        )
    return rows


@dataclass(frozen=True)
class BuildResult:
    build_id: int
    content_hash: str
    counts: dict[str, int]
    source_versions: dict[str, int]


def _counts(rows: list[Row]) -> dict[str, int]:
    counts = {
        "substations": len(rows),
        "strict": sum(r.classification.in_strict for r in rows),
        "likely": sum(r.classification.in_likely for r in rows),
        "inclusive": sum(r.classification.in_inclusive for r in rows),
        "audit_sample": sum(r.audit_sample for r in rows),
    }
    for tier in ("A", "B", "C", "D"):
        counts[f"tier_{tier}"] = sum(r.tier == tier for r in rows)
    return counts


def _insert_values(build_id: int, r: Row) -> tuple[Any, ...]:
    c = r.classification
    return (
        build_id,
        r.source_id,
        r.layer,
        r.osm_key,
        r.feature_id,
        r.name,
        r.operator,
        r.substation_type,
        r.voltage_v,
        c.voltage_source,
        r.hint,
        r.decision,
        r.decision_id,
        c.in_strict,
        c.in_likely,
        c.in_inclusive,
        c.reason,
        r.tier,
        r.in_reach,
        r.impact,
        r.audit_sample,
        r.location_ewkb,
    )


def validated_versions(conn: psycopg.Connection[Any], rules: RuleSet) -> dict[str, int]:
    """The newest validated snapshot of each source of the rule set.

    A source without a verdict, or whose newest snapshot was quarantined, stops the work.
    """
    if not rules.states:
        msg = "the rule set names no source"
        raise CoreError(msg)
    versions: dict[str, int] = {}
    for source_id in sorted(rules.states):
        verdict = conn.execute(VERDICT_SQL, (source_id,)).fetchone()
        if verdict is None:
            msg = f"{source_id}: no validated snapshot; run validate first"
            raise CoreError(msg)
        if verdict[1] != "passed":
            msg = f"{source_id}: the newest snapshot ({verdict[0]}) was quarantined"
            raise CoreError(msg)
        versions[source_id] = int(verdict[0])
    return versions


def build_substations(conn: psycopg.Connection[Any], rules: RuleSet = DEFAULT_RULES) -> BuildResult:
    """Build the substations of the core from the newest validated snapshots and the decisions."""
    with conn.transaction():
        versions = validated_versions(conn, rules)
        rule_set_id = register_rules(conn, rules)
        rows: list[Row] = []
        for source_id in sorted(rules.states):
            rows += _facts(conn, rules, source_id, rules.states[source_id], versions[source_id])
        # the newest decision that this build applied, read from the same rows it used
        decisions_through = max((r.decision_id or 0 for r in rows), default=0)
        chosen = audit_sample(rows, rules)
        rows = [replace(r, audit_sample=(r.source_id, r.layer, r.osm_key) in chosen) for r in rows]
        wind_version = conn.execute(WIND_VERSION_SQL, (sorted(rules.states.values()),)).fetchone()
        versions["wind_units"] = int(wind_version[0]) if wind_version else 0
        counts = _counts(rows)
        digest = content_hash(rows)
        built = conn.execute(
            BUILD_SQL,
            (rule_set_id, json.dumps(versions), decisions_through, json.dumps(counts), digest),
        ).fetchone()
        build_id = _returned_id(built)
        with conn.cursor() as cursor:
            cursor.executemany(INSERT_SQL, [_insert_values(build_id, r) for r in rows])
    return BuildResult(build_id, digest, counts, versions)


SUBJECT_SQL = """
SELECT 1 FROM staging.osm_power_current
WHERE source_id = %s AND layer = %s AND osm_key = %s AND kind = 'substation'
"""
NEWEST_SQL = """
SELECT max(decision_id) FROM core.review_decision
WHERE source_id = %s AND layer = %s AND osm_key = %s
"""
DECIDE_SQL = """
INSERT INTO core.review_decision
    (source_id, layer, osm_key, decision, reason, reviewer, evidence, rule_set_id,
     supersedes_decision_id)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING decision_id
"""


def record_decision(
    conn: psycopg.Connection[Any],
    source_id: str,
    layer: str,
    osm_key: str,
    decision: str,
    *,
    reason: str,
    reviewer: str,
    evidence: dict[str, Any] | None = None,
    rules: RuleSet = DEFAULT_RULES,
) -> int:
    """Append one decision about a substation; it replaces the newest one for that substation."""
    if decision not in DECISIONS:
        msg = f"decision must be one of {', '.join(DECISIONS)}"
        raise CoreError(msg)
    if not reason.strip() or not reviewer.strip():
        msg = "a decision needs a reason and a reviewer"
        raise CoreError(msg)
    try:
        with conn.transaction():
            if conn.execute(SUBJECT_SQL, (source_id, layer, osm_key)).fetchone() is None:
                msg = f"{source_id}/{layer}/{osm_key} is not a current substation"
                raise CoreError(msg)
            newest = conn.execute(NEWEST_SQL, (source_id, layer, osm_key)).fetchone()
            created = conn.execute(
                DECIDE_SQL,
                (
                    source_id,
                    layer,
                    osm_key,
                    decision,
                    reason,
                    reviewer,
                    json.dumps(evidence or {}),
                    register_rules(conn, rules),
                    newest[0] if newest else None,
                ),
            ).fetchone()
    except psycopg.errors.CheckViolation as exc:
        if "must replace the newest" not in str(exc):
            raise
        msg = f"another decision about {source_id}/{layer}/{osm_key} was recorded first; look again"
        raise CoreError(msg) from exc
    return _returned_id(created)


QUEUE_SQL = """
SELECT source_id, layer, osm_key, coalesce(name, ''), tier, impact, wind_units_in_reach,
       coalesce(hint, ''), audit_sample, round(lon::numeric, 5), round(lat::numeric, 5)
FROM core.review_queue
WHERE (%(tier)s::text IS NULL OR tier = %(tier)s::text)
ORDER BY (tier = 'A') DESC NULLS LAST, impact DESC, source_id, layer, osm_key
LIMIT %(limit)s
"""


def queue_rows(
    conn: psycopg.Connection[Any], tier: str | None, limit: int
) -> list[tuple[Any, ...]]:
    return conn.execute(QUEUE_SQL, {"tier": tier, "limit": limit}).fetchall()
