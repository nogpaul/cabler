"""How good are the hint rules? Measured two ways, each with its own limit.

The hints of :mod:`cabler.validate` (a nearby line with a voltage, the substation type) turn an
untagged substation into ``hv_candidate``, ``conflict``, ``likely_low_voltage`` or
``no_evidence``. Nobody knows the true voltage of an untagged substation, so the rules are measured
on two groups that do have an answer:

* **Leave-tag-out.** Every substation that carries a voltage tag is judged by the same rules as if
  it had none, and the answer is compared with the tag. Plenty of cases, but they describe *tagged*
  substations, and those are mapped more carefully than the untagged ones.
* **Audit labels.** The audit sample (:func:`cabler.core.audit_sample`) is a random draw from the
  untagged substations with a hint. What people decide about it is the true label. Few cases, but
  the draw is random within each hint, so the interval is valid for the untagged substations in
  reach of a wind unit.

Every share comes with a Wilson score interval (95%), which stays honest for small counts and for
shares near 0 or 1, where the usual normal interval does not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import psycopg

from cabler import core
from cabler.core import DEFAULT_RULES, RuleSet
from cabler.validate import LINE_HINT_RADIUS_M

# The validate rules compare the voltage of a nearby line with this value.
HINT_VOLTAGE_V = 110_000
SUGGESTIONS = ("hv_candidate", "conflict", "likely_low_voltage", "no_evidence")
Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for k successes in n trials; (0, 1) when nothing was counted."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    scale = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / scale
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / scale
    low = 0.0 if k == 0 else max(0.0, centre - half)
    high = 1.0 if k == n else min(1.0, centre + half)
    return (low, high)


@dataclass(frozen=True)
class Cell:
    """The result for one rule outcome of one source."""

    source_id: str
    suggestion: str
    n: int  # substations counted
    n_high: int  # of them at or above the limit (a tag, or a person's decision)
    n_not_a_substation: int = 0  # audit only: people said it is no substation (not in n)
    n_unlabelled: int = 0  # audit only: drawn, but nobody has decided yet (not in n)

    @property
    def share(self) -> float | None:
        return self.n_high / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.n_high, self.n)


@dataclass(frozen=True)
class Evaluation:
    versions: dict[str, int]
    leave_out: list[Cell] = field(default_factory=list)
    audit: list[Cell] = field(default_factory=list)
    build_id: int | None = None  # the build whose audit sample was read
    high_voltage_v: int = 110_000  # the limit that 'high' meant


# The hint of every substation of a snapshot, tagged or not, by the rules of cabler.validate:
# the nearest line with a voltage within the radius, and the class of the substation type.
HINTS_SQL = """
SELECT u.layer, u.osm_key, u.voltage_v,
       CASE
           WHEN h.conflict THEN 'conflict'
           WHEN h.line_v >= %(hint_v)s OR h.type_class = 'high_plus' THEN 'hv_candidate'
           WHEN h.type_class = 'medium_low' OR h.line_v IS NOT NULL THEN 'likely_low_voltage'
           ELSE 'no_evidence'
       END AS suggestion
FROM staging.osm_power_as_of(%(source)s, %(version)s) u
CROSS JOIN LATERAL (
    SELECT n.line_v, c.type_class,
           coalesce((n.line_v >= %(hint_v)s AND c.type_class = 'medium_low')
                 OR (n.line_v < %(hint_v)s AND c.type_class = 'high_plus'), false) AS conflict
    FROM (SELECT CASE u.substation_type WHEN 'minor_distribution' THEN 'medium_low'
                                        WHEN 'transmission' THEN 'high_plus' END AS type_class) c
    LEFT JOIN LATERAL (
        SELECT l.voltage_v AS line_v
        FROM staging.osm_power_as_of(%(source)s, %(version)s) l
        WHERE l.kind IN ('line', 'cable') AND l.voltage_v IS NOT NULL
          AND ST_DWithin(l.geom, u.geom, %(radius)s)
        ORDER BY ST_Distance(l.geom, u.geom), l.voltage_v DESC
        LIMIT 1
    ) n ON true
) h
WHERE u.kind = 'substation'
"""
LEAVE_OUT_SQL = f"""
WITH hints AS ({HINTS_SQL})
SELECT suggestion, count(*), count(*) FILTER (WHERE voltage_v >= %(high)s)
FROM hints WHERE voltage_v IS NOT NULL GROUP BY suggestion
"""  # noqa: S608
BUILD_SQL = "SELECT max(build_id) FROM core.build"
AUDIT_SQL = """
SELECT s.hint,
       count(*) FILTER (WHERE d.decision IN ('high_voltage', 'not_high_voltage')),
       count(*) FILTER (WHERE d.decision = 'high_voltage'),
       count(*) FILTER (WHERE d.decision = 'not_a_substation'),
       count(*) FILTER (WHERE d.decision_id IS NULL)
