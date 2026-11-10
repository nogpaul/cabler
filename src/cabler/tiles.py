"""The *tiles* variant of the fetch stage, for publishers that list thousands of files in an index.

A state publishes its terrain model as tens of thousands of small GeoTIFF tiles next to an index
file with each tile's name, size and time. One run:

1. reads the index and compares it with ``meta.tile``: tiles are *new*, *changed* (size or time
   differ), *unchanged*, or *removed* (no longer listed)
2. downloads the new and changed ones in parallel, each verified against the index size and
   hashed while it streams
3. records every finished tile at once, so an interrupted run resumes where it stopped

A changed tile keeps its old file under ``superseded/``; nothing is ever deleted.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import psycopg
from defusedxml import ElementTree

from cabler import fetch, remote, runs
from cabler.remote import RemoteError
from cabler.sources import SourceSpec

INDEX_LIMIT = 100_000_000
# \w is a letter, digit or underscore in any language ("Mülheim"); the dot and hyphen are added.
TILE_NAME = re.compile(r"[\w.-]+")
ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 4.0)
FAILURES_SHOWN = 10

KNOWN_SQL = """
SELECT name, remote_timestamp, size, local_path, sha256
FROM meta.tile
WHERE source_id = %s AND status = 'fetched'
"""
INSERT_SQL = """
INSERT INTO meta.tile (source_id, name, remote_timestamp, size, sha256, local_path, status)
VALUES (%s, %s, %s, %s, %s, %s, 'fetched')
ON CONFLICT (source_id, name, remote_timestamp) DO UPDATE
SET size = EXCLUDED.size, sha256 = EXCLUDED.sha256, local_path = EXCLUDED.local_path,
    status = 'fetched', fetched_at = now()
"""
CORRUPT_SQL = """
UPDATE meta.tile SET status = 'corrupt'
WHERE source_id = %s AND name = ANY(%s) AND status = 'fetched'
"""
SUPERSEDE_SQL = """
UPDATE meta.tile SET status = 'superseded', local_path = %s
WHERE source_id = %s AND name = %s AND status = 'fetched'
"""
REMOVE_SQL = """
UPDATE meta.tile SET status = 'removed'
WHERE source_id = %s AND name = ANY(%s) AND status = 'fetched'
"""


class TilesError(Exception):
    """The index cannot be read, or the source is not an index-based tile source."""


@dataclass(frozen=True)
class TileEntry:
    """One line of the publisher's index."""

    name: str
    size: int
    timestamp: datetime


@dataclass(frozen=True)
class KnownTile:
    """A tile we already hold."""

    name: str
    timestamp: datetime
    size: int
    local_path: str
    sha256: str


@dataclass(frozen=True)
class TilePlan:
    new: tuple[TileEntry, ...]
    changed: tuple[TileEntry, ...]
    unchanged: int
    removed: tuple[str, ...]

    @property
    def todo(self) -> tuple[TileEntry, ...]:
        return self.new + self.changed


@dataclass(frozen=True)
class TileResult:
    source_id: str
    status: str  # planned | fetched | up_to_date
    listed: int
    new: int
    changed: int
    unchanged: int
    removed: int
    fetched: int
    failed: int
    bytes_fetched: int
    planned_bytes: int
    failures: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "listed": self.listed,
            "new": self.new,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "fetched": self.fetched,
            "failed": self.failed,
            "bytes_fetched": self.bytes_fetched,
            "planned_bytes": self.planned_bytes,
            "failures": list(self.failures),
        }


def is_safe_name(name: str) -> bool:
    """A plain file name: letters of any language, digits, ``.``, ``_`` and ``-``.

    It cannot hold a path separator, a space, a control character, a query or fragment mark, or
    start with a dot, so joining it to a folder or to a URL can never leave that folder or URL.
    """
    return 0 < len(name) <= 200 and TILE_NAME.fullmatch(name) is not None and name[0] != "."


