"""The exit check of the data phase: every layer is judged on the same questions.

A layer is *loaded, valid, in the right CRS, with its lineage recorded* when, for every source in
it, the current rows pass these checks:

* ``geometry_missing``: a row without a geometry, or with an empty one (an error, except for wind
  units, whose register has none for some units: a warning);
* ``crs_not_25832``: a geometry in another reference system (an error);
* ``geometry_invalid``: a geometry that is not valid after the repair of the apply stage (an error;
  with ``validity=False`` it is stored as *skipped*, never as ok, and the report is *partial*);
* ``outside_germany``: a geometry that is not completely inside the envelope of Germany (a warning,
  an error above 1%);
* ``lineage_snapshot_missing`` / ``lineage_snapshot_unsealed``: a row whose snapshot is not in the
  catalogue, belongs to another source, has no checksum or was never applied (errors); a layer
  without a column for the snapshot (the terrain tiles) has no such check and no result for it;
* ``source_missing``: a source of the registry whose theme belongs to the layer and that has an
  applied snapshot, but no current row in the layer (an error);
* ``licence_missing``: a source without licence or credit line (an error);
* ``verdict``: for the sources that have a validate stage, the newest applied snapshot needs a
  verdict, and it must be *passed* (an error);
* ``snapshot_file``: the file of the newest snapshot is still there (a warning);
* the quality flags that the apply stage set (``geometry_repaired``, ``area_mismatch``, ...) are
  counted and reported as warnings or information; they describe the data, they do not block.

An error fails the check, a warning is only reported. All of it is read in one REPEATABLE READ
transaction, so the report describes one state of the data. Every run is stored in
``meta.check_run`` and ``meta.check_result``, which refuse changes, so a later report can be
compared with an older one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cabler.db import ISOLATION_SQL, repeatable_read
from cabler.sources import SourceSpec
from cabler.validate import GERMANY_ENVELOPE, share_severity

# "error" or "warning" when any bad row counts, or (warn share, error share) of the total
Rule = str | tuple[float, float]

OUTSIDE_RULE: Rule = (0.0, 0.01)
FLAG_SEVERITY = {"geometry_repaired": "info"}  # a repair is what the apply stage is for
# which layer holds the data of a source with this theme (terrain tiles have no theme)
THEME_LAYERS = {
    "power": "osm_power",
    "wind": "wind_unit",
    "land_cover": "land_cover",
    "nature_reserve": "protected_area",
    "habitat_area": "protected_area",
    "bird_area": "protected_area",
    "national_park": "protected_area",
    "landscape_area": "protected_area",
    "parcel": "parcel",
    "municipality": "admin_area",
    "district": "admin_area",
}


@dataclass(frozen=True)
class Extra:
    """A check that only one layer has: rows that satisfy ``condition`` are bad."""

    name: str
    condition: str
    rule: Rule
    message: str


@dataclass(frozen=True)
class Layer:
    name: str
    table: str
    geom: str
    current: str  # the condition that selects the current rows of the table alias t
    version: str | None  # the column with the snapshot that created the row
    null_geom: Rule = "error"
    verdict: bool = False  # the sources of the layer have a validate stage
    extras: tuple[Extra, ...] = ()


LAYERS = (
    Layer(
        "osm_power",
        "staging.osm_power_feature",
        "geom",
        "t.valid_to_version_id IS NULL",
        "valid_from_version_id",
        verdict=True,
    ),
    Layer(
        "wind_unit",
        "staging.wind_unit",
        "geom",
        "t.valid_to_version_id IS NULL",
        "valid_from_version_id",
        null_geom="warning",
        verdict=True,
    ),
    Layer(
        "land_cover",
        "staging.land_cover",
        "geom",
        "t.valid_to_version_id IS NULL",
        "valid_from_version_id",
        extras=(
            Extra(
                "clc_code_unknown",
                "t.clc_code IS NULL OR NOT EXISTS"
                " (SELECT 1 FROM staging.clc_class c WHERE c.code = t.clc_code)",
                "error",
                "land cover code that is not in the CORINE legend",
            ),
        ),
    ),
    Layer(
        "protected_area",
        "staging.protected_area",
        "geom",
        "t.valid_to_version_id IS NULL",
        "valid_from_version_id",
    ),
    Layer(
        "parcel",
        "staging.parcel",
        "geom",
        "t.valid_to_version_id IS NULL",
        "valid_from_version_id",
    ),
    Layer(
        "admin_area",
        "staging.admin_area",
        "geom",
        "t.valid_to_version_id IS NULL",
        "valid_from_version_id",
        extras=(Extra("area_key_missing", "t.area_key IS NULL", "error", "area without its key"),),
    ),
    Layer(
        "terrain_tile",
        "staging.terrain_tile",
        "footprint",
        "t.is_current",
        None,
        extras=(
            Extra(
                "tile_not_25832",
                "t.epsg IS DISTINCT FROM 25832",
                "error",
                "tile in another reference system",
            ),
        ),
    ),
)


@dataclass(frozen=True)
class CheckResult:
    layer: str
    source_id: str
    check: str
    severity: str  # ok | info | warning | error
    bad: int
    total: int
    message: str = ""


@dataclass(frozen=True)
class Report:
    results: tuple[CheckResult, ...]
    validity: bool = True  # False: the validity scan of the geometries was not run
    isolation: str = "unknown"  # the isolation level that the check read at

    @property
    def errors(self) -> int:
        return sum(r.severity == "error" for r in self.results)

    @property
    def warnings(self) -> int:
        return sum(r.severity == "warning" for r in self.results)

    @property
    def passed(self) -> bool:
        return self.errors == 0

    @property
    def partial(self) -> bool:
        """A check that skipped something cannot say that everything is fine."""
        return not self.validity


@dataclass(frozen=True)
class Check:
    """One count of the scan: the rows that satisfy ``condition`` are bad. No condition: not run."""

    name: str
    condition: str | None
    rule: Rule
    message: str


def severity_of(bad: int, total: int, rule: Rule) -> str:
    """The severity of ``bad`` rows out of ``total`` under a rule."""
    if bad <= 0:
        return "ok"
    if isinstance(rule, tuple):
        return share_severity(bad, total, warn=rule[0], error=rule[1]) or "info"
    return rule


def checks_of(layer: Layer, *, validity: bool) -> list[Check]:
    """The checks of a layer, in the order of the columns of its scan.

    It is the one list that both the SQL and the results are made from, so that no count can be
    assigned to the wrong check.
    """
    g = f"t.{layer.geom}"
    box = ", ".join(str(x) for x in GERMANY_ENVELOPE)
    checks = [
        Check(
            "geometry_missing",
            f"{g} IS NULL OR ST_IsEmpty({g})",  # an empty geometry is of no more use than none
            layer.null_geom,
            "rows without a geometry (none, or an empty one)",
        ),
        Check(
            "crs_not_25832",
            f"{g} IS NOT NULL AND ST_SRID({g}) <> 25832",
            "error",
            "geometries in another reference system",
        ),
        Check(
            "geometry_invalid",
            f"{g} IS NOT NULL AND NOT ST_IsValid({g})" if validity else None,
            "error",
            "geometries that are not valid",
        ),
        Check(
            "outside_germany",
            # the box of the geometry inside the box of Germany: for a rectangle that is containment
            f"{g} IS NOT NULL AND NOT ({g} @ ST_MakeEnvelope({box}, 25832))",
            OUTSIDE_RULE,
            "geometries not completely inside the envelope of Germany",
        ),
    ]
    if layer.version:
        checks += [
            Check(
                "lineage_snapshot_missing",
                "v.version_id IS NULL OR v.source_id <> t.source_id",
                "error",
                "rows whose snapshot is not in the catalogue",
            ),
            Check(
                "lineage_snapshot_unsealed",
                "v.version_id IS NOT NULL AND"
                " (v.sha256 IS NULL OR v.status NOT IN ('applied', 'quarantined'))",
                "error",
                "rows whose snapshot has no checksum or no apply",
            ),
        ]
    checks += [Check(e.name, e.condition, e.rule, e.message) for e in layer.extras]
    return checks


def scan_sql(layer: Layer, *, validity: bool) -> str:
    """One pass over the current rows of a layer, one line per source, all counts at once."""
    columns = "".join(
        f", count(*) FILTER (WHERE {c.condition})"
        for c in checks_of(layer, validity=validity)
        if c.condition
    )
    join = (
        f"LEFT JOIN meta.source_version v ON v.version_id = t.{layer.version}"
        if layer.version
        else ""
    )
    return (
        f"SELECT t.source_id, count(*){columns}"  # noqa: S608 (constants of this module)
        f" FROM {layer.table} t {join} WHERE {layer.current} GROUP BY t.source_id ORDER BY 1"
    )


def flags_sql(layer: Layer) -> str:
    return (
        f"SELECT t.source_id, f, count(*) FROM {layer.table} t,"  # noqa: S608
        f" unnest(t.qa_flags) AS f WHERE {layer.current} GROUP BY t.source_id, f ORDER BY 1, 2"
    )


LICENCE_SQL = """
SELECT source_id FROM meta.source
WHERE source_id = ANY(%s)
  AND (btrim(licence_id) = '' OR btrim(licence_url) = '' OR btrim(attribution) = '')
