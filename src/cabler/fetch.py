"""The *fetch* stage: download a source, verify it, and keep it as an immutable snapshot.

A snapshot is a folder ``<raw>/<source_id>/<UTC time>_<sha256[:12]>/`` holding the file exactly as
delivered plus a ``manifest.json``. Files are made read-only and are never executed. A download
that fails, is too large or is incomplete leaves nothing behind.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from cabler import db, remote
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
    """A safe file name taken from the final URL, or derived from the source id."""
    path = urlsplit(final_url).path
    name = "" if path.endswith("/") else Path(path).name
    return name if SAFE_NAME.match(name) else f"{spec.source_id}.{spec.file_format}"


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
        with target.open("wb") as handle:
            for chunk in response.iter_raw(CHUNK_SIZE):
                size += len(chunk)
                if size > spec.max_bytes:
                    msg = f"{spec.url}: more than {spec.max_bytes} bytes received; stopped"
                    raise TooLargeError(msg)
                digest.update(chunk)
                handle.write(chunk)
    finally:
        response.close()
    if state.content_length is not None and size != state.content_length:
        msg = f"{spec.url}: incomplete download, got {size} of {state.content_length} bytes"
        raise DownloadError(msg)
    return Downloaded(target, digest.hexdigest(), size, state)
