"""Database connection helpers.

Connection settings come from environment variables. If ``CABLER_ENV`` is set
(``dev``, ``staging`` or ``production``) the matching ``env/<name>.env`` file
is read first, and real environment variables override it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import psycopg

Role = Literal["owner", "app", "ro"]

ROLE_NAMES: Mapping[Role, str] = {
    "owner": "cabler_owner",
    "app": "cabler_app",
    "ro": "cabler_ro",
}
ROLE_PASSWORD_VARS: Mapping[Role, str] = {
    "owner": "CABLER_OWNER_PASSWORD",
    "app": "CABLER_APP_PASSWORD",
    "ro": "CABLER_RO_PASSWORD",
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` file. Blank lines and ``#`` comments are ignored."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def resolve_settings(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge the selected env file (if any) with real environment variables."""
    environ = dict(os.environ if environ is None else environ)
    settings: dict[str, str] = {}
    env_name = environ.get("CABLER_ENV")
    if env_name:
        env_file = REPO_ROOT / "env" / f"{env_name}.env"
        if not env_file.is_file():
            msg = f"CABLER_ENV={env_name!r} but {env_file} does not exist"
            raise FileNotFoundError(msg)
        settings.update(load_env_file(env_file))
    settings.update(environ)
    return settings


def connect(
    role: Role = "app", environ: Mapping[str, str] | None = None
) -> psycopg.Connection[Any]:
    """Open a connection as one of the three application roles."""
    settings = resolve_settings(environ)
    password_var = ROLE_PASSWORD_VARS[role]
    missing = [k for k in ("DB_HOST", "DB_PORT", "DB_NAME", password_var) if k not in settings]
    if missing:
        msg = f"Missing settings: {', '.join(missing)}"
        raise KeyError(msg)
    return psycopg.connect(
        host=settings["DB_HOST"],
        port=int(settings["DB_PORT"]),
        dbname=settings["DB_NAME"],
        user=ROLE_NAMES[role],
        password=settings[password_var],
        connect_timeout=10,
    )
