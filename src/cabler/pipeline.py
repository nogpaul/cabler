"""Command line for the acquisition pipeline.

Usage: python -m cabler.pipeline list
       python -m cabler.pipeline register {dev,staging,production}
       python -m cabler.pipeline check {dev,staging,production} [SOURCE ...] [--json]
       python -m cabler.pipeline fetch {dev,staging,production} SOURCE ... [--yes] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from cabler import checks, db, fetch, remote, sources
from cabler.envgen import NAMES
from cabler.remote import RemoteError
from cabler.sources import SourceSpec


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

    with db.connect("app", environ={"CABLER_ENV": args.name}) as conn:
        for spec in specs.values():
            created = sources.register(conn, spec)
            print(f"{'created' if created else 'updated':<8} {spec.source_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
