"""A set of tiles as one snapshot, so that the stages after *fetch* can treat it like any source.

A tile source (ALKIS parcels: one GeoPackage per district) is fetched file by file into
``meta.tile``. The *load* stage needs one snapshot to read and to compare with the next one. This
module writes it: a listing of every current tile with its checksum (``tiles.json``), stored in a
snapshot folder next to a ``manifest.json`` like any other snapshot, and recorded in
``meta.source_version``. The listing's own checksum is the snapshot's checksum, so the same set of
tiles is never stored twice and a changed tile makes a new snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

from cabler import __version__

if TYPE_CHECKING:  # sources imports the readers, which import this module
    from cabler.sources import SourceSpec

TILES_SQL = """
SELECT name, size, local_path, sha256
FROM meta.tile
WHERE source_id = %s AND status = 'fetched'
ORDER BY name
"""
EXISTING_SQL = "SELECT version_id FROM meta.source_version WHERE source_id = %s AND sha256 = %s"
INSERT_SQL = """
INSERT INTO meta.source_version
    (source_id, remote_url, remote_etag, remote_last_modified, content_length, sha256,
     local_path, status)
VALUES (%s, %s, NULL, NULL, %s, %s, %s, 'fetched')
RETURNING version_id
"""
LISTING = "tiles.json"


class TileSetError(Exception):
    """The set cannot be turned into a snapshot or read back."""


@dataclass(frozen=True)
class SetTile:
    """One tile of a set, as recorded in the listing."""

    name: str
    path: str  # relative to the raw directory
    size: int
    sha256: str


def snapshot_set(conn: psycopg.Connection[Any], spec: SourceSpec, raw: Path) -> int | None:
    """Record the current tiles of a source as one snapshot; the version id, or None if no tiles.

    A set of tiles that was recorded before gives the same version again.
    """
    rows = conn.execute(TILES_SQL, (spec.source_id,)).fetchall()
    if not rows:
        return None
    sizes = [int(r[1]) for r in rows]
    tiles = [
        {"name": str(r[0]), "size": int(r[1]), "path": str(r[2]), "sha256": str(r[3])} for r in rows
    ]
    listing = json.dumps({"source_id": spec.source_id, "tiles": tiles}, indent=2, sort_keys=True)
    data = (listing + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    existing = conn.execute(EXISTING_SQL, (spec.source_id, digest)).fetchone()
    if existing is not None:
        return int(existing[0])

    now = datetime.now(UTC)
    parent = raw / spec.source_id / "sets"
    staging = parent / f".partial-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        (staging / LISTING).write_bytes(data)
        manifest = {
            "source_id": spec.source_id,
            "requested_url": spec.url,
            "file": LISTING,
            "size": len(data),
            "sha256": digest,
            "tiles": len(tiles),
            "tile_bytes": sum(sizes),
            "fetched_at": now.isoformat(),
            "fetched_by": f"cabler-pipeline/{__version__}",
            "licence_id": spec.licence_id,
            "attribution": spec.attribution,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for item in staging.iterdir():
            item.chmod(0o444)
        final = parent / f"{now:%Y%m%dT%H%M%SZ}_{digest[:12]}"
        os.replace(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    row = conn.execute(
        INSERT_SQL,
        (spec.source_id, spec.url, len(data), digest, str(final.relative_to(raw))),
    ).fetchone()
    if row is None:
        msg = f"{spec.source_id}: the tile set could not be recorded"
        raise TileSetError(msg)
    return int(row[0])


def read_listing(listing: Path) -> list[SetTile]:
    """The tiles of a snapshot's listing file."""
    try:
        data = json.loads(listing.read_text(encoding="utf-8"))
        return [
            SetTile(str(t["name"]), str(t["path"]), int(t["size"]), str(t["sha256"]))
            for t in data["tiles"]
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        msg = f"{listing} is not a tile listing"
        raise TileSetError(msg) from exc


def raw_root(listing: Path) -> Path:
    """The raw directory of a listing: raw/<source>/sets/<snapshot>/tiles.json."""
    return listing.parents[3]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_tiles(listing: Path) -> list[tuple[SetTile, Path]]:
    """Every tile of a listing with its file, after checking that the file is the recorded one."""
    root = raw_root(listing)
    result = []
    for tile in read_listing(listing):
        path = root / tile.path
        if not path.is_file() or path.stat().st_size != tile.size or sha256_of(path) != tile.sha256:
            msg = f"{path}: the tile is missing or no longer matches the checksum recorded for it"
            raise TileSetError(msg)
        result.append((tile, path))
    return result
