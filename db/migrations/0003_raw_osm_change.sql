-- The change set between two snapshots of a source, one row per object that was added, changed
-- or removed. A snapshot's first diff has no predecessor: every object counts as added.

ALTER TABLE meta.source_version DROP CONSTRAINT source_version_status_check;
ALTER TABLE meta.source_version ADD CONSTRAINT source_version_status_check
    CHECK (status IN ('discovered', 'fetched', 'loaded', 'diffed', 'applied', 'failed', 'quarantined'));

CREATE TABLE raw.osm_change (
    version_id          bigint NOT NULL REFERENCES meta.source_version (version_id),
    previous_version_id bigint REFERENCES meta.source_version (version_id),
    layer               text   NOT NULL CHECK (layer IN ('points', 'lines', 'multipolygons')),
    osm_key             text   NOT NULL CHECK (osm_key ~ '^[nwr][0-9]+$'),
    change              text   NOT NULL CHECK (change IN ('added', 'changed', 'removed')),
    changed_fields      text[] NOT NULL DEFAULT '{}',
    old_osm_version     integer,
    new_osm_version     integer,
    PRIMARY KEY (version_id, layer, osm_key),
    CHECK ((change = 'changed') = (cardinality(changed_fields) > 0))
);
CREATE INDEX osm_change_kind_idx ON raw.osm_change (version_id, change);

COMMENT ON TABLE raw.osm_change IS
    'What changed in version_id compared with previous_version_id (NULL for a first snapshot).';
COMMENT ON COLUMN raw.osm_change.changed_fields IS
    'For a changed object: the columns whose value differs, plus geometry when the shape differs.';
