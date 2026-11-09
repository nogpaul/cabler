-- Features of every source that is not OpenStreetMap, exactly as delivered, one row per feature per
-- loaded snapshot: wind units, land-cover polygons, protected areas and code catalogues.
-- Attributes stay as the publisher wrote them (text); typing and decoding happen on the way to
-- staging. Geometry keeps the coordinate system of the delivery; the system is part of the value.

CREATE TABLE raw.feature (
    version_id   bigint NOT NULL REFERENCES meta.source_version (version_id),
    layer        text   NOT NULL CHECK (layer ~ '^[a-z][a-z0-9_]*$'),
    feature_key  text   NOT NULL CHECK (feature_key <> ''),
    attributes   jsonb  NOT NULL,
    content_hash text   NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    geom         geometry(Geometry),
    PRIMARY KEY (version_id, layer, feature_key),
    CHECK (geom IS NULL OR ST_SRID(geom) > 0)
);
CREATE INDEX feature_geom_idx ON raw.feature USING gist (geom) WHERE geom IS NOT NULL;
CREATE INDEX feature_layer_idx ON raw.feature (version_id, layer);

COMMENT ON TABLE raw.feature IS
    'Features of non-OSM sources as delivered. feature_key is the publisher''s stable identifier.';
COMMENT ON COLUMN raw.feature.content_hash IS
    'SHA-256 over the attributes and the geometry; equal hashes mean an unchanged feature.';

-- What changed between two snapshots of such a source.
CREATE TABLE raw.feature_change (
    version_id          bigint NOT NULL REFERENCES meta.source_version (version_id),
    previous_version_id bigint REFERENCES meta.source_version (version_id),
    layer               text   NOT NULL,
    feature_key         text   NOT NULL,
    change              text   NOT NULL CHECK (change IN ('added', 'changed', 'removed')),
    changed_fields      text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (version_id, layer, feature_key),
    CHECK ((change = 'changed') = (cardinality(changed_fields) > 0))
);
CREATE INDEX feature_change_kind_idx ON raw.feature_change (version_id, change);

COMMENT ON TABLE raw.feature_change IS
    'For a changed feature: the attribute names whose value differs, plus geometry when it differs.';