"""
APPLIED_SQL = """
SELECT DISTINCT source_id FROM meta.source_version
WHERE source_id = ANY(%s) AND status IN ('applied', 'quarantined')
"""
VERDICT_SQL = """
SELECT s.source_id,
       (SELECT max(version_id) FROM meta.source_version
        WHERE source_id = s.source_id AND status IN ('applied', 'quarantined')),
       (SELECT max(version_id) FROM staging.snapshot_validation WHERE source_id = s.source_id),
       (SELECT outcome FROM staging.snapshot_validation
        WHERE source_id = s.source_id ORDER BY version_id DESC LIMIT 1)
FROM meta.source s WHERE s.source_id = ANY(%s)
"""
FILE_SQL = """
SELECT DISTINCT ON (source_id) source_id, local_path FROM meta.source_version
WHERE source_id = ANY(%s) AND status IN ('applied', 'quarantined')
ORDER BY source_id, version_id DESC
"""


def expected_sources(specs: Iterable[SourceSpec]) -> dict[str, frozenset[str]]:
    """The sources of the registry that belong to each layer, by their themes."""
    found: dict[str, set[str]] = {}
    for spec in specs:
        for theme in spec.load_themes:
            layer = THEME_LAYERS.get(theme)
            if layer:
                found.setdefault(layer, set()).add(spec.source_id)
    return {layer: frozenset(sources) for layer, sources in found.items()}


def _scan_results(
    layer: Layer, rows: list[tuple[Any, ...]], *, validity: bool = True
) -> list[CheckResult]:
    results: list[CheckResult] = []
    checks = checks_of(layer, validity=validity)
    names = [c.name for c in checks if c.condition]
    for row in rows:
        source, total = str(row[0]), int(row[1])
        counts = dict(zip(names, (int(x) for x in row[2:]), strict=True))
        for check in checks:
            if check.condition is None:
                skipped = "skipped: the validity scan was not run"
                results.append(
                    CheckResult(layer.name, source, check.name, "info", 0, total, skipped)
                )
                continue
            bad = counts[check.name]
            severity = severity_of(bad, total, check.rule)
            message = check.message if bad else ""
            results.append(
                CheckResult(layer.name, source, check.name, severity, bad, total, message)
            )
    return results


def _flag_results(
    layer: Layer, rows: list[tuple[Any, ...]], totals: dict[str, int]
) -> list[CheckResult]:
    return [
        CheckResult(
            layer.name,
            str(source),
            f"flag:{flag}",
            FLAG_SEVERITY.get(str(flag), "warning"),
            int(count),
            totals[str(source)],
            "rows carrying this quality flag",
        )
        for source, flag, count in rows
    ]


def verdict_result(
    layer: str, source: str, newest: int | None, judged: int | None, outcome: str | None
) -> CheckResult:
    """Newest applied snapshot judged and passed, or an error saying what is missing."""
    if judged is None or newest is None or judged < newest:
        message = "the newest applied snapshot has no verdict; run validate"
        return CheckResult(layer, source, "verdict", "error", 1, 1, message)
    if outcome != "passed":
        message = f"the newest verdict is '{outcome}'"
        return CheckResult(layer, source, "verdict", "error", 1, 1, message)
    return CheckResult(layer, source, "verdict", "ok", 0, 1)


def _missing_sources(
    conn: psycopg.Connection[Any], layer: Layer, wanted: Iterable[str], present: set[str]
) -> list[CheckResult]:
    """Sources that were applied but have no current row in the layer."""
    names = sorted(wanted)
    if not names:
        return []
    applied = {str(r[0]) for r in conn.execute(APPLIED_SQL, (names,)).fetchall()}
    return [
        CheckResult(
            layer.name,
            source,
            "source_missing",
            "error",
            1,
            1,
            "the source has an applied snapshot but no current row in this layer",
        )
        for source in sorted(applied - present)
    ]


def run_checks(
    conn: psycopg.Connection[Any],
    layers: tuple[Layer, ...] = LAYERS,
    *,
    validity: bool = True,
    files: bool = True,
    raw: Path | None = None,
    exists: Callable[[str], bool] | None = None,
    expected: dict[str, frozenset[str]] | None = None,
) -> Report:
    """Run every check over every layer in one snapshot. Nothing is written.

    The snapshot of a version is stored relative to the folder of the raw data (``raw``); ``exists``
    replaces the look at the disk, for tests. ``expected`` names the sources that each layer must
    hold (see :func:`expected_sources`).
    """
    if exists is None:

        def exists(path: str) -> bool:
            return (raw / path).exists() if raw is not None else os.path.exists(path)

    results: list[CheckResult] = []
    with repeatable_read(conn), conn.transaction():
        level = conn.execute(ISOLATION_SQL).fetchone()
        isolation = str(level[0]) if level else "unknown"
        for layer in layers:
            rows = conn.execute(scan_sql(layer, validity=validity)).fetchall()
            wanted = (expected or {}).get(layer.name, frozenset())
            if not rows:
                results.append(
                    CheckResult(
                        layer.name, "-", "layer_not_empty", "error", 1, 0, "no current rows"
                    )
                )
                results += _missing_sources(conn, layer, wanted, set())
                continue
            results += _scan_results(layer, rows, validity=validity)
            totals = {str(r[0]): int(r[1]) for r in rows}
            results += _missing_sources(conn, layer, wanted, set(totals))
            flags = conn.execute(flags_sql(layer)).fetchall()
            results += _flag_results(layer, flags, totals)
            sources = sorted(totals)
            for (source,) in conn.execute(LICENCE_SQL, (sources,)).fetchall():
                message = "no licence or credit"
                results.append(
                    CheckResult(layer.name, str(source), "licence_missing", "error", 1, 1, message)
                )
            if layer.verdict:
                verdicts = conn.execute(VERDICT_SQL, (sources,)).fetchall()
                for source, newest, judged, outcome in verdicts:
                    results.append(verdict_result(layer.name, str(source), newest, judged, outcome))
            if files:
                for source, path in conn.execute(FILE_SQL, (sources,)).fetchall():
                    present = path is not None and exists(str(path))
                    message = "" if present else f"snapshot file is gone: {path}"
                    severity = "ok" if present else "warning"
                    bad = 0 if present else 1
                    results.append(
                        CheckResult(
                            layer.name, str(source), "snapshot_file", severity, bad, 1, message
                        )
                    )
    return Report(tuple(results), validity=validity, isolation=isolation)


RUN_SQL = """
INSERT INTO meta.check_run (outcome, errors, warnings, options)
VALUES (%s, %s, %s, %s) RETURNING check_run_id
"""
RESULT_SQL = """
INSERT INTO meta.check_result
    (check_run_id, layer, source_id, check_name, severity, bad, total, message)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def store_report(conn: psycopg.Connection[Any], report: Report, options: dict[str, Any]) -> int:
    """Write the report as one run with its results; returns the run id."""
    with conn.transaction():
        row = conn.execute(
            RUN_SQL,
            (
                "passed" if report.passed else "failed",
                report.errors,
                report.warnings,
                Jsonb({**options, "partial": report.partial, "isolation": report.isolation}),
            ),
        ).fetchone()
        if row is None:
            msg = "the database returned no run id"
            raise RuntimeError(msg)
        run_id = int(row[0])
        with conn.cursor() as cursor:
            cursor.executemany(
                RESULT_SQL,
                [
                    (run_id, r.layer, r.source_id, r.check, r.severity, r.bad, r.total, r.message)
                    for r in report.results
                ],
            )
    return run_id


