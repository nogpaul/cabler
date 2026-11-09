-- Staging: land cover (CORINE) and protected areas (BfN), reprojected to EPSG:25832, with history.
--
-- One row is one version of one polygon. A row is current while valid_to_version_id is NULL; the
-- convention is the one of staging.osm_power_feature and staging.wind_unit. Geometry is repaired
-- when it is invalid (and flagged), never dropped.

-- The CORINE legend: the 44 classes of the nomenclature plus the three codes for "no data".
CREATE TABLE staging.clc_class (
    code     text PRIMARY KEY CHECK (code ~ '^[0-9]{3}$'),
    name     text NOT NULL,
    level1   text NOT NULL
);
INSERT INTO staging.clc_class (code, name, level1) VALUES
    ('111', 'Continuous urban fabric', 'Artificial surfaces'),
    ('112', 'Discontinuous urban fabric', 'Artificial surfaces'),
    ('121', 'Industrial or commercial units', 'Artificial surfaces'),
    ('122', 'Road and rail networks and associated land', 'Artificial surfaces'),
    ('123', 'Port areas', 'Artificial surfaces'),
    ('124', 'Airports', 'Artificial surfaces'),
    ('131', 'Mineral extraction sites', 'Artificial surfaces'),
    ('132', 'Dump sites', 'Artificial surfaces'),
    ('133', 'Construction sites', 'Artificial surfaces'),
    ('141', 'Green urban areas', 'Artificial surfaces'),
    ('142', 'Sport and leisure facilities', 'Artificial surfaces'),
    ('211', 'Non-irrigated arable land', 'Agricultural areas'),
    ('212', 'Permanently irrigated land', 'Agricultural areas'),
    ('213', 'Rice fields', 'Agricultural areas'),
    ('221', 'Vineyards', 'Agricultural areas'),
    ('222', 'Fruit trees and berry plantations', 'Agricultural areas'),
    ('223', 'Olive groves', 'Agricultural areas'),
    ('231', 'Pastures', 'Agricultural areas'),
    ('241', 'Annual crops associated with permanent crops', 'Agricultural areas'),
    ('242', 'Complex cultivation patterns', 'Agricultural areas'),
    ('243', 'Agriculture with significant areas of natural vegetation', 'Agricultural areas'),
    ('244', 'Agro-forestry areas', 'Agricultural areas'),
    ('311', 'Broad-leaved forest', 'Forest and semi-natural areas'),
    ('312', 'Coniferous forest', 'Forest and semi-natural areas'),
    ('313', 'Mixed forest', 'Forest and semi-natural areas'),
    ('321', 'Natural grasslands', 'Forest and semi-natural areas'),
    ('322', 'Moors and heathland', 'Forest and semi-natural areas'),
    ('323', 'Sclerophyllous vegetation', 'Forest and semi-natural areas'),
    ('324', 'Transitional woodland-shrub', 'Forest and semi-natural areas'),
    ('331', 'Beaches, dunes, sands', 'Forest and semi-natural areas'),
    ('332', 'Bare rocks', 'Forest and semi-natural areas'),
    ('333', 'Sparsely vegetated areas', 'Forest and semi-natural areas'),
    ('334', 'Burnt areas', 'Forest and semi-natural areas'),
    ('335', 'Glaciers and perpetual snow', 'Forest and semi-natural areas'),
    ('411', 'Inland marshes', 'Wetlands'),
    ('412', 'Peat bogs', 'Wetlands'),
    ('421', 'Salt marshes', 'Wetlands'),
    ('422', 'Salines', 'Wetlands'),
    ('423', 'Intertidal flats', 'Wetlands'),
    ('511', 'Water courses', 'Water bodies'),
    ('512', 'Water bodies', 'Water bodies'),
    ('521', 'Coastal lagoons', 'Water bodies'),
    ('522', 'Estuaries', 'Water bodies'),
    ('523', 'Sea and ocean', 'Water bodies'),
    ('990', 'Unclassified land surface', 'No data'),
    ('995', 'Unclassified water bodies', 'No data'),
    ('999', 'No data', 'No data');

