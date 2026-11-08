-- Validation of applied snapshots: one verdict per snapshot, the findings behind it, and a review
-- queue of substations whose voltage is not tagged.
--
-- A quarantined snapshot stays in staging (so the history stays complete) but must never be
-- promoted: promotion looks only at snapshots whose verdict is 'passed'.

CREATE TABLE staging.snapshot_validation (
    version_id   bigint PRIMARY KEY REFERENCES meta.source_version (version_id),
    source_id    text   NOT NULL REFERENCES meta.source (source_id),
    outcome      text   NOT NULL CHECK (outcome IN ('passed', 'quarantined')),
    errors       integer NOT NULL CHECK (errors >= 0),
    warnings     integer NOT NULL CHECK (warnings >= 0),
    validated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((outcome = 'quarantined') = (errors > 0))
);
CREATE INDEX snapshot_validation_source_idx ON staging.snapshot_validation (source_id, version_id DESC);

CREATE TABLE staging.validation_finding (
    finding_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES meta.source_version (version_id),
    rule       text   NOT NULL,
    severity   text   NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    feature_id bigint REFERENCES staging.osm_power_feature (feature_id),
    detail     jsonb  NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX validation_finding_rule_idx ON staging.validation_finding (version_id, rule);
CREATE INDEX validation_finding_feature_idx ON staging.validation_finding (feature_id);

-- Nearest-line lookups run on the shape of any row version, not only the current ones.
CREATE INDEX osm_power_feature_geom_idx ON staging.osm_power_feature USING gist (geom);

-- Substations without a tagged voltage in the newest validated snapshot, with the evidence found
-- for them. Evidence is only a hint: it never overwrites the tagged value.
CREATE VIEW staging.substation_review AS
WITH latest AS (
    SELECT DISTINCT ON (source_id) source_id, version_id
    FROM staging.snapshot_validation
    ORDER BY source_id, version_id DESC
), hints AS (
    SELECT f.feature_id,
           max((f.detail ->> 'hint_voltage_v')::integer)
               FILTER (WHERE f.rule = 'voltage_hint_line') AS line_hint_v,
           min((f.detail ->> 'distance_m')::numeric)
               FILTER (WHERE f.rule = 'voltage_hint_line') AS line_hint_distance_m,
           max(f.detail ->> 'line_osm_key')
               FILTER (WHERE f.rule = 'voltage_hint_line') AS line_osm_key,
           max(f.detail ->> 'hint_class')
               FILTER (WHERE f.rule = 'voltage_hint_type') AS type_hint_class,
           coalesce(bool_or(f.rule = 'voltage_hint_conflict'), false) AS conflict
    FROM staging.validation_finding f
    JOIN latest l ON l.version_id = f.version_id
    WHERE f.rule LIKE 'voltage_hint%'
    GROUP BY f.feature_id
)
SELECT p.source_id, p.layer, p.osm_key, p.feature_id, p.name, p.operator, p.substation_type,
       ST_X(ST_Transform(p.location, 4326)) AS lon, ST_Y(ST_Transform(p.location, 4326)) AS lat,
       h.line_hint_v, h.line_hint_distance_m, h.line_osm_key, h.type_hint_class,
       coalesce(h.conflict, false) AS conflict,
       CASE
           WHEN h.conflict THEN 'conflict'
           WHEN h.line_hint_v >= 110000 OR h.type_hint_class = 'high_plus' THEN 'hv_candidate'
           WHEN h.type_hint_class = 'medium_low' THEN 'likely_low_voltage'
           WHEN h.line_hint_v IS NOT NULL THEN 'likely_low_voltage'
           ELSE 'no_evidence'
       END AS suggestion
FROM staging.osm_power_feature p
JOIN latest l ON l.source_id = p.source_id
LEFT JOIN hints h ON h.feature_id = p.feature_id
WHERE p.kind = 'substation'
  AND p.voltage_v IS NULL
  AND p.valid_from_version_id <= l.version_id
  AND (p.valid_to_version_id IS NULL OR p.valid_to_version_id > l.version_id);

COMMENT ON TABLE staging.snapshot_validation IS
    'The verdict on an applied snapshot: passed, or quarantined because an error rule fired.';
COMMENT ON TABLE staging.validation_finding IS
    'Findings of the validate stage: snapshot-level rules and per-substation voltage hints.';
COMMENT ON VIEW staging.substation_review IS
    'Untagged-voltage substations with the hints found for them; suggestions need a human decision.';