FROM core.substation_current s
LEFT JOIN core.review_decision_current d
       ON d.source_id = s.source_id AND d.layer = s.layer AND d.osm_key = s.osm_key
WHERE s.source_id = %s AND s.audit_sample
GROUP BY s.hint
"""


def hints(
    conn: psycopg.Connection[Any], source_id: str, version: int
) -> dict[tuple[str, str], tuple[str, int | None]]:
    """(layer, key) -> (hint, voltage tag) for every substation of a snapshot."""
    params = {
        "source": source_id,
        "version": version,
        "hint_v": HINT_VOLTAGE_V,
        "radius": LINE_HINT_RADIUS_M,
    }
    return {(r[0], r[1]): (r[3], r[2]) for r in conn.execute(HINTS_SQL, params).fetchall()}


def evaluate_rules(conn: psycopg.Connection[Any], rules: RuleSet = DEFAULT_RULES) -> Evaluation:
    """Measure the hint rules on the newest validated snapshots and on the audit labels."""
    versions = core.validated_versions(conn, rules)
    leave_out: list[Cell] = []
    audit: list[Cell] = []
    build = conn.execute(BUILD_SQL).fetchone()
    build_id = int(build[0]) if build and build[0] is not None else None
    for source_id in sorted(rules.states):
        params = {
            "source": source_id,
            "version": versions[source_id],
            "hint_v": HINT_VOLTAGE_V,
            "radius": LINE_HINT_RADIUS_M,
            "high": rules.high_voltage_v,
        }
        counted = {r[0]: (int(r[1]), int(r[2])) for r in conn.execute(LEAVE_OUT_SQL, params)}
        for suggestion in SUGGESTIONS:
            n, high = counted.get(suggestion, (0, 0))
            leave_out.append(Cell(source_id, suggestion, n, high))
        if build_id is None:
            continue
        drawn = {r[0]: r[1:] for r in conn.execute(AUDIT_SQL, (source_id,)).fetchall()}
        for suggestion in SUGGESTIONS:
            if suggestion in drawn:
                n, high, other, open_ = (int(x) for x in drawn[suggestion])
                audit.append(Cell(source_id, suggestion, n, high, other, open_))
    return Evaluation(versions, leave_out, audit, build_id, rules.high_voltage_v)


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _line(cell: Cell, *, audit: bool) -> str:
    low, high = cell.interval
    interval = "-" if cell.n == 0 else f"{_percent(low)} to {_percent(high)}"
    text = (
        f"  {cell.suggestion:<20} {cell.n:>7,} {cell.n_high:>7,} "
        f"{_percent(cell.share):>8}  {interval:<17}"
    )
    if audit:
        text += f" {cell.n_not_a_substation:>5,} {cell.n_unlabelled:>10,}"
    return text


def format_report(result: Evaluation) -> list[str]:
    """The evaluation as lines of text."""
    lines = [
        "rule evaluation, snapshots "
        + ", ".join(f"{k} {v}" for k, v in sorted(result.versions.items())),
        f"high means a voltage of at least {result.high_voltage_v:,} V; "
        "the interval is a 95% Wilson one",
        "",
        "leave-tag-out: tagged substations judged by the rules as if they had no tag",
        "(describes tagged substations only)",
    ]
    header = f"  {'rule':<20} {'n':>7} {'high':>7} {'share':>8}  {'95% interval':<17}"
    for source_id in sorted({c.source_id for c in result.leave_out}):
        lines += ["", source_id, header]
        lines += [_line(c, audit=False) for c in result.leave_out if c.source_id == source_id]
    lines += ["", "audit labels: what people decided about the audit sample"]
    if result.build_id is None:
        lines.append("  no core build yet; run: core")
        return lines
    lines[-1] += f" (core build {result.build_id})"
    header = (
        f"  {'rule':<20} {'labelled':>7} {'high':>7} {'share':>8}  {'95% interval':<17}"
        f" {'other':>5} {'unlabelled':>10}"
    )
    for source_id in sorted({c.source_id for c in result.audit}):
        lines += ["", source_id, header]
        lines += [_line(c, audit=True) for c in result.audit if c.source_id == source_id]
    if not result.audit:
        lines.append("  the audit sample is empty")
    return lines
