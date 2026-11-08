"""Record every pipeline stage execution in ``meta.pipeline_run``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


@dataclass
class Run:
    """A running stage. Set ``result`` (and optionally ``version_id``) before the block ends."""

    run_id: int
    version_id: int | None = None
    result: dict[str, Any] = field(default_factory=dict)


@contextmanager
def recorded(conn: psycopg.Connection[Any], stage: str, source_id: str) -> Iterator[Run]:
    """Insert a ``running`` row, then mark it ``succeeded`` or ``failed`` when the block ends."""
    row = conn.execute(
        "INSERT INTO meta.pipeline_run (stage, source_id) VALUES (%s, %s) RETURNING run_id",
        (stage, source_id),
    ).fetchone()
    if row is None:
        msg = "could not record the pipeline run"
        raise RuntimeError(msg)
    run = Run(int(row[0]))
    try:
        yield run
    except Exception as exc:
        conn.execute(
            "UPDATE meta.pipeline_run SET status = 'failed', finished_at = now(), error = %s,"
            " result = %s WHERE run_id = %s",
            (str(exc)[:2000], Jsonb(run.result), run.run_id),
        )
        raise
    conn.execute(
        "UPDATE meta.pipeline_run SET status = 'succeeded', finished_at = now(), result = %s,"
        " version_id = %s WHERE run_id = %s",
        (Jsonb(run.result), run.version_id, run.run_id),
    )
