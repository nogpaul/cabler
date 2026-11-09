-- Staging: cadastral parcels (ALKIS), reprojected to EPSG:25832, with history.
--
-- One row is one version of one parcel. A row is current while valid_to_version_id is NULL; the
-- convention is the one of the other staging tables. A parcel is identified by its official key
-- (the Flurstueckskennzeichen), which does not change when the publisher's files are split
-- differently. Geometry is repaired when invalid (and flagged), never dropped. The official area
-- is kept as delivered next to the area of the geometry, so a disagreement is visible.

CREATE TABLE staging.parcel (
    parcel_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id              text   NOT NULL REFERENCES meta.source (source_id),
    feature_key            text   NOT NULL CHECK (feature_key <> ''),
    valid_from_version_id  bigint NOT NULL REFERENCES meta.source_version (version_id),
    valid_to_version_id    bigint REFERENCES meta.source_version (version_id),
    state_key              text,
    district               text,
    municipality           text,
    municipality_key       text,
    cadastral_district     text,
    cadastral_district_key text,
    field                  text,
    number_numerator       text,
    number_denominator     text,
    location_text          text,
    area_official_m2       numeric,
    area_geometry_m2       numeric,
    updated                date,
    qa_flags               text[] NOT NULL DEFAULT '{}',
    geom                   geometry(MultiPolygon, 25832),
    UNIQUE (source_id, feature_key, valid_from_version_id),
    CHECK (valid_to_version_id IS NULL OR valid_to_version_id > valid_from_version_id)
);
CREATE UNIQUE INDEX parcel_current_uq
    ON staging.parcel (source_id, feature_key) WHERE valid_to_version_id IS NULL;
CREATE INDEX parcel_geom_idx
    ON staging.parcel USING gist (geom) WHERE valid_to_version_id IS NULL AND geom IS NOT NULL;
CREATE INDEX parcel_municipality_idx
    ON staging.parcel (state_key, municipality_key) WHERE valid_to_version_id IS NULL;

CREATE VIEW staging.parcel_current AS
    SELECT * FROM staging.parcel WHERE valid_to_version_id IS NULL;

CREATE FUNCTION staging.parcel_as_of(p_source_id text, p_version_id bigint)
    RETURNS SETOF staging.parcel
    LANGUAGE sql STABLE
    AS $$
        SELECT * FROM staging.parcel
        WHERE source_id = p_source_id
          AND valid_from_version_id <= p_version_id
          AND (valid_to_version_id IS NULL OR valid_to_version_id > p_version_id)
    $$;

COMMENT ON TABLE staging.parcel IS
    'ALKIS parcels (without owner data), EPSG:25832, one row per parcel version.';
COMMENT ON COLUMN staging.parcel.feature_key IS
    'The official parcel key (Flurstueckskennzeichen), unique within a source.';
COMMENT ON COLUMN staging.parcel.area_official_m2 IS
    'The area stated in the register, in square metres, as delivered.';
COMMENT ON COLUMN staging.parcel.area_geometry_m2 IS
    'The area of the geometry in EPSG:25832, in square metres.';
COMMENT ON COLUMN staging.parcel.qa_flags IS
    'Findings: geometry_missing, geometry_repaired, geometry_empty, area_mismatch (over 1 per cent and 1 m2), area_mismatch_large (over 10 per cent and 25 m2).';
