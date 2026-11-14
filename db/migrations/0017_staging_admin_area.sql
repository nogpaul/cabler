-- Staging: administrative areas (BKG VG250), reprojected to EPSG:25832, with history.
--
-- One row is one version of one polygon of one level. A row is current while valid_to_version_id
-- is NULL; the convention is the one of staging.protected_area. The publisher's object identifier
-- (OBJID) is the feature key: the municipality key is not unique, because a municipality at the
-- coast or on a lake has one row for its land and one for its water (land_flag 4 and 2).

CREATE TABLE staging.admin_area (
    admin_area_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             text   NOT NULL REFERENCES meta.source (source_id),
    feature_key           text   NOT NULL CHECK (feature_key <> ''),
    valid_from_version_id bigint NOT NULL REFERENCES meta.source_version (version_id),
    valid_to_version_id   bigint REFERENCES meta.source_version (version_id),
    level                 text   NOT NULL CHECK (level IN ('municipality', 'district')),
    area_key              text,
    regional_key          text,
    name                  text,
    designation           text,
    land_flag             smallint CHECK (land_flag BETWEEN 1 AND 4),
    state_key             text,
    qa_flags              text[] NOT NULL DEFAULT '{}',
    geom                  geometry(MultiPolygon, 25832),
    UNIQUE (source_id, feature_key, valid_from_version_id),
    CHECK (valid_to_version_id IS NULL OR valid_to_version_id > valid_from_version_id)
);
CREATE UNIQUE INDEX admin_area_current_uq
    ON staging.admin_area (source_id, level, feature_key) WHERE valid_to_version_id IS NULL;
-- Not partial: the as-of function reads closed rows too, and a place lookup must use the index.
CREATE INDEX admin_area_geom_idx ON staging.admin_area USING gist (geom) WHERE geom IS NOT NULL;
CREATE INDEX admin_area_key_idx ON staging.admin_area (source_id, level, area_key);

CREATE VIEW staging.admin_area_current AS
    SELECT * FROM staging.admin_area WHERE valid_to_version_id IS NULL;

CREATE FUNCTION staging.admin_area_as_of(p_source_id text, p_version_id bigint)
    RETURNS SETOF staging.admin_area
    LANGUAGE sql STABLE
    AS $$
        SELECT * FROM staging.admin_area
        WHERE source_id = p_source_id
          AND valid_from_version_id <= p_version_id
          AND (valid_to_version_id IS NULL OR valid_to_version_id > p_version_id)
    $$;

COMMENT ON TABLE staging.admin_area IS
    'Municipalities and districts (BKG VG250), EPSG:25832, one row per polygon version.';
COMMENT ON COLUMN staging.admin_area.feature_key IS 'OBJID of the publisher; unique per polygon.';
COMMENT ON COLUMN staging.admin_area.area_key IS
    'AGS: 8 digits for a municipality, 5 for a district. Not unique: land and water parts share it.';
COMMENT ON COLUMN staging.admin_area.land_flag IS
    'GF of the publisher: 4 land with structure, 3 land without, 2 water with structure, 1 water without.';
COMMENT ON COLUMN staging.admin_area.qa_flags IS
    'Findings: key_malformed, name_missing, geometry_missing, geometry_repaired, geometry_empty.';
