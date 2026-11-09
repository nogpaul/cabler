"""Generic feature storage for sources that are not OpenStreetMap.

A source reader yields :class:`Feature` objects; :func:`store` writes them to ``raw.feature`` tagged
with the snapshot's version id. Attributes are kept as delivered (text), so that what the publisher
sent can always be shown. A SHA-256 over attributes and geometry makes change detection a plain
comparison.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

BATCH_SIZE = 2000

INSERT_SQL = """
INSERT INTO raw.feature (version_id, layer, feature_key, attributes, content_hash, geom)
VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromWKB(%s::bytea), %s))
"""


@dataclass(frozen=True)
class Feature:
    """One feature as delivered. ``wkb`` is None when the publisher gave no geometry."""

    layer: str
    key: str
    attributes: dict[str, str | None]
    wkb: bytes | None = None
    srid: int = 0

    @property
    def content_hash(self) -> str:
        return content_hash(self.attributes, self.wkb, self.srid)


def content_hash(attributes: dict[str, str | None], wkb: bytes | None, srid: int) -> str:
    """SHA-256 over the canonical attributes and the geometry with its coordinate system."""
    digest = hashlib.sha256()
    digest.update(json.dumps(attributes, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(srid).encode())
    digest.update(b"\0")
    digest.update(wkb or b"")
    return digest.hexdigest()


def store(conn: psycopg.Connection[Any], version_id: int, features: Iterable[Feature]) -> int:
    """Insert features in batches, tagged with the snapshot version; returns how many."""
    stored = 0
    batch: list[tuple[Any, ...]] = []
    with conn.cursor() as cur:
        for feature in features:
            batch.append(
                (
                    version_id,
                    feature.layer,
                    feature.key,
                    Jsonb(feature.attributes),
                    feature.content_hash,
                    feature.wkb,
                    feature.srid,
                )
            )
            if len(batch) >= BATCH_SIZE:
                cur.executemany(INSERT_SQL, batch)
                stored += len(batch)
                batch = []
        if batch:
            cur.executemany(INSERT_SQL, batch)
            stored += len(batch)
    return stored
