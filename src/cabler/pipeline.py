"""Command line for the acquisition pipeline.

Usage: python -m cabler.pipeline list
       python -m cabler.pipeline register {dev,staging,production}
"""

from __future__ import annotations

import argparse

from cabler import db, sources
from cabler.envgen import NAMES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="show the sources defined in config/sources")
    register = commands.add_parser("register", help="write sources and licences to the database")
    register.add_argument("name", choices=NAMES)
    args = parser.parse_args(argv)

    specs = sources.load_all()
    if args.action == "list":
        for spec in specs.values():
            print(f"{spec.source_id:<22} {spec.licence_id:<16} {spec.url}")
        return 0

    with db.connect("app", environ={"CABLER_ENV": args.name}) as conn:
        for spec in specs.values():
            created = sources.register(conn, spec)
            print(f"{'created' if created else 'updated':<8} {spec.source_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
