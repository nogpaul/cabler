"""The *check* stage: ask the publisher whether a source changed, without downloading it.

Outcomes:
    new           nothing recorded for this source yet
    unchanged     the publisher's validators match the latest recorded version
    changed       they differ, so a fetch is worthwhile
    unverifiable  the publisher gives no usable validators; only a download can tell
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import psycopg

from cabler import remote, runs
from cabler.remote import RemoteState
from cabler.sources import SourceSpec

LATEST_VERSION = """
SELECT version_id, remote_etag, remote_last_modified, content_length
FROM meta.source_version
WHERE source_id = %s AND status IN ('fetched', 'loaded', 'applied')
ORDER BY created_at DESC, version_id DESC
LIMIT 1
"""


@dataclass(frozen=True)
class Known:
    """The publisher's validators as recorded when we last fetched the source."""

    version_id: int
    etag: str | None
    last_modified: datetime | None
    content_length: int | None


@dataclass(frozen=True)
class CheckResult:
    source_id: str
    outcome: str
    remote: RemoteState
    known_version_id: int | None

    def to_json(self) -> dict[str, Any]:
        modified = self.remote.last_modified
        return {
            "source_id": self.source_id,
            "outcome": self.outcome,
            "known_version_id": self.known_version_id,
            "remote": {
                "url": self.remote.url,
                "etag": self.remote.etag,
                "last_modified": modified.isoformat() if modified else None,
                "content_length": self.remote.content_length,
            },
        }


def latest_known(conn: psycopg.Connection[Any], source_id: str) -> Known | None:
    """The most recent successfully fetched version of a source, if any."""
    row = conn.execute(LATEST_VERSION, (source_id,)).fetchone()
    return None if row is None else Known(*row)


def decide(state: RemoteState, known: Known | None) -> str:
    """Compare what the publisher says now with what we recorded."""
    if known is None:
        return "new"
    if state.etag and known.etag:
        return "unchanged" if state.etag == known.etag else "changed"
    if state.last_modified and known.last_modified:
        same = (
            state.last_modified == known.last_modified
            and state.content_length == known.content_length
        )
        return "unchanged" if same else "changed"
    return "unverifiable"


def check_source(
    conn: psycopg.Connection[Any], client: httpx.Client, spec: SourceSpec
) -> CheckResult:
    """Run the check stage for one source and record the run."""
    with runs.recorded(conn, "check", spec.source_id) as run:
        state = remote.head(client, spec)
        known = latest_known(conn, spec.source_id)
        result = CheckResult(
            spec.source_id,
            decide(state, known),
            state,
            known.version_id if known else None,
        )
        run.result = result.to_json()
    return result
