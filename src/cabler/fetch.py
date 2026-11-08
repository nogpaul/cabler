"""The *fetch* stage: download a source, verify it, and keep it as an immutable snapshot.

A snapshot is a folder ``<raw>/<source_id>/<UTC time>_<sha256[:12]>/`` holding the file exactly as
delivered plus a ``manifest.json``. Files are made read-only and are never executed. A download
that fails, is too large or is incomplete leaves nothing behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg

from cabler import __version__, checks, db, remote, runs
from cabler.remote import RemoteError, RemoteState, TooLargeError
from cabler.sources import SourceSpec

CHUNK_SIZE = 1024 * 1024
FREE_SPACE_FACTOR = 2
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class FetchError(RemoteError):
    """Base class for problems while fetching."""


class DownloadError(FetchError):
    """The bytes received do not match what the publisher announced."""


class NotEnoughSpaceError(FetchError):
    """The data disk has too little free space for this download."""


@dataclass(frozen=True)
class FetchResult:
    source_id: str
    status: str  # fetched | skipped | duplicate
    version_id: int | None
    path: str | None
    sha256: str | None
    size: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "version_id": self.version_id,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class Downloaded:
    path: Path
    sha256: str
    size: int
    state: RemoteState


def data_root() -> Path:
    """Where downloaded data lives: ``$CABLER_DATA_DIR`` or ``<projects>/data`` beside the repo."""
    configured = os.environ.get("CABLER_DATA_DIR")
    return Path(configured) if configured else db.REPO_ROOT.parent / "data"


def raw_dir(env: str) -> Path:
    """The folder holding the immutable snapshots of one environment."""
    return data_root() / env / "raw"


def filename_for(spec: SourceSpec, final_url: str) -> str:
    """A safe file name with an extension from the final URL, or one derived from the source id."""
    path = urlsplit(final_url).path
    name = "" if path.endswith("/") else Path(path).name
    if SAFE_NAME.match(name) and "." in name:
        return name
    return f"{spec.source_id}.{spec.file_format}"


def ensure_space(directory: Path, needed: int) -> None:
    """Refuse a download unless the disk has room for it twice over."""
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    if free < needed * FREE_SPACE_FACTOR:
        msg = f"only {free} bytes free in {directory}; {needed * FREE_SPACE_FACTOR} are needed"
        raise NotEnoughSpaceError(msg)


def download(client: httpx.Client, spec: SourceSpec, directory: Path) -> Downloaded:
    """Stream the file into ``directory`` while hashing it and enforcing the size limit."""
    response = remote.send(client, "GET", spec, stream=True)
    try:
        state = remote.state_of(response, spec)
        target = directory / filename_for(spec, state.url)
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_raw(CHUNK_SIZE):
                    size += len(chunk)
                    if size > spec.max_bytes:
                        msg = f"{spec.url}: more than {spec.max_bytes} bytes received; stopped"
                        raise TooLargeError(msg)
                    digest.update(chunk)
                    handle.write(chunk)
        except httpx.TransportError as exc:
            msg = f"{spec.url}: the connection broke after {size} bytes ({type(exc).__name__})"
            raise DownloadError(msg) from exc
    finally:
        response.close()
    if state.content_length is not None and size != state.content_length:
        msg = f"{spec.url}: incomplete download, got {size} of {state.content_length} bytes"
        raise DownloadError(msg)
    return Downloaded(target, digest.hexdigest(), size, state)


DUPLICATE_SQL = "SELECT version_id FROM meta.source_version WHERE source_id = %s AND sha256 = %s"
INSERT_SQL = """
INSERT INTO meta.source_version
    (source_id, remote_url, remote_etag, remote_last_modified, content_length, sha256,
     local_path, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, 'fetched')
RETURNING version_id
"""
REFRESH_SQL = """
UPDATE meta.source_version
SET remote_url = %s, remote_etag = %s, remote_last_modified = %s, content_length = %s
WHERE version_id = %s
"""


def _write_manifest(
    directory: Path, spec: SourceSpec, got: Downloaded, fetched_at: datetime
) -> None:
    modified = got.state.last_modified
    manifest = {
        "source_id": spec.source_id,
        "requested_url": spec.url,
        "final_url": got.state.url,
        "file": got.path.name,
        "size": got.size,
        "sha256": got.sha256,
        "etag": got.state.etag,
        "last_modified": modified.isoformat() if modified else None,
        "fetched_at": fetched_at.isoformat(),
        "fetched_by": f"cabler-pipeline/{__version__}",
        "licence_id": spec.licence_id,
        "attribution": spec.attribution,
    }
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (directory / "manifest.json").write_text(text, encoding="utf-8")


def _fetch_new(
    conn: psycopg.Connection[Any],
    client: httpx.Client,
    spec: SourceSpec,
    raw: Path,
    state: RemoteState,
) -> FetchResult:
    ensure_space(raw, state.content_length or spec.max_bytes)
    parent = raw / spec.source_id
    staging = parent / f".partial-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        got = download(client, spec, staging)
        modified = got.state.last_modified
        existing = conn.execute(DUPLICATE_SQL, (spec.source_id, got.sha256)).fetchone()
        if existing is not None:
            # Same bytes as a snapshot we already hold: keep one copy, refresh the validators so
            # the next check can say "unchanged".
            version_id = int(existing[0])
            conn.execute(
                REFRESH_SQL,
                (got.state.url, got.state.etag, modified, got.state.content_length, version_id),
            )
            return FetchResult(spec.source_id, "duplicate", version_id, None, got.sha256, got.size)

        fetched_at = datetime.now(UTC)
        _write_manifest(staging, spec, got, fetched_at)
        for item in staging.iterdir():
            item.chmod(0o444)
        final = parent / f"{fetched_at:%Y%m%dT%H%M%SZ}_{got.sha256[:12]}"
        os.replace(staging, final)
        local_path = str(final.relative_to(raw))
        row = conn.execute(
            INSERT_SQL,
            (
                spec.source_id,
                got.state.url,
                got.state.etag,
                modified,
                got.state.content_length,
                got.sha256,
                local_path,
            ),
        ).fetchone()
        if row is None:
            msg = f"{spec.source_id}: the snapshot could not be recorded"
            raise FetchError(msg)
        return FetchResult(spec.source_id, "fetched", int(row[0]), local_path, got.sha256, got.size)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def fetch_source(
    conn: psycopg.Connection[Any],
    client: httpx.Client,
    spec: SourceSpec,
    raw: Path,
    *,
    force: bool = False,
) -> FetchResult:
    """Run the fetch stage for one source: skip it if unchanged, otherwise store a new snapshot."""
    with runs.recorded(conn, "fetch", spec.source_id) as run:
        state = remote.head(client, spec)
        known = checks.latest_known(conn, spec.source_id)
        if not force and known is not None and checks.decide(state, known) == "unchanged":
            result = FetchResult(spec.source_id, "skipped", known.version_id, None, None, None)
        else:
            result = _fetch_new(conn, client, spec, raw, state)
        run.version_id = result.version_id
        run.result = result.to_json()
    return result
