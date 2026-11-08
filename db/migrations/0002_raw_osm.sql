-- OpenStreetMap objects exactly as delivered, one row per object per loaded snapshot.
-- Geometry stays in WGS 84 (EPSG:4326); reprojection to EPSG:25832 happens on the way to staging.

CREATE TABLE raw.osm_object (
    version_id                   bigint NOT NULL REFERENCES meta.source_version (version_id),
    layer                        text   NOT NULL CHECK (layer IN ('points', 'lines', 'multipolygons')),
    osm_key                      text   NOT NULL CHECK (osm_key ~ '^[nwr][0-9]+$'),
    osm_version                  integer,
    osm_timestamp                timestamptz,
    name                         text,
    ref                          text,
    operator                     text,
    power                        text,
    voltage                      text,
    cables                       text,
    circuits                     text,
    generator_source             text,
    generator_method             text,
    generator_output_electricity text,
    highway                      text,
    railway                      text,
    waterway                     text,
    "natural"                    text,
    landuse                      text,
    building                     text,
    other_tags                   text,
    geom                         geometry(Geometry, 4326) NOT NULL,
    PRIMARY KEY (version_id, layer, osm_key)
);
CREATE INDEX osm_object_geom_idx ON raw.osm_object USING gist (geom);
CREATE INDEX osm_object_power_idx ON raw.osm_object (version_id, power) WHERE power IS NOT NULL;

COMMENT ON TABLE raw.osm_object IS
    'OpenStreetMap objects as delivered. osm_key is n<id>, w<id> or r<id>: the stable feature id.';
COMMENT ON COLUMN raw.osm_object.other_tags IS
    'All tags not promoted to a column, in GDAL hstore text form.';
