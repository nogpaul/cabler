"""The *load* stage: read a fetched snapshot and store its objects in the ``raw`` schema.

The snapshot file is verified against the checksum recorded when it was fetched, then every object
of the selected themes is inserted into ``raw.osm_object`` tagged with the snapshot's version id.
Everything happens in one transaction: a load either completes or leaves nothing behind.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pyogrio
import pyogrio.raw
from psycopg import sql

from cabler import features, mastr, osm, runs
from cabler.sources import SourceSpec

BATCH_SIZE = 2000

PENDING_SQL = """
SELECT version_id, local_path, sha256
FROM meta.source_version
WHERE source_id = %s AND status = 'fetched'
ORDER BY created_at DESC, version_id DESC
LIMIT 1
"""
LOADED_SQL = """
SELECT version_id
FROM meta.source_version
WHERE source_id = %s AND status IN ('loaded', 'diffed', 'applied')
ORDER BY created_at DESC, version_id DESC
LIMIT 1
"""

COLUMNS = [
    "version_id",
    "layer",
    "osm_key",
    "osm_version",
    "osm_timestamp",
    *[column for _, column in osm.PROMOTED],
    "geom",
]
INSERT = sql.SQL("INSERT INTO raw.osm_object ({columns}) VALUES ({values})").format(
    columns=sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS),
    values=sql.SQL(", ").join(
        [
            *[sql.Placeholder()] * 4,
            sql.SQL("%s::timestamptz"),
            *[sql.Placeholder()] * len(osm.PROMOTED),
            sql.SQL("ST_SetSRID(ST_GeomFromWKB(%s), 4326)"),
        ]
    ),
)


class LoadError(Exception):
    """A snapshot cannot be loaded: nothing to load, or the file is not what was fetched."""


@dataclass(frozen=True)
class LoadResult:
    source_id: str
    status: str  # loaded | skipped
    version_id: int | None
    counts: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "version_id": self.version_id,
            "counts": self.counts,
        }


def where_clause(themes: Sequence[str]) -> str:
    """The OGR SQL filter that keeps only objects belonging to at least one theme."""
    return " OR ".join(f"({osm.THEMES[theme]})" for theme in themes)


def osm_key(layer: str, record: Mapping[str, Any]) -> str | None:
    """The stable feature id: n<id> for nodes, w<id> for ways, r<id> for relations."""
    if layer == "points":
        prefix, ident = "n", record.get("osm_id")
    elif layer == "lines":
        prefix, ident = "w", record.get("osm_id")
    elif record.get("osm_id") is not None:
        prefix, ident = "r", record.get("osm_id")
    else:
        prefix, ident = "w", record.get("osm_way_id")
    return None if ident in (None, "") else f"{prefix}{ident}"


def _clean(value: Any) -> Any:
    """Turn numpy scalars and empty strings into plain Python values."""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, str) and value == "":
        return None
    return value


def _utc(timestamp: str | None) -> str | None:
    """OSM times are always UTC; GDAL sometimes omits the marker, so make it explicit."""
    if timestamp is None or timestamp.endswith("Z") or "+" in timestamp[10:]:
        return timestamp
    return timestamp + "Z"


def read_layer(path: Path, layer: str, themes: Sequence[str]) -> Iterator[tuple[Any, ...]]:
    """Yield one row per object of the themes, in the column order of :data:`COLUMNS`.

    The version id is not included; :func:`store` puts it in front.
    """
    pyogrio.set_gdal_config_options({"OSM_CONFIG_FILE": str(osm.OSM_CONFIG)})
    meta, _, geometry, field_data = pyogrio.raw.read(
        str(path), layer=layer, where=where_clause(themes), datetime_as_string=True
    )
    index = {name: i for i, name in enumerate(meta["fields"])}
    for row in range(len(geometry)):
        record = {name: _clean(field_data[i][row]) for name, i in index.items()}
        key = osm_key(layer, record)
        if key is None or geometry[row] is None:
            continue
        version = record.get("osm_version")
        yield (
            layer,
            key,
            None if version is None else int(version),
            _utc(record.get("osm_timestamp")),
            *(record.get(source) for source, _ in osm.PROMOTED),
            bytes(geometry[row]),
        )


def store(conn: psycopg.Connection[Any], version_id: int, rows: Iterable[tuple[Any, ...]]) -> int:
    """Insert rows in batches, tagged with the snapshot version. Returns how many were stored."""
    stored = 0
    batch: list[tuple[Any, ...]] = []
    with conn.cursor() as cur:
        for row in rows:
            batch.append((version_id, *row))
            if len(batch) >= BATCH_SIZE:
                cur.executemany(INSERT, batch)
                stored += len(batch)
                batch = []
        if batch:
            cur.executemany(INSERT, batch)
            stored += len(batch)
    return stored


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_file(raw: Path, local_path: str) -> Path:
    """The data file inside a snapshot folder, as named by its manifest."""
    folder = raw / local_path
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return folder / str(manifest["file"])


def _load_osm(
    conn: psycopg.Connection[Any], version_id: int, path: Path, spec: SourceSpec
) -> dict[str, int]:
    counts = {
        layer: store(conn, version_id, read_layer(path, layer, spec.load_themes))
        for layer in osm.LAYERS
    }
    # Fresh statistics, so that the diff and apply queries that follow get a sound plan.
    conn.execute("ANALYZE raw.osm_object")
    return counts


def _load_mastr(
    conn: psycopg.Connection[Any], version_id: int, path: Path, spec: SourceSpec
) -> dict[str, int]:
    features.store(conn, version_id, mastr.read_export(path, spec.load_themes))
    conn.execute("ANALYZE raw.feature")
    return _feature_counts(conn, version_id)


def _feature_counts(conn: psycopg.Connection[Any], version_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT layer, count(*) FROM raw.feature WHERE version_id = %s GROUP BY layer ORDER BY 1",
        (version_id,),
    ).fetchall()
    return {str(layer): int(count) for layer, count in rows}


LOADERS: dict[str, Callable[[psycopg.Connection[Any], int, Path, SourceSpec], dict[str, int]]] = {
    "osm_pbf": _load_osm,
    "mastr_xml": _load_mastr,
}


def load_source(conn: psycopg.Connection[Any], spec: SourceSpec, raw: Path) -> LoadResult:
    """Run the load stage for one source: load its newest fetched snapshot, if any."""
    if spec.load_kind not in LOADERS:
        msg = f"{spec.source_id}: no [load] section of kind {sorted(LOADERS)}; nothing to load"
        raise LoadError(msg)
    with runs.recorded(conn, "load", spec.source_id) as run:
        row = conn.execute(PENDING_SQL, (spec.source_id,)).fetchone()
        if row is None:
            loaded = conn.execute(LOADED_SQL, (spec.source_id,)).fetchone()
            result = LoadResult(spec.source_id, "skipped", int(loaded[0]) if loaded else None, {})
        else:
            version_id, local_path, expected = int(row[0]), str(row[1]), str(row[2])
            path = snapshot_file(raw, local_path)
            if sha256_of(path) != expected:
                msg = (
                    f"{path}: the file no longer matches the checksum recorded when it was fetched"
                )
                raise LoadError(msg)
            with conn.transaction():
                counts = LOADERS[str(spec.load_kind)](conn, version_id, path, spec)
                conn.execute(
                    "UPDATE meta.source_version SET status = 'loaded' WHERE version_id = %s",
                    (version_id,),
                )
            result = LoadResult(spec.source_id, "loaded", version_id, counts)
        run.version_id = result.version_id
        run.result = result.to_json()
    return result
