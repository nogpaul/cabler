"""The exit check of the data phase: every layer is judged on the same questions.

A layer is *loaded, valid, in the right CRS, with its lineage recorded* when, for every source in
it, the current rows pass these checks:

* ``geometry_missing``: a row without a geometry (an error, except for wind units, whose register
  has none for some units: a warning);
* ``crs_not_25832``: a geometry in another reference system (an error);
* ``geometry_invalid``: a geometry that is not valid after the repair of the apply stage (an error);
* ``outside_germany``: a geometry outside the envelope of Germany (a warning, an error above 1%);
* ``lineage_snapshot_missing`` / ``lineage_snapshot_unsealed``: a row whose snapshot is not in the
  catalogue, belongs to another source, has no checksum or was never applied (errors);
* ``licence_missing``: a source without licence or credit line (an error);
* ``verdict``: for the sources that have a validate stage, the newest applied snapshot needs a
  verdict, and it must be *passed* (an error);
* ``snapshot_file``: the file of the newest snapshot is still there (a warning);
* the quality flags that the apply stage set (``geometry_repaired``, ``area_mismatch``, ...) are
  counted and reported as warnings or information; they describe the data, they do not block.

An error fails the check, a warning is only reported. Every run is stored in ``meta.check_run`` and
``meta.check_result``, which refuse changes, so a later report can be compared with an older one.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cabler.validate import GERMANY_ENVELOPE, share_severity

# "error" or "warning" when any bad row counts, or (warn share, error share) of the total
Rule = str | tuple[float, float]

OUTSIDE_RULE: Rule = (0.0, 0.01)
FLAG_SEVERITY = {"geometry_repaired": "info"}  # a repair is what the apply stage is for


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

    @property
    def errors(self) -> int:
        return sum(r.severity == "error" for r in self.results)

    @property
    def warnings(self) -> int:
        return sum(r.severity == "warning" for r in self.results)

    @property
    def passed(self) -> bool:
        return self.errors == 0


def severity_of(bad: int, total: int, rule: Rule) -> str:
    """The severity of ``bad`` rows out of ``total`` under a rule."""
    if bad <= 0:
        return "ok"
    if isinstance(rule, tuple):
        return share_severity(bad, total, warn=rule[0], error=rule[1]) or "info"
    return rule


def scan_sql(layer: Layer, *, validity: bool) -> str:
    """One pass over the current rows of a layer, one line per source, all counts at once."""
    g = f"t.{layer.geom}"
    invalid = (
        f"count(*) FILTER (WHERE {g} IS NOT NULL AND NOT ST_IsValid({g}))" if validity else "0"
    )
    if layer.version:
        join = f"LEFT JOIN meta.source_version v ON v.version_id = t.{layer.version}"
        missing = "count(*) FILTER (WHERE v.version_id IS NULL OR v.source_id <> t.source_id)"
        unsealed = (
            "count(*) FILTER (WHERE v.version_id IS NOT NULL AND"
            " (v.sha256 IS NULL OR v.status NOT IN ('applied', 'quarantined')))"
        )
    else:
        join, missing, unsealed = "", "0", "0"
    extras = "".join(f", count(*) FILTER (WHERE {e.condition})" for e in layer.extras)
    return (
        f"SELECT t.source_id, count(*),"  # noqa: S608 (the pieces are constants of this module)
        f" count(*) FILTER (WHERE {g} IS NULL),"
        f" count(*) FILTER (WHERE {g} IS NOT NULL AND ST_SRID({g}) <> 25832),"
        f" {invalid},"
        f" count(*) FILTER (WHERE {g} IS NOT NULL AND NOT"
        f" ({g} && ST_MakeEnvelope({', '.join(str(x) for x in GERMANY_ENVELOPE)}, 25832))),"
        f" {missing}, {unsealed}{extras}"
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


def _scan_results(layer: Layer, rows: list[tuple[Any, ...]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    standard = (
        ("geometry_missing", layer.null_geom, "rows without a geometry"),
        ("crs_not_25832", "error", "geometries in another reference system"),
        ("geometry_invalid", "error", "geometries that are not valid"),
        ("outside_germany", OUTSIDE_RULE, "geometries outside the envelope of Germany"),
        ("lineage_snapshot_missing", "error", "rows whose snapshot is not in the catalogue"),
        ("lineage_snapshot_unsealed", "error", "rows whose snapshot has no checksum or no apply"),
    )
    for row in rows:
        source, total = str(row[0]), int(row[1])
        counts = [int(x) for x in row[2:]]
        for (name, rule, message), bad in zip(standard, counts[: len(standard)], strict=True):
            severity = severity_of(bad, total, rule)
            results.append(
                CheckResult(layer.name, source, name, severity, bad, total, message if bad else "")
            )
        for extra, bad in zip(layer.extras, counts[len(standard) :], strict=True):
            severity = severity_of(bad, total, extra.rule)
            results.append(
                CheckResult(
                    layer.name,
                    source,
                    extra.name,
                    severity,
                    bad,
                    total,
                    extra.message if bad else "",
                )
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


def run_checks(
    conn: psycopg.Connection[Any],
    layers: tuple[Layer, ...] = LAYERS,
    *,
    validity: bool = True,
    files: bool = True,
    raw: Path | None = None,
    exists: Callable[[str], bool] | None = None,
) -> Report:
    """Run every check over every layer. Nothing is written.

    The snapshot of a version is stored relative to the folder of the raw data (``raw``); ``exists``
    replaces the look at the disk, for tests.
    """
    if exists is None:

        def exists(path: str) -> bool:
            return (raw / path).exists() if raw is not None else os.path.exists(path)

    results: list[CheckResult] = []
    for layer in layers:
        rows = conn.execute(scan_sql(layer, validity=validity)).fetchall()
        if not rows:
            results.append(
                CheckResult(layer.name, "-", "layer_not_empty", "error", 1, 0, "no current rows")
            )
            continue
        results += _scan_results(layer, rows)
        totals = {str(r[0]): int(r[1]) for r in rows}
        flags = conn.execute(flags_sql(layer)).fetchall()
        results += _flag_results(layer, flags, totals)
        sources = sorted(totals)
        for (source,) in conn.execute(LICENCE_SQL, (sources,)).fetchall():
            results.append(
                CheckResult(
                    layer.name,
                    str(source),
                    "licence_missing",
                    "error",
                    1,
                    1,
                    "no licence or credit",
                )
            )
        if layer.verdict:
            for source, newest, judged, outcome in conn.execute(VERDICT_SQL, (sources,)).fetchall():
                results.append(verdict_result(layer.name, str(source), newest, judged, outcome))
        if files:
            for source, path in conn.execute(FILE_SQL, (sources,)).fetchall():
                present = path is not None and exists(str(path))
                message = "" if present else f"snapshot file is gone: {path}"
                severity = "ok" if present else "warning"
                results.append(
                    CheckResult(
                        layer.name,
                        str(source),
                        "snapshot_file",
                        severity,
                        0 if present else 1,
                        1,
                        message,
                    )
                )
    return Report(tuple(results))


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
                Jsonb(options),
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
    verdict = "PASSED" if report.passed else "FAILED"
    lines.append(f"exit check {verdict}: {report.errors} errors, {report.warnings} warnings")
    return lines
