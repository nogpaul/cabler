"""Generate ``env/<name>.env`` from its ``.env.example`` template with random secrets.

Usage: python -m cabler.envgen dev [--force]
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parents[2] / "env"
PLACEHOLDER = "__GENERATE__"
NAMES = ("dev", "staging", "production")


def render(template: str) -> str:
    """Replace every placeholder with its own random secret."""
    lines: list[str] = []
    for line in template.splitlines():
        while PLACEHOLDER in line:
            line = line.replace(PLACEHOLDER, secrets.token_urlsafe(24), 1)
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=NAMES)
    parser.add_argument("--force", action="store_true", help="overwrite an existing env file")
    args = parser.parse_args(argv)

    template_path = ENV_DIR / f"{args.name}.env.example"
    target = ENV_DIR / f"{args.name}.env"
    if target.exists() and not args.force:
        print(f"{target} already exists; use --force to replace it.", file=sys.stderr)
        return 1
    target.write_text(render(template_path.read_text(encoding="utf-8")), encoding="utf-8")
    target.chmod(0o600)
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
