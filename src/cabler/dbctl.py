"""Manage the database container of one environment.

Usage: python -m cabler.dbctl {up,down,status,logs,psql,destroy} {dev,staging,production}
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from cabler.envgen import NAMES

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker" / "compose.yaml"
DISPOSABLE = frozenset({"dev"})
ACTIONS = ("up", "down", "status", "logs", "psql", "destroy")
PSQL_SHELL = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'


def compose_command(name: str, *args: str) -> list[str]:
    """Build the ``docker compose`` command line for one environment."""
    env_file = ROOT / "env" / f"{name}.env"
    return ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE_FILE), *args]


def compose(name: str, *args: str) -> int:
    """Run ``docker compose`` for an environment and return its exit code."""
    env_file = ROOT / "env" / f"{name}.env"
    if not env_file.is_file():
        print(f"{env_file} missing. Run: python -m cabler.envgen {name}", file=sys.stderr)
        return 2
    return subprocess.run(compose_command(name, *args), check=False).returncode  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("name", choices=NAMES)
    parser.add_argument(
        "--yes-delete-data",
        action="store_true",
        help="required for 'destroy' outside disposable environments",
    )
    args = parser.parse_args(argv)

    commands: dict[str, tuple[str, ...]] = {
        "up": ("up", "-d", "--build", "--wait"),
        "down": ("down",),
        "status": ("ps",),
        "logs": ("logs", "--tail", "100", "db"),
        "psql": ("exec", "db", "sh", "-c", PSQL_SHELL),
    }
    if args.action in commands:
        return compose(args.name, *commands[args.action])

    # destroy: removes the container AND its data volume.
    if args.name not in DISPOSABLE and not args.yes_delete_data:
        print(
            "Refusing: 'destroy' outside a disposable environment needs --yes-delete-data.",
            file=sys.stderr,
        )
        return 3
    return compose(args.name, "down", "--volumes")


if __name__ == "__main__":
    raise SystemExit(main())
