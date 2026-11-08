"""OpenStreetMap specifics: which themes we load and how their attributes map to raw columns."""

from __future__ import annotations

from cabler import db

OSM_CONFIG = db.REPO_ROOT / "config" / "osm" / "osmconf.ini"
LAYERS = ("points", "lines", "multipolygons")

# A theme is a filter on the promoted columns of osmconf.ini (OGR SQL syntax).
THEMES: dict[str, str] = {
    "power": "power IS NOT NULL",
    "transport": "highway IS NOT NULL OR railway IS NOT NULL",
    "water": "waterway IS NOT NULL OR \"natural\" = 'water'",
    "landuse": 'landuse IS NOT NULL OR "natural" IS NOT NULL',
    "buildings": "building IS NOT NULL",
}

# GDAL field name -> column of raw.osm_object (the order is the order of the INSERT).
# GDAL turns tag names such as "generator:source" into field names such as "generator_source".
PROMOTED: tuple[tuple[str, str], ...] = (
    ("name", "name"),
    ("ref", "ref"),
    ("operator", "operator"),
    ("power", "power"),
    ("voltage", "voltage"),
    ("cables", "cables"),
    ("circuits", "circuits"),
    ("generator_source", "generator_source"),
    ("generator_method", "generator_method"),
    ("generator_output_electricity", "generator_output_electricity"),
    ("highway", "highway"),
    ("railway", "railway"),
    ("waterway", "waterway"),
    ("natural", "natural"),
    ("landuse", "landuse"),
    ("building", "building"),
    ("other_tags", "other_tags"),
)
