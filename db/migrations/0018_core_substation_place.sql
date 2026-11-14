-- Core: the place of every substation (municipality and district).
--
-- Most OpenStreetMap substations have no name, so a person who has to decide about one cannot tell
-- where it is. The place is looked up from the position against the administrative boundaries; it
-- is derived, never read from a tag, and it is part of the content hash of a build. A substation
-- that lies outside every boundary (or a build without boundaries) has no place.

ALTER TABLE core.substation
    ADD COLUMN municipality_key text CHECK (municipality_key IS NULL OR municipality_key ~ '^[0-9]{8}$'),
    ADD COLUMN municipality     text,
    ADD COLUMN district         text;
CREATE INDEX substation_municipality_idx ON core.substation (build_id, municipality_key);

-- The views list the columns they had when they were created; they get the new ones at the end.
CREATE OR REPLACE VIEW core.substation_current AS
    SELECT * FROM core.substation WHERE build_id = (SELECT max(build_id) FROM core.build);

CREATE OR REPLACE VIEW core.review_queue AS
    SELECT s.source_id, s.layer, s.osm_key, s.name, s.operator, s.substation_type, s.tier,
           s.impact, s.wind_units_in_reach, s.hint, s.audit_sample, s.reason,
           ST_X(ST_Transform(s.location, 4326)) AS lon, ST_Y(ST_Transform(s.location, 4326)) AS lat,
           s.municipality, s.district
    FROM core.substation_current s
    LEFT JOIN core.review_decision_current d
           ON d.source_id = s.source_id AND d.layer = s.layer AND d.osm_key = s.osm_key
    WHERE d.decision_id IS NULL
      AND s.voltage_v IS NULL
      AND (s.tier IN ('A', 'C') OR s.audit_sample);

COMMENT ON COLUMN core.substation.municipality_key IS
    'AGS of the municipality that covers the substation; NULL when no boundary covers it.';
COMMENT ON COLUMN core.substation.municipality IS 'Name of that municipality.';
COMMENT ON COLUMN core.substation.district IS 'Name of the district of that municipality.';