def parse_index(xml: bytes) -> list[TileEntry]:
    """Read the tiles from an opengeodata index: ``<file name=".." size=".." timestamp=".."/>``.

    The index gives times without a zone; they are taken as UTC so that they compare stably.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        msg = f"the index is not valid XML: {exc}"
        raise TilesError(msg) from exc
    entries = []
    for element in root.iter("file"):
        try:
            entry = TileEntry(
                name=element.attrib["name"],
                size=int(element.attrib["size"]),
                timestamp=datetime.fromisoformat(element.attrib["timestamp"]).replace(tzinfo=UTC),
            )
        except (KeyError, ValueError) as exc:
            msg = f"an index entry is unusable: {dict(element.attrib)}"
            raise TilesError(msg) from exc
        if not is_safe_name(entry.name):
            msg = f"an index entry has a name that is not a plain file name: {entry.name!r}"
            raise TilesError(msg)
        entries.append(entry)
    if not entries:
        msg = "the index lists no files"
        raise TilesError(msg)
    return sorted(entries, key=lambda e: e.name)


def plan(index: Sequence[TileEntry], known: dict[str, KnownTile]) -> TilePlan:
    """Sort the index into new, changed and unchanged tiles; known tiles not listed are removed."""
    listed = {entry.name for entry in index}
    new, changed, unchanged = [], [], 0
    for entry in index:
        have = known.get(entry.name)
        if have is None:
            new.append(entry)
        elif have.size != entry.size or have.timestamp != entry.timestamp:
            changed.append(entry)
        else:
            unchanged += 1
    removed = tuple(sorted(set(known) - listed))
    return TilePlan(tuple(new), tuple(changed), unchanged, removed)


def read_index(client: httpx.Client, spec: SourceSpec) -> bytes:
    """Download the index itself (small), refusing anything unreasonably large."""
    response = remote.send(client, "GET", spec, stream=True)
    try:
        remote.state_of(response, spec)
        body = bytearray()
        for chunk in response.iter_raw(1 << 20):
            body.extend(chunk)
            if len(body) > INDEX_LIMIT:
                msg = f"{spec.url}: the index is larger than {INDEX_LIMIT} bytes"
                raise TilesError(msg)
        return bytes(body)
    except httpx.TransportError as exc:
        msg = f"{spec.url}: {type(exc).__name__} while reading the index"
        raise TilesError(msg) from exc
    finally:
        response.close()


def _download_one(
    client: httpx.Client, spec: SourceSpec, entry: TileEntry, partial: Path
) -> fetch.Downloaded:
    """Fetch one tile into its own scratch folder, retrying transient failures."""
    tile_spec = replace(spec, url=urljoin(spec.url, entry.name), probe="none")
    last: RemoteError | None = None
    for attempt in range(ATTEMPTS):
        folder = partial / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            got = fetch.download(client, tile_spec, folder)
        except (remote.ConnectionFailedError, fetch.DownloadError, remote.RemoteStatusError) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            last = exc
            if attempt < ATTEMPTS - 1:
                time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            continue
        except RemoteError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        if got.size != entry.size:
            shutil.rmtree(folder, ignore_errors=True)
            msg = f"{entry.name}: got {got.size} bytes, the index says {entry.size}"
            raise fetch.DownloadError(msg)
        return got
    if last is None:
        msg = f"{entry.name}: no download attempt was made"
        raise fetch.DownloadError(msg)
    raise last


def _target(tiles_dir: Path, name: str) -> Path:
    """Where a tile is stored. The name is checked again here, with the rule of the index."""
    if not is_safe_name(name):
        msg = f"{name}: not a plain file name; the tile would not stay in its folder"
        raise TilesError(msg)
    return tiles_dir / name


def _store(
    conn: psycopg.Connection[Any],
    spec: SourceSpec,
    raw: Path,
    entry: TileEntry,
    got: fetch.Downloaded,
    old: KnownTile | None,
) -> None:
    """Move a verified tile into place and record it, keeping any older file."""
    tiles_dir = raw / spec.source_id / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    target = _target(tiles_dir, entry.name)
    if old is not None and target.exists():
        keep = tiles_dir / "superseded"
        keep.mkdir(exist_ok=True)
        moved = keep / f"{old.sha256[:12]}_{entry.name}"
        os.replace(target, moved)
        conn.execute(SUPERSEDE_SQL, (str(moved.relative_to(raw)), spec.source_id, entry.name))
    got.path.chmod(0o444)
    os.replace(got.path, target)
    conn.execute(
        INSERT_SQL,
        (
            spec.source_id,
            entry.name,
            entry.timestamp,
            got.size,
            got.sha256,
            str(target.relative_to(raw)),
        ),
    )


def fetch_tiles(
    conn: psycopg.Connection[Any],
    client: httpx.Client,
    spec: SourceSpec,
    raw: Path,
    *,
    yes: bool = False,
    limit: int | None = None,
    workers: int = 6,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> TileResult:
    """Run the tiles fetch for one source. Without ``yes`` only the plan is made."""
    if spec.tiles_kind is None:
        msg = f"{spec.source_id}: no [tiles] section; use the ordinary fetch"
        raise TilesError(msg)
    with runs.recorded(conn, "fetch", spec.source_id) as run:
        index = parse_index(read_index(client, spec))
        known = {
            str(row[0]): KnownTile(str(row[0]), row[1], int(row[2]), str(row[3]), str(row[4]))
            for row in conn.execute(KNOWN_SQL, (spec.source_id,)).fetchall()
        }
        tile_plan = plan(index, known)
        todo = list(tile_plan.todo[:limit] if limit is not None else tile_plan.todo)
        planned_bytes = sum(entry.size for entry in todo)
        fetched = failed = bytes_fetched = 0
        failures: list[str] = []
        if yes and todo:
            fetch.ensure_space(raw, planned_bytes)
            partial = raw / spec.source_id / ".tiles-partial"
            shutil.rmtree(partial, ignore_errors=True)
            partial.mkdir(parents=True)
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    jobs = {
                        pool.submit(_download_one, client, spec, entry, partial): entry
                        for entry in todo
                    }
                    for done, job in enumerate(as_completed(jobs), 1):
                        entry = jobs[job]
                        try:
                            got = job.result()
                            _store(conn, spec, raw, entry, got, known.get(entry.name))
                            fetched += 1
                            bytes_fetched += got.size
                        except (RemoteError, OSError) as exc:
                            failed += 1
                            failures.append(f"{entry.name}: {exc}")
                        if on_progress is not None:
                            on_progress(done, len(todo), bytes_fetched)
            finally:
                shutil.rmtree(partial, ignore_errors=True)
            if tile_plan.removed:
                conn.execute(REMOVE_SQL, (spec.source_id, list(tile_plan.removed)))
            raw_index = raw / spec.source_id / "index"
            raw_index.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            (raw_index / f"{stamp}.xml").write_bytes(
                b"".join(f"{e.name} {e.size} {e.timestamp.isoformat()}\n".encode() for e in index)
            )
        if not yes:
            status = "planned"
        elif todo or tile_plan.removed:
            status = "fetched"
        else:
            status = "up_to_date"
        result = TileResult(
            source_id=spec.source_id,
            status=status,
            listed=len(index),
            new=len(tile_plan.new),
            changed=len(tile_plan.changed),
            unchanged=tile_plan.unchanged,
            removed=len(tile_plan.removed),
            fetched=fetched,
            failed=failed,
            bytes_fetched=bytes_fetched,
            planned_bytes=planned_bytes,
            failures=tuple(failures[:FAILURES_SHOWN]),
        )
        run.result = result.to_json()
    return result


@dataclass(frozen=True)
class VerifyResult:
    source_id: str
    checked: int
    ok: int
    corrupt: int
    missing: int
    repaired: bool
    names: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "checked": self.checked,
            "ok": self.ok,
            "corrupt": self.corrupt,
            "missing": self.missing,
            "repaired": self.repaired,
            "names": list(self.names),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_of(raw: Path, tile: KnownTile) -> str:
    """ok, corrupt (size or checksum differs) or missing, for one stored tile."""
    path = raw / tile.local_path
    if not path.exists():
        return "missing"
    if path.stat().st_size != tile.size or _sha256(path) != tile.sha256:
        return "corrupt"
    return "ok"


def verify_tiles(
    conn: psycopg.Connection[Any],
    spec: SourceSpec,
    raw: Path,
    *,
    fix: bool = False,
    workers: int = 6,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> VerifyResult:
    """Re-hash every stored tile. With ``fix`` damaged files are set aside and the tiles marked."""
    with runs.recorded(conn, "verify", spec.source_id) as run:
        known = [
            KnownTile(str(r[0]), r[1], int(r[2]), str(r[3]), str(r[4]))
            for r in conn.execute(KNOWN_SQL, (spec.source_id,)).fetchall()
        ]
        states: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {pool.submit(_state_of, raw, tile): tile for tile in known}
            for done, job in enumerate(as_completed(jobs), 1):
                states[jobs[job].name] = job.result()
                if on_progress is not None:
                    on_progress(done, len(known), 0)
        bad = sorted(name for name, state in states.items() if state != "ok")
        if fix and bad:
            aside = raw / spec.source_id / "tiles" / "_corrupt"
            aside.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            for tile in known:
                if states[tile.name] == "corrupt":
                    os.replace(raw / tile.local_path, aside / f"{tile.name}.{stamp}")
            conn.execute(CORRUPT_SQL, (spec.source_id, bad))
        result = VerifyResult(
            source_id=spec.source_id,
            checked=len(known),
            ok=len(known) - len(bad),
            corrupt=sum(1 for s in states.values() if s == "corrupt"),
            missing=sum(1 for s in states.values() if s == "missing"),
            repaired=fix and bool(bad),
            names=tuple(bad[:FAILURES_SHOWN]),
        )
        run.result = result.to_json()
    return result
