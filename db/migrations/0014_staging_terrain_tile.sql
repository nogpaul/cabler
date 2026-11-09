-- Staging: the terrain catalogue. One row per tile file that was ever catalogued: where it is
-- (a footprint in EPSG:25832), what it holds (size, pixel size, data type, no-data value) and what
-- is unusual about it (qa_flags). The catalogue is derived data: it can be rebuilt at any time
-- from the tile files, which stay untouched. A tile is identified by its source, its name and the
-- checksum of its file, so a replaced tile is a new row; is_current marks the tiles of the set that
-- was catalogued last.

CREATE TABLE staging.terrain_tile (
    tile_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id       text    NOT NULL REFERENCES meta.source (source_id),
    name            text    NOT NULL CHECK (name <> ''),
    file_sha256     text    NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    file_size       bigint  NOT NULL CHECK (file_size > 0),
    epsg            integer,
    width_px        integer NOT NULL CHECK (width_px > 0),
    height_px       integer NOT NULL CHECK (height_px > 0),
    pixel_x         double precision NOT NULL CHECK (pixel_x > 0),
    pixel_y         double precision NOT NULL CHECK (pixel_y > 0),
    x_min           double precision NOT NULL,
    y_min           double precision NOT NULL,
    x_max           double precision NOT NULL,
    y_max           double precision NOT NULL,
    bits_per_sample integer NOT NULL,
    sample_format   integer NOT NULL,
    compression     integer NOT NULL,
    nodata          text,
    qa_flags        text[]  NOT NULL DEFAULT '{}',
    footprint       geometry(Polygon, 25832) NOT NULL,
    is_current      boolean NOT NULL DEFAULT true,
    catalogued_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, name, file_sha256),
    CHECK (x_min < x_max AND y_min < y_max)
);
CREATE INDEX terrain_tile_footprint_idx
    ON staging.terrain_tile USING gist (footprint) WHERE is_current;
CREATE INDEX terrain_tile_source_idx ON staging.terrain_tile (source_id) WHERE is_current;

CREATE VIEW staging.terrain_tile_current AS
    SELECT * FROM staging.terrain_tile WHERE is_current;

COMMENT ON TABLE staging.terrain_tile IS
    'Terrain tile catalogue: footprint and properties of every GeoTIFF tile, read from its header.';
COMMENT ON COLUMN staging.terrain_tile.footprint IS
    'The rectangle the tile covers, from the header, in EPSG:25832 (a different system is flagged).';
COMMENT ON COLUMN staging.terrain_tile.qa_flags IS
    'Findings: crs_unexpected, pixel_size_unexpected, size_unexpected, off_grid, name_extent_mismatch, nodata_missing, not_float.';
COMMENT ON COLUMN staging.terrain_tile.is_current IS
    'True for the tiles of the set that was catalogued last.';

-- The catalogue is a stage of its own in the run history.
ALTER TABLE meta.pipeline_run DROP CONSTRAINT pipeline_run_stage_check;
ALTER TABLE meta.pipeline_run ADD CONSTRAINT pipeline_run_stage_check
    CHECK (stage IN ('check', 'fetch', 'load', 'diff', 'apply', 'validate', 'verify', 'catalogue'));
