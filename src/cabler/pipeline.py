"""Command line for the acquisition pipeline.

Usage: python -m cabler.pipeline list
       python -m cabler.pipeline register {dev,staging,production}
       python -m cabler.pipeline check {dev,staging,production} [SOURCE ...] [--json]
       python -m cabler.pipeline fetch {dev,staging,production} SOURCE ... [--yes] [--force]
       python -m cabler.pipeline load {dev,staging,production} SOURCE ...
       python -m cabler.pipeline diff {dev,staging,production} SOURCE ...
       python -m cabler.pipeline apply {dev,staging,production} SOURCE ...
       python -m cabler.pipeline tiles {dev,staging,production} SOURCE
           [--yes] [--limit N] [--workers N]
       python -m cabler.pipeline validate {dev,staging,production} SOURCE ...
       python -m cabler.pipeline review {dev,staging,production} SOURCE
           [--suggestion S] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from cabler import apply, checks, db, diff, fetch, load, remote, sources, tiles, validate
from cabler.apply import ApplyError
from cabler.diff import DiffError
from cabler.envgen import NAMES
from cabler.load import LoadError
from cabler.remote import RemoteError
from cabler.sources import SourceSpec
from cabler.tiles import TilesError
from cabler.validate import ValidateError


def _select(specs: dict[str, SourceSpec], wanted: list[str]) -> list[SourceSpec]:
    if not wanted:
        return list(specs.values())
    unknown = [w for w in wanted if w not in specs]
    if unknown:
        known = ", ".join(sorted(specs))
        raise SystemExit(f"unknown source(s): {', '.join(unknown)}; known sources: {known}")
    return [specs[w] for w in wanted]


def _require_registered(
    conn: psycopg.Connection[Any], selected: list[SourceSpec], env: str
) -> None:
    ids = [spec.source_id for spec in selected]
    rows = conn.execute("SELECT source_id FROM meta.source WHERE source_id = ANY(%s)", (ids,))
    missing = sorted(set(ids) - {row[0] for row in rows.fetchall()})
    if missing:
        raise SystemExit(
            f"not registered in {env}: {', '.join(missing)}. "
            f"Run: python -m cabler.pipeline register {env}"
        )


def _describe(result: checks.CheckResult) -> str:
    state = result.remote
    modified = state.last_modified.isoformat() if state.last_modified else "-"
    return (
        f"{result.source_id:<18} {result.outcome:<13} etag={state.etag or '-'}  "
        f"modified={modified}  size={state.content_length or '-'}"
    )


def _run_checks(env: str, selected: list[SourceSpec], as_json: bool) -> int:
    results: list[checks.CheckResult] = []
    failures = 0
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, selected, env)
        with remote.make_client() as client:
            for spec in selected:
                try:
                    result = checks.check_source(conn, client, spec)
                except RemoteError as exc:
                    failures += 1
                    print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
                    continue
                results.append(result)
                if not as_json:
                    print(_describe(result))
    if as_json:
        print(json.dumps([r.to_json() for r in results], indent=2))
    return 1 if failures else 0


def _human(size: int | None) -> str:
    if size is None:
        return "unknown size"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1000
    return f"{size} B"


def _run_fetch(env: str, selected: list[SourceSpec], *, yes: bool, force: bool) -> int:
    failures = 0
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, selected, env)
        with remote.make_client() as client:
            for spec in selected:
                try:
                    if not yes:
                        plan = checks.check_source(conn, client, spec)
                        wanted = force or plan.outcome != "unchanged"
                        action = "would download" if wanted else "up to date, nothing to download"
                        size = _human(plan.remote.content_length)
                        print(f"{spec.source_id:<18} {action} ({size}) from {plan.remote.url}")
                        continue
                    result = fetch.fetch_source(conn, client, spec, fetch.raw_dir(env), force=force)
                except RemoteError as exc:
                    failures += 1
                    print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
                    continue
                where = result.path or "-"
                shown = "up to date" if result.status == "skipped" else _human(result.size)
                print(
                    f"{result.status:<10} {spec.source_id:<18} {shown}  "
                    f"sha256={(result.sha256 or '-')[:12]}  {where}"
                )
    if not yes and not failures:
        print("Nothing was downloaded. Add --yes to download for real.")
    return 1 if failures else 0


def _run_load(env: str, selected: list[SourceSpec]) -> int:
    failures = 0
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, selected, env)
        for spec in selected:
            try:
                result = load.load_source(conn, spec, fetch.raw_dir(env))
            except (RemoteError, LoadError) as exc:
                failures += 1
                print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
                continue
            counts = ", ".join(f"{k} {n:,}" for k, n in result.counts.items()) or "nothing new"
            print(f"{result.status:<10} {spec.source_id:<18} {counts}")
    return 1 if failures else 0


def _run_diff(env: str, selected: list[SourceSpec]) -> int:
    failures = 0
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, selected, env)
        for spec in selected:
            try:
                result = diff.diff_source(conn, spec)
            except DiffError as exc:
                failures += 1
                print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
                continue
            if not result.steps:
                print(f"{result.status:<10} {spec.source_id:<18} nothing new to compare")
            for step in result.steps:
                if step.baseline:
                    summary = f"first snapshot: {step.added:,} objects"
                else:
                    summary = (
                        f"+{step.added:,} added  ~{step.changed:,} changed  "
                        f"-{step.removed:,} removed  "
                        f"(of {step.total_before:,} before, {step.total_after:,} now)"
                    )
                print(f"{result.status:<10} {spec.source_id:<18} {summary}")
    return 1 if failures else 0


def _run_apply(env: str, selected: list[SourceSpec]) -> int:
    failures = 0
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, selected, env)
        for spec in selected:
            try:
                result = apply.apply_source(conn, spec)
            except ApplyError as exc:
                failures += 1
                print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
                continue
            if not result.steps:
                print(f"{result.status:<10} {spec.source_id:<18} nothing new to apply")
            for step in result.steps:
                print(
                    f"{result.status:<10} {spec.source_id:<18} "
                    f"+{step.opened:,} opened  -{step.closed:,} closed  "
                    f"({step.current:,} current)"
                )
            if result.steps:
                last = result.steps[-1]
                kinds = ", ".join(f"{k} {n:,}" for k, n in last.kinds.items())
                flags = ", ".join(f"{k} {n:,}" for k, n in last.flags.items()) or "none"
                print(f"{'':<29}kinds: {kinds}")
                print(f"{'':<29}findings: {flags}")
    return 1 if failures else 0


def _run_validate(env: str, selected: list[SourceSpec]) -> int:
    failures = 0
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, selected, env)
        for spec in selected:
            try:
                result = validate.validate_source(conn, spec)
            except ValidateError as exc:
                failures += 1
                print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
                continue
            if not result.steps:
                print(f"{result.status:<10} {spec.source_id:<18} nothing new to validate")
            for step in result.steps:
                verdict = "PASSED" if step.outcome == "passed" else "QUARANTINED"
                print(
                    f"{result.status:<10} {spec.source_id:<18} {verdict}  "
                    f"{step.errors} errors, {step.warnings} warnings  ({step.total:,} objects)"
                )
                for problem in step.problems:
                    print(f"{'':<29}problem: {problem}")
                if step.rules:
                    found = ", ".join(f"{k} {n:,}" for k, n in step.rules.items())
                    print(f"{'':<29}findings: {found}")
                if step.outcome == "quarantined":
                    failures += 1
    return 1 if failures else 0


def _run_review(env: str, spec: SourceSpec, suggestion: str | None, limit: int) -> int:
    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        counts = validate.review_counts(conn, spec.source_id)
        if not counts:
            print(f"{spec.source_id}: nothing to review (validate the source first)")
            return 0
        summary = ", ".join(f"{k} {n:,}" for k, n in counts.items())
        print(f"{spec.source_id}: substations without a tagged voltage: {summary}")
        for row in validate.review_rows(conn, spec.source_id, suggestion, limit):
            key, kind, name, operator, subtype, line_v, line_d, type_hint, lon, lat = row
            evidence = []
            if line_v is not None:
                evidence.append(f"line {line_v:,} V at {line_d} m")
            if type_hint:
                evidence.append(f"type {subtype or '-'} -> {type_hint}")
            print(
                f"  {key:<14} {kind:<18} {name or operator or '-':<28.28} "
                f"{'; '.join(evidence) or 'no evidence':<44} {lon},{lat}"
            )
    return 0


def _run_tiles(env: str, spec: SourceSpec, *, yes: bool, limit: int | None, workers: int) -> int:
    def progress(done: int, total: int, fetched: int) -> None:
        if done % 500 == 0 or done == total:
            print(f"  {done:,} of {total:,} tiles  ({_human(fetched)})", flush=True)

    with db.connect("app", environ={"CABLER_ENV": env}) as conn:
        conn.autocommit = True
        _require_registered(conn, [spec], env)
        try:
            with remote.make_client() as client:
                result = tiles.fetch_tiles(
                    conn,
                    client,
                    spec,
                    fetch.raw_dir(env),
                    yes=yes,
                    limit=limit,
                    workers=workers,
                    on_progress=progress,
                )
        except (RemoteError, TilesError) as exc:
            print(f"ERROR {spec.source_id}: {exc}", file=sys.stderr)
            return 1
    print(
        f"{spec.source_id}: {result.listed:,} tiles listed; {result.new:,} new, "
        f"{result.changed:,} changed, {result.unchanged:,} unchanged, {result.removed:,} removed"
    )
    if not yes:
        wanted = result.new + result.changed
        print(f"Would download {wanted:,} tiles ({_human(result.planned_bytes)}).")
        print("Nothing was downloaded. Add --yes to download for real.")
        return 0
    got = _human(result.bytes_fetched)
    print(f"fetched {result.fetched:,} tiles ({got}); {result.failed:,} failed")
    for failure in result.failures:
        print(f"  FAILED {failure}", file=sys.stderr)
    return 1 if result.failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="show the sources defined in config/sources")
    register = commands.add_parser("register", help="write sources and licences to the database")
    register.add_argument("name", choices=NAMES)
    check = commands.add_parser("check", help="ask publishers whether data changed (no download)")
    check.add_argument("name", choices=NAMES)
    check.add_argument("sources", nargs="*", help="source ids (default: all)")
    check.add_argument("--json", action="store_true", help="print machine-readable results")
    fetch_cmd = commands.add_parser("fetch", help="download changed sources as immutable snapshots")
    fetch_cmd.add_argument("name", choices=NAMES)
    fetch_cmd.add_argument("sources", nargs="+", help="source ids to download")
    fetch_cmd.add_argument(
        "--yes", action="store_true", help="really download (default: plan only)"
    )
    fetch_cmd.add_argument("--force", action="store_true", help="download even if unchanged")
    load_cmd = commands.add_parser("load", help="load fetched snapshots into the raw schema")
    load_cmd.add_argument("name", choices=NAMES)
    load_cmd.add_argument("sources", nargs="+", help="source ids to load")
    diff_cmd = commands.add_parser("diff", help="compare loaded snapshots with their predecessors")
    diff_cmd.add_argument("name", choices=NAMES)
    diff_cmd.add_argument("sources", nargs="+", help="source ids to diff")
    apply_cmd = commands.add_parser("apply", help="apply diffed snapshots to the staging state")
    apply_cmd.add_argument("name", choices=NAMES)
    apply_cmd.add_argument("sources", nargs="+", help="source ids to apply")
    validate_cmd = commands.add_parser("validate", help="judge applied snapshots")
    validate_cmd.add_argument("name", choices=NAMES)
    validate_cmd.add_argument("sources", nargs="+", help="source ids to validate")
    review_cmd = commands.add_parser("review", help="list substations needing a voltage decision")
    review_cmd.add_argument("name", choices=NAMES)
    review_cmd.add_argument("source", help="source id")
    review_cmd.add_argument("--suggestion", help="only this suggestion")
    review_cmd.add_argument("--limit", type=int, default=20, help="rows to show")
    tiles_cmd = commands.add_parser("tiles", help="fetch the tiles listed by an index source")
    tiles_cmd.add_argument("name", choices=NAMES)
    tiles_cmd.add_argument("source", help="source id")
    tiles_cmd.add_argument("--yes", action="store_true", help="really download")
    tiles_cmd.add_argument("--limit", type=int, help="only the first N missing tiles")
    tiles_cmd.add_argument("--workers", type=int, default=6, help="parallel downloads")
    args = parser.parse_args(argv)

    specs = sources.load_all()
    if args.action == "list":
        for spec in specs.values():
            print(f"{spec.source_id:<22} {spec.licence_id:<16} {spec.url}")
        return 0

    if args.action == "check":
        return _run_checks(args.name, _select(specs, args.sources), args.json)

    if args.action == "fetch":
        selected = _select(specs, args.sources)
        return _run_fetch(args.name, selected, yes=args.yes, force=args.force)

    if args.action == "load":
        return _run_load(args.name, _select(specs, args.sources))

    if args.action == "diff":
        return _run_diff(args.name, _select(specs, args.sources))

    if args.action == "apply":
        return _run_apply(args.name, _select(specs, args.sources))

    if args.action == "tiles":
        (tile_spec,) = _select(specs, [args.source])
        return _run_tiles(
            args.name, tile_spec, yes=args.yes, limit=args.limit, workers=args.workers
        )

    if args.action == "validate":
        return _run_validate(args.name, _select(specs, args.sources))

    if args.action == "review":
        (spec,) = _select(specs, [args.source])
        return _run_review(args.name, spec, args.suggestion, args.limit)

    with db.connect("app", environ={"CABLER_ENV": args.name}) as conn:
        for spec in specs.values():
            created = sources.register(conn, spec)
            print(f"{'created' if created else 'updated':<8} {spec.source_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
