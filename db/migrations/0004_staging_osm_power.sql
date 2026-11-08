-- Staging: the cleaned, classified, reprojected state of OpenStreetMap power objects, with history.
--
-- One row is one version of one object. A row is current while valid_to_version_id is NULL;
-- when the object changes or disappears, the row is closed (valid_to_version_id = the snapshot
-- that changed it) and, for a change, a new current row is opened. Snapshot ids of one source
-- increase with time, so "valid from" and "valid to" are snapshot ids.

-- Value of a key in GDAL's hstore text form: "key"=>"value", "key2"=>"value2"
CREATE FUNCTION staging.osm_tag(tags text, tag_key text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT substring(tags FROM format('"%s"=>"([^"]*)"', tag_key)) $$;

-- Highest voltage in volts of a tag such as '110000', '380000;110000' or '110000;medium'.
-- Tokens that are not plain whole numbers are ignored; nothing usable gives NULL.
CREATE FUNCTION staging.parse_voltage_v(raw text) RETURNS integer
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
        SELECT max(btrim(token)::integer)
        FROM unnest(string_to_array(raw, ';')) AS token
        WHERE btrim(token) ~ '^[0-9]{1,9}$'
    $$;

-- Electrical output in watts of a tag such as '3 MW', '600 kW', '2,4 MW' or '500' (watts).
-- Anything else, including lists and 'yes', gives NULL.
CREATE FUNCTION staging.parse_output_w(raw text) RETURNS bigint
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
        SELECT round(
            replace(m[1], ',', '.')::numeric * CASE lower(coalesce(m[2], 'w'))
                WHEN 'w' THEN 1 WHEN 'kw' THEN 1e3 WHEN 'mw' THEN 1e6 WHEN 'gw' THEN 1e9 END
        )::bigint
        FROM (SELECT regexp_match(btrim(raw), '^([0-9]+(?:[.,][0-9]+)?)\s*(W|kW|MW|GW)?$', 'i') AS m) s
        WHERE m IS NOT NULL
    $$;

CREATE FUNCTION staging.power_kind(power text, generator_source text, generator_method text)
    RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
        SELECT CASE power
            WHEN 'substation' THEN 'substation'
            WHEN 'line' THEN 'line'
            WHEN 'cable' THEN 'cable'
            WHEN 'minor_line' THEN 'minor_line'
            WHEN 'generator' THEN CASE
                WHEN generator_source = 'wind' OR generator_method = 'wind_turbine'
                THEN 'wind_turbine' ELSE 'generator' END
            WHEN 'tower' THEN 'support'
            WHEN 'pole' THEN 'support'
            WHEN 'portal' THEN 'support'
            WHEN 'catenary_mast' THEN 'support'
            ELSE 'other'
        END
    $$;

-- Voltage level of grid assets. Unknown when the voltage is missing or unreadable.
CREATE FUNCTION staging.voltage_class(kind text, voltage_v integer) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
        SELECT CASE
            WHEN kind NOT IN ('substation', 'line', 'cable') THEN NULL
            WHEN voltage_v IS NULL THEN 'unknown'
            WHEN voltage_v >= 220000 THEN 'extra_high'
            WHEN voltage_v >= 110000 THEN 'high'
            ELSE 'medium_low'
        END
    $$;

CREATE TABLE staging.osm_power_feature (
    feature_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             text   NOT NULL REFERENCES meta.source (source_id),
    layer                 text   NOT NULL CHECK (layer IN ('points', 'lines', 'multipolygons')),
    osm_key               text   NOT NULL CHECK (osm_key ~ '^[nwr][0-9]+$'),
    valid_from_version_id bigint NOT NULL REFERENCES meta.source_version (version_id),
    valid_to_version_id   bigint REFERENCES meta.source_version (version_id),
    kind                  text   NOT NULL CHECK (kind IN (
        'substation', 'line', 'cable', 'minor_line', 'wind_turbine', 'generator', 'support', 'other')),
    power                 text   NOT NULL,
    name                  text,
    ref                   text,
    operator              text,
    substation_type       text,
    voltage_raw           text,
    voltage_v             integer,
    voltage_class         text CHECK (voltage_class IN ('extra_high', 'high', 'medium_low', 'unknown')),
    generator_source      text,
    output_raw            text,
    output_w              bigint,
    qa_flags              text[] NOT NULL DEFAULT '{}',
    osm_version           integer,
    osm_timestamp         timestamptz,
    geom                  geometry(Geometry, 25832) NOT NULL,
    location              geometry(Point, 25832) NOT NULL,
    UNIQUE (source_id, layer, osm_key, valid_from_version_id),
    CHECK (valid_to_version_id IS NULL OR valid_to_version_id > valid_from_version_id)
);
CREATE UNIQUE INDEX osm_power_feature_current_uq
    ON staging.osm_power_feature (source_id, layer, osm_key) WHERE valid_to_version_id IS NULL;
CREATE INDEX osm_power_feature_location_idx
    ON staging.osm_power_feature USING gist (location) WHERE valid_to_version_id IS NULL;
CREATE INDEX osm_power_feature_kind_idx
    ON staging.osm_power_feature (kind) WHERE valid_to_version_id IS NULL;

CREATE VIEW staging.osm_power_current AS
    SELECT * FROM staging.osm_power_feature WHERE valid_to_version_id IS NULL;

-- The state of one source as it was when snapshot p_version_id had just been applied.
CREATE FUNCTION staging.osm_power_as_of(p_source_id text, p_version_id bigint)
    RETURNS SETOF staging.osm_power_feature
    LANGUAGE sql STABLE
    AS $$
        SELECT * FROM staging.osm_power_feature
        WHERE source_id = p_source_id
          AND valid_from_version_id <= p_version_id
          AND (valid_to_version_id IS NULL OR valid_to_version_id > p_version_id)
    $$;

COMMENT ON TABLE staging.osm_power_feature IS
    'OSM power objects, cleaned and classified, EPSG:25832, one row per object version.';
COMMENT ON COLUMN staging.osm_power_feature.qa_flags IS
    'Data-quality findings: voltage_missing, voltage_unparsable, output_missing, output_unparsable, geometry_repaired.';
COMMENT ON COLUMN staging.osm_power_feature.location IS
    'A point on the object (the point itself, or a point inside its line or outline).';
