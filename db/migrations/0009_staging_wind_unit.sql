-- Staging: wind units of the Marktstammdatenregister (MaStR), decoded, typed and located, with history.
--
-- One row is one version of one unit. A row is current while valid_to_version_id is NULL; the
-- convention is the one of staging.osm_power_feature. Catalogue codes (status, technology,
-- manufacturer, ...) are decoded from the catalogue delivered in the same snapshot; the codes
-- stay in raw.feature. Nothing is corrected: coordinates that cannot be right are kept as
-- delivered and carry a qa_flag, so that the decision to use or drop them is visible and reversible.

-- A number from text such as '2000.000'; anything else gives NULL.
CREATE FUNCTION staging.parse_number(raw text) RETURNS numeric
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT CASE WHEN btrim(raw) ~ '^-?[0-9]{1,15}(\.[0-9]+)?$' THEN btrim(raw)::numeric END $$;

-- A date from text such as '2015-02-04' or '2026-06-03T09:59:16.22'; anything else, including an
-- impossible date, gives NULL.
CREATE FUNCTION staging.parse_date(raw text) RETURNS date
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
    AS $$
    BEGIN
        IF raw ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
            RETURN left(raw, 10)::date;
        END IF;
        RETURN NULL;
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END
    $$;

-- Coarse bounding boxes (EPSG:4326) used to notice coordinates in the wrong place. They are boxes,
-- not borders: a point inside the box of its state is not proven right, a point outside is wrong.
CREATE TABLE staging.state_box (
    state_key text PRIMARY KEY CHECK (state_key ~ '^[0-9]{2}$'),
    name      text NOT NULL,
    lon_min   numeric NOT NULL,
    lat_min   numeric NOT NULL,
    lon_max   numeric NOT NULL,
    lat_max   numeric NOT NULL,
    CHECK (lon_min < lon_max AND lat_min < lat_max)
);
INSERT INTO staging.state_box VALUES
    ('02', 'Hamburg',                  9.70, 53.35, 10.40, 53.75),
    ('05', 'Nordrhein-Westfalen',      5.85, 50.30,  9.55, 52.55);

CREATE TABLE staging.wind_unit (
    unit_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             text   NOT NULL REFERENCES meta.source (source_id),
    unit_key              text   NOT NULL CHECK (unit_key <> ''),
    valid_from_version_id bigint NOT NULL REFERENCES meta.source_version (version_id),
    valid_to_version_id   bigint REFERENCES meta.source_version (version_id),
    state_key             text,
    status_code           integer,
    status                text,
    name                  text,
    wind_farm             text,
    municipality_key      text,
    municipality          text,
    district              text,
    postcode              text,
    manufacturer          text,
    type_designation      text,
    location_type         text,
    gross_kw              numeric,
    net_kw                numeric,
    hub_height_m          numeric,
    rotor_diameter_m      numeric,
    commissioned          date,
    planned_commissioning date,
    decommissioned        date,
    grid_location_key     text,
    qa_flags              text[] NOT NULL DEFAULT '{}',
    geom                  geometry(Point, 25832),
    UNIQUE (source_id, unit_key, valid_from_version_id),
    CHECK (valid_to_version_id IS NULL OR valid_to_version_id > valid_from_version_id)
);
CREATE UNIQUE INDEX wind_unit_current_uq
    ON staging.wind_unit (source_id, unit_key) WHERE valid_to_version_id IS NULL;
CREATE INDEX wind_unit_geom_idx
    ON staging.wind_unit USING gist (geom) WHERE valid_to_version_id IS NULL AND geom IS NOT NULL;
CREATE INDEX wind_unit_state_status_idx
    ON staging.wind_unit (state_key, status_code) WHERE valid_to_version_id IS NULL;

CREATE VIEW staging.wind_unit_current AS
    SELECT * FROM staging.wind_unit WHERE valid_to_version_id IS NULL;

-- The state of one source as it was when snapshot p_version_id had just been applied.
CREATE FUNCTION staging.wind_unit_as_of(p_source_id text, p_version_id bigint)
    RETURNS SETOF staging.wind_unit
    LANGUAGE sql STABLE
    AS $$
        SELECT * FROM staging.wind_unit
        WHERE source_id = p_source_id
          AND valid_from_version_id <= p_version_id
          AND (valid_to_version_id IS NULL OR valid_to_version_id > p_version_id)
    $$;

COMMENT ON TABLE staging.wind_unit IS
    'MaStR wind units, decoded and typed, EPSG:25832, one row per unit version.';
COMMENT ON COLUMN staging.wind_unit.state_key IS
    'First two digits of the official municipality key: 02 Hamburg, 05 Nordrhein-Westfalen, ...';
COMMENT ON COLUMN staging.wind_unit.qa_flags IS
    'Findings: coordinate_missing, coordinate_placeholder, coordinate_outside_germany, coordinate_outside_state, capacity_missing, capacity_implausible, date_unparsable.';
COMMENT ON COLUMN staging.wind_unit.geom IS
    'The registered position, reprojected; NULL only when the register has none. Check qa_flags before use.';