def format_report(report: Report) -> list[str]:
    """One block per layer, one line per source, then what is not ok."""
    lines: list[str] = []
    by_layer: dict[str, dict[str, list[CheckResult]]] = {}
    for r in report.results:
        by_layer.setdefault(r.layer, {}).setdefault(r.source_id, []).append(r)
    for layer, sources in by_layer.items():
        lines.append(layer)
        for source, results in sources.items():
            total = max((r.total for r in results), default=0)
            worst = (
                "error"
                if any(r.severity == "error" for r in results)
                else "warning"
                if any(r.severity == "warning" for r in results)
                else "ok"
            )
            lines.append(f"  {source:<24} {total:>11,} rows  {worst.upper()}")
            for r in results:
                if r.severity == "ok":
                    continue
                share = f" ({r.bad / r.total:.1%})" if r.total > 1 and r.bad else ""
                counts = f"{r.bad:>11,} of {r.total:>11,}{share}"
                head = f"    {r.severity.upper():<8} {r.check:<28} {counts}"
                lines.append(f"{head}  {r.message}".rstrip())
    if report.isolation == "read committed":
        lines.append("note: the check read at 'read committed', not from one snapshot")
    verdict = "PASSED" if report.passed else "FAILED"
    note = " (partial: the validity scan was skipped)" if report.partial else ""
    lines.append(f"exit check {verdict}{note}: {report.errors} errors, {report.warnings} warnings")
    return lines
