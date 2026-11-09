"""Source registry: one TOML file per data source in ``config/sources``.

A source file says where the data comes from, which licence applies and what credit to print, and
how it may be downloaded (https only, from an allow-listed host, below a size limit). Files are
validated strictly, so a typo fails loudly instead of silently changing behaviour.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from cabler import db, mastr, nas, ogr, osm

SOURCES_DIR = db.REPO_ROOT / "config" / "sources"
SOURCE_ID = re.compile(r"^[a-z0-9_]+$")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
FILE_FORMAT = re.compile(r"^[a-z0-9.]+$")

TOP_LEVEL_KEYS = {
    "source_id",
    "title",
    "publisher",
    "licence_id",
    "licence_url",
    "attribution",
    "homepage",
    "download",
    "load",
    "tiles",
    "catalogue",
}
DOWNLOAD_KEYS = {"url", "allowed_hosts", "max_bytes", "format", "probe"}
PROBES = {"head", "none"}
LOAD_KEYS = {"kind", "themes"}
# What a [load] section of each kind may ask for: the themes its reader knows.
LOAD_THEMES: dict[str, Mapping[str, object]] = {
    "osm_pbf": osm.THEMES,
    "mastr_xml": mastr.THEMES,
    "ogr": ogr.THEMES,
    "nas_gml": nas.THEMES,
}
LOAD_KINDS = set(LOAD_THEMES)
TILES_KEYS = {"kind"}
TILES_KINDS = {"opengeodata_index"}
CATALOGUE_KEYS = {"kind"}
CATALOGUE_KINDS = {"geotiff"}


class SourceConfigError(Exception):
    """A source file is missing something, has a typo, or breaks a safety rule."""


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    title: str
    publisher: str
    licence_id: str
    licence_url: str
    attribution: str
    homepage: str | None
    url: str
    allowed_hosts: tuple[str, ...]
    max_bytes: int
    file_format: str
    load_kind: str | None = None
    load_themes: tuple[str, ...] = ()
    probe: str = "head"
    tiles_kind: str | None = None
    catalogue_kind: str | None = None


def _text(table: Mapping[str, Any], key: str, origin: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{origin}: '{key}' must be a non-empty text value"
        raise SourceConfigError(msg)
    return value.strip()


def _https_url(value: str, what: str, origin: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        msg = f"{origin}: {what} must be an https URL, got {value!r}"
        raise SourceConfigError(msg)
    return value


def _unknown_keys(table: Mapping[str, Any], allowed: set[str], origin: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        msg = f"{origin}: unknown key(s) {unknown}; allowed: {sorted(allowed)}"
        raise SourceConfigError(msg)


def _parse_load(table: object, origin: str) -> tuple[str | None, tuple[str, ...]]:
    """Validate the optional [load] table: which loader to use and which themes to keep."""
    if table is None:
        return None, ()
    if not isinstance(table, dict):
        msg = f"{origin}: [load] must be a table"
        raise SourceConfigError(msg)
    _unknown_keys(table, LOAD_KEYS, origin)
    kind = _text(table, "kind", origin)
    if kind not in LOAD_KINDS:
        msg = f"{origin}: load.kind must be one of {sorted(LOAD_KINDS)}, got {kind!r}"
        raise SourceConfigError(msg)
    known = sorted(LOAD_THEMES[kind])
    themes = table.get("themes")
    if (
        not isinstance(themes, list)
        or not themes
        or not all(isinstance(t, str) and t in known for t in themes)
    ):
        msg = f"{origin}: load.themes must be a non-empty list from {known}"
        raise SourceConfigError(msg)
    return kind, tuple(themes)


def _parse_tiles(table: object, origin: str) -> str | None:
    """Validate the optional [tiles] table: the source is an index of many tile files."""
    if table is None:
        return None
    if not isinstance(table, dict):
        msg = f"{origin}: [tiles] must be a table"
        raise SourceConfigError(msg)
    _unknown_keys(table, TILES_KEYS, origin)
    kind = _text(table, "kind", origin)
    if kind not in TILES_KINDS:
        msg = f"{origin}: tiles.kind must be one of {sorted(TILES_KINDS)}, got {kind!r}"
        raise SourceConfigError(msg)
    return kind


def _parse_catalogue(table: object, origin: str) -> str | None:
    """Validate the optional [catalogue] table: the files are rasters to be catalogued."""
    if table is None:
        return None
    if not isinstance(table, dict):
        msg = f"{origin}: [catalogue] must be a table"
        raise SourceConfigError(msg)
    _unknown_keys(table, CATALOGUE_KEYS, origin)
    kind = _text(table, "kind", origin)
    if kind not in CATALOGUE_KINDS:
        msg = f"{origin}: catalogue.kind must be one of {sorted(CATALOGUE_KINDS)}, got {kind!r}"
        raise SourceConfigError(msg)
    return kind


def parse(data: Mapping[str, Any], origin: str) -> SourceSpec:
    """Validate a parsed TOML document and turn it into a :class:`SourceSpec`."""
    _unknown_keys(data, TOP_LEVEL_KEYS, origin)
    source_id = _text(data, "source_id", origin)
    if not SOURCE_ID.match(source_id):
        msg = f"{origin}: source_id must match {SOURCE_ID.pattern}, got {source_id!r}"
        raise SourceConfigError(msg)

    download = data.get("download")
    if not isinstance(download, dict):
        msg = f"{origin}: a [download] table is required"
        raise SourceConfigError(msg)
    _unknown_keys(download, DOWNLOAD_KEYS, origin)

    url = _https_url(_text(download, "url", origin), "download.url", origin)
    hosts = download.get("allowed_hosts")
    if (
        not isinstance(hosts, list)
        or not hosts
        or not all(isinstance(h, str) and HOSTNAME.match(h) for h in hosts)
    ):
        msg = f"{origin}: download.allowed_hosts must be a non-empty list of lowercase host names"
        raise SourceConfigError(msg)
    if urlsplit(url).hostname not in hosts:
        msg = f"{origin}: the download host is not in allowed_hosts {hosts}"
        raise SourceConfigError(msg)

    max_bytes = download.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        msg = f"{origin}: download.max_bytes must be a positive whole number"
        raise SourceConfigError(msg)
    file_format = _text(download, "format", origin)
    if not FILE_FORMAT.match(file_format):
        msg = f"{origin}: download.format must match {FILE_FORMAT.pattern}, got {file_format!r}"
        raise SourceConfigError(msg)

    probe = download.get("probe", "head")
    if probe not in PROBES:
        msg = f"{origin}: download.probe must be one of {sorted(PROBES)}, got {probe!r}"
        raise SourceConfigError(msg)

    homepage = data.get("homepage")
    if homepage is not None:
        homepage = _https_url(_text(data, "homepage", origin), "homepage", origin)

    load_kind, load_themes = _parse_load(data.get("load"), origin)
    tiles_kind = _parse_tiles(data.get("tiles"), origin)
    catalogue_kind = _parse_catalogue(data.get("catalogue"), origin)

    return SourceSpec(
        source_id=source_id,
        title=_text(data, "title", origin),
        publisher=_text(data, "publisher", origin),
        licence_id=_text(data, "licence_id", origin),
        licence_url=_https_url(_text(data, "licence_url", origin), "licence_url", origin),
        attribution=_text(data, "attribution", origin),
        homepage=homepage,
        url=url,
        allowed_hosts=tuple(hosts),
        max_bytes=max_bytes,
        file_format=file_format,
        load_kind=load_kind,
        load_themes=load_themes,
        probe=probe,
        tiles_kind=tiles_kind,
        catalogue_kind=catalogue_kind,
    )


def load_source(path: Path) -> SourceSpec:
    """Read and validate one source file. The file name must equal the source_id."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path.name}: not valid TOML ({exc})"
        raise SourceConfigError(msg) from exc
    spec = parse(data, path.name)
    if spec.source_id != path.stem:
        msg = f"{path.name}: source_id {spec.source_id!r} must match the file name"
        raise SourceConfigError(msg)
    return spec


def load_all(directory: Path = SOURCES_DIR) -> dict[str, SourceSpec]:
    """Read every ``*.toml`` file in the directory, keyed by source_id."""
    return {spec.source_id: spec for spec in map(load_source, sorted(directory.glob("*.toml")))}


UPSERT = """
INSERT INTO meta.source
    (source_id, title, publisher, licence_id, licence_url, attribution, homepage)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source_id) DO UPDATE SET
    title = EXCLUDED.title,
    publisher = EXCLUDED.publisher,
    licence_id = EXCLUDED.licence_id,
    licence_url = EXCLUDED.licence_url,
    attribution = EXCLUDED.attribution,
    homepage = EXCLUDED.homepage
RETURNING (xmax = 0)
"""


def register(conn: psycopg.Connection[Any], spec: SourceSpec) -> bool:
    """Write a source and its licence to ``meta.source``. Returns True if it was newly created."""
    row = conn.execute(
        UPSERT,
        (
            spec.source_id,
            spec.title,
            spec.publisher,
            spec.licence_id,
            spec.licence_url,
            spec.attribution,
            spec.homepage,
        ),
    ).fetchone()
    return bool(row and row[0])