CREATE TABLE staging.land_cover (
    land_cover_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             text   NOT NULL REFERENCES meta.source (source_id),
    feature_key           text   NOT NULL CHECK (feature_key <> ''),
    valid_from_version_id bigint NOT NULL REFERENCES meta.source_version (version_id),
    valid_to_version_id   bigint REFERENCES meta.source_version (version_id),
    clc_code              text,
    qa_flags              text[] NOT NULL DEFAULT '{}',
    geom                  geometry(MultiPolygon, 25832),
    UNIQUE (source_id, feature_key, valid_from_version_id),
    CHECK (valid_to_version_id IS NULL OR valid_to_version_id > valid_from_version_id)
);
CREATE UNIQUE INDEX land_cover_current_uq
    ON staging.land_cover (source_id, feature_key) WHERE valid_to_version_id IS NULL;
CREATE INDEX land_cover_geom_idx
    ON staging.land_cover USING gist (geom) WHERE valid_to_version_id IS NULL AND geom IS NOT NULL;
CREATE INDEX land_cover_code_idx
    ON staging.land_cover (clc_code) WHERE valid_to_version_id IS NULL;

CREATE VIEW staging.land_cover_current AS
    SELECT l.*, c.name AS class_name, c.level1 AS class_group
    FROM staging.land_cover l LEFT JOIN staging.clc_class c ON c.code = l.clc_code
    WHERE l.valid_to_version_id IS NULL;

CREATE FUNCTION staging.land_cover_as_of(p_source_id text, p_version_id bigint)
    RETURNS SETOF staging.land_cover
    LANGUAGE sql STABLE
    AS $$
        SELECT * FROM staging.land_cover
        WHERE source_id = p_source_id
          AND valid_from_version_id <= p_version_id
          AND (valid_to_version_id IS NULL OR valid_to_version_id > p_version_id)
    $$;

CREATE TABLE staging.protected_area (
    protected_area_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             text   NOT NULL REFERENCES meta.source (source_id),
    feature_key           text   NOT NULL CHECK (feature_key <> ''),
    valid_from_version_id bigint NOT NULL REFERENCES meta.source_version (version_id),
    valid_to_version_id   bigint REFERENCES meta.source_version (version_id),
    kind                  text   NOT NULL CHECK (kind IN (
        'nature_reserve', 'habitat_area', 'bird_area', 'national_park', 'landscape_area')),
    name                  text,
    state_code            text,
    site_code             text,
    iucn_category         text,
    designated            date,
    legal_title           text,
    area_ha               numeric,
    qa_flags              text[] NOT NULL DEFAULT '{}',
    geom                  geometry(MultiPolygon, 25832),
    UNIQUE (source_id, feature_key, valid_from_version_id),
    CHECK (valid_to_version_id IS NULL OR valid_to_version_id > valid_from_version_id)
);
CREATE UNIQUE INDEX protected_area_current_uq
    ON staging.protected_area (source_id, feature_key) WHERE valid_to_version_id IS NULL;
CREATE INDEX protected_area_geom_idx
    ON staging.protected_area USING gist (geom)
    WHERE valid_to_version_id IS NULL AND geom IS NOT NULL;
CREATE INDEX protected_area_kind_idx
    ON staging.protected_area (kind, state_code) WHERE valid_to_version_id IS NULL;

CREATE VIEW staging.protected_area_current AS
    SELECT * FROM staging.protected_area WHERE valid_to_version_id IS NULL;

CREATE FUNCTION staging.protected_area_as_of(p_source_id text, p_version_id bigint)
    RETURNS SETOF staging.protected_area
    LANGUAGE sql STABLE
    AS $$
        SELECT * FROM staging.protected_area
        WHERE source_id = p_source_id
          AND valid_from_version_id <= p_version_id
          AND (valid_to_version_id IS NULL OR valid_to_version_id > p_version_id)
    $$;

COMMENT ON TABLE staging.land_cover IS
    'CORINE land-cover polygons (5 ha minimum mapping unit), EPSG:25832, one row per polygon version.';
COMMENT ON COLUMN staging.land_cover.feature_key IS
    'Row number in the publisher file; CORINE has no identifier, so a new release re-keys everything.';
COMMENT ON COLUMN staging.land_cover.qa_flags IS
    'Findings: clc_code_unknown, geometry_missing, geometry_repaired, geometry_empty.';
COMMENT ON TABLE staging.protected_area IS
    'BfN protected areas of five kinds, EPSG:25832, one row per area version.';
COMMENT ON COLUMN staging.protected_area.state_code IS
    'Two-letter state code as delivered by the BfN (NW, HH, ...); not the municipality key of wind units.';
COMMENT ON COLUMN staging.protected_area.area_ha IS
    'Computed from the geometry, in hectares.';
COMMENT ON COLUMN staging.protected_area.qa_flags IS
    'Findings: geometry_missing, geometry_repaired, geometry_empty.';
