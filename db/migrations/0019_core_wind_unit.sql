-- Core: the wind units that routing may start from, how sure we are of each position, and why.
--
-- Like the substations, the wind units of the core are derived: a build reads the current MaStR
-- units, the newest validated OSM snapshots and the boundaries, and puts every unit into three
-- nested scenarios. A build records the exact parameters it used and a hash of its content, so a
-- rebuild from the raw data must give the same hash. Nothing in staging is changed and no position
-- is moved: the registered position stays, and what the OSM turbine says is stored next to it.

-- One build of the wind units: the rules (with their parameters), the inputs and a hash of the result.
CREATE TABLE core.wind_build (
    wind_build_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rules_version   text   NOT NULL CHECK (rules_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    parameters      jsonb  NOT NULL,
    source_versions jsonb  NOT NULL,
    counts          jsonb  NOT NULL,
    content_hash    text   NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER wind_build_append_only BEFORE UPDATE OR DELETE ON core.wind_build
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER wind_build_no_truncate BEFORE TRUNCATE ON core.wind_build
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
REVOKE UPDATE, DELETE, TRUNCATE ON core.wind_build FROM cabler_app;

-- The wind units of one build. The scenarios are nested: strict (existing units with a position
-- that is confirmed or plausible) is inside likely (plus planned units), likely is inside
-- inclusive (plus every unit whose position is doubtful).
CREATE TABLE core.wind_unit (
    wind_build_id        bigint NOT NULL REFERENCES core.wind_build (wind_build_id),
    source_id            text   NOT NULL,
    unit_key             text   NOT NULL,
    unit_id              bigint NOT NULL REFERENCES staging.wind_unit (unit_id),
    state_key            text,
    status               text,
    status_group         text   NOT NULL CHECK (status_group IN ('existing', 'planned', 'retired')),
    name                 text,
    wind_farm            text,
    grid_location_key    text,
    municipality_key     text,
    municipality         text,
    place_key            text,
    gross_kw             numeric,
    hub_height_m         numeric,
    rotor_diameter_m     numeric,
    commissioned         date,
    qa_flags             text[] NOT NULL DEFAULT '{}',
    position_class       text   NOT NULL CHECK (position_class IN (
        'confirmed', 'plausible', 'doubtful', 'invalid', 'unlocated')),
    reason               text   NOT NULL,
    osm_source_id        text,
    osm_key              text,
    osm_distance_m       numeric CHECK (osm_distance_m >= 0),
    registered_distance_m numeric CHECK (registered_distance_m >= 0),
    farm_gap_m           numeric CHECK (farm_gap_m >= 0),
    in_strict            boolean NOT NULL,
    in_likely            boolean NOT NULL,
    in_inclusive         boolean NOT NULL,
    geom                 geometry(Point, 25832),
    PRIMARY KEY (wind_build_id, source_id, unit_key),
    CHECK ((NOT in_strict OR in_likely) AND (NOT in_likely OR in_inclusive)),
    CHECK (geom IS NOT NULL OR NOT in_inclusive),
    CHECK ((osm_source_id IS NULL) = (osm_key IS NULL) AND (osm_key IS NULL) = (osm_distance_m IS NULL))
);
CREATE INDEX wind_unit_core_geom_idx ON core.wind_unit USING gist (geom);
CREATE INDEX wind_unit_core_class_idx ON core.wind_unit (wind_build_id, position_class);

CREATE VIEW core.wind_unit_current AS
    SELECT * FROM core.wind_unit
    WHERE wind_build_id = (SELECT max(wind_build_id) FROM core.wind_build);

COMMENT ON TABLE core.wind_build IS
    'One build of the core wind units, with the parameters used, the inputs and a hash of the result.';
COMMENT ON TABLE core.wind_unit IS
    'MaStR wind units of one build with the quality of their position and their nested scenarios.';
COMMENT ON COLUMN core.wind_unit.position_class IS
    'confirmed: an OSM turbine within the confirm distance; plausible: nothing against it; doubtful: far from the registered municipality or from the rest of its wind farm; invalid: flagged coordinate; unlocated: none.';
COMMENT ON COLUMN core.wind_unit.registered_distance_m IS
    'Distance from the position to the municipality that the register names; 0 inside it, NULL when the register names none or the boundaries lack it.';
COMMENT ON COLUMN core.wind_unit.farm_gap_m IS
    'Distance to the nearest other unit of the same MaStR location (wind farm); NULL when there is none.';
