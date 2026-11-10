-- Core: the substations that routing may use, how sure we are about each, and who decided what.
--
-- The core is derived. core.substation is rebuilt from staging and from the human decisions; a
-- build is recorded in core.build with a hash of its content, so a rebuild from the raw data must
-- give the same hash. The only things that cannot be rebuilt are the decisions, and they live in
-- tables that refuse UPDATE, DELETE and TRUNCATE: for the application role by privilege, for every
-- role (the owner included) by trigger.

CREATE FUNCTION core.refuse_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is refused: the table is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$$;

-- A numbered set of rules and parameters. A version is never changed; a change is a new version.
CREATE TABLE core.rule_set (
    rule_set_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version     text   NOT NULL UNIQUE CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    parameters  jsonb  NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- One decision of a person about one substation. A change of mind is a new row that names the row
-- it replaces; the current decision is the newest one (core.review_decision_current).
CREATE TABLE core.review_decision (
    decision_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id              text   NOT NULL REFERENCES meta.source (source_id),
    layer                  text   NOT NULL CHECK (layer IN ('points', 'lines', 'multipolygons')),
    osm_key                text   NOT NULL CHECK (osm_key ~ '^[nwr][0-9]+$'),
    decision               text   NOT NULL
        CHECK (decision IN ('high_voltage', 'not_high_voltage', 'not_a_substation')),
    reason                 text   NOT NULL CHECK (btrim(reason) <> ''),
    reviewer               text   NOT NULL CHECK (btrim(reviewer) <> ''),
    evidence               jsonb  NOT NULL DEFAULT '{}'::jsonb,
    rule_set_id            bigint NOT NULL REFERENCES core.rule_set (rule_set_id),
    supersedes_decision_id bigint REFERENCES core.review_decision (decision_id),
    decided_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX review_decision_subject_idx
    ON core.review_decision (source_id, layer, osm_key, decision_id DESC);

-- The chain of decisions about one substation has no forks: a decision replaces the newest one
-- (or is the first), nothing else.
CREATE FUNCTION core.check_decision_chain() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    newest bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.source_id || '/' || NEW.layer || '/' || NEW.osm_key, 0));
    SELECT max(decision_id) INTO newest FROM core.review_decision
    WHERE source_id = NEW.source_id AND layer = NEW.layer AND osm_key = NEW.osm_key;
    IF NEW.supersedes_decision_id IS DISTINCT FROM newest THEN
        RAISE EXCEPTION 'a decision about %/%/% must replace the newest one (%), not %',
            NEW.source_id, NEW.layer, NEW.osm_key, newest, NEW.supersedes_decision_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER review_decision_chain BEFORE INSERT ON core.review_decision
    FOR EACH ROW EXECUTE FUNCTION core.check_decision_chain();

CREATE VIEW core.review_decision_current AS
    SELECT DISTINCT ON (source_id, layer, osm_key) *
    FROM core.review_decision
    ORDER BY source_id, layer, osm_key, decision_id DESC;

-- One build of the core: which rules, which snapshots, which decisions, and a hash of the result.
CREATE TABLE core.build (
    build_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_set_id      bigint NOT NULL REFERENCES core.rule_set (rule_set_id),
    source_versions  jsonb  NOT NULL,
    decisions_through bigint NOT NULL CHECK (decisions_through >= 0),
    counts           jsonb  NOT NULL,
    content_hash     text   NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER rule_set_append_only BEFORE UPDATE OR DELETE ON core.rule_set
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER rule_set_no_truncate BEFORE TRUNCATE ON core.rule_set
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER review_decision_append_only BEFORE UPDATE OR DELETE ON core.review_decision
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER review_decision_no_truncate BEFORE TRUNCATE ON core.review_decision
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER build_append_only BEFORE UPDATE OR DELETE ON core.build
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER build_no_truncate BEFORE TRUNCATE ON core.build
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
REVOKE UPDATE, DELETE, TRUNCATE ON core.rule_set, core.review_decision, core.build FROM cabler_app;

-- The substations of one build, each in the scenarios it belongs to. The scenarios are nested:
-- strict is inside likely, likely is inside inclusive.
CREATE TABLE core.substation (
    build_id         bigint NOT NULL REFERENCES core.build (build_id),
    source_id        text   NOT NULL,
    layer            text   NOT NULL,
    osm_key          text   NOT NULL,
    feature_id       bigint NOT NULL REFERENCES staging.osm_power_feature (feature_id),
    name             text,
    operator         text,
    substation_type  text,
    voltage_v        integer,
    voltage_source   text   NOT NULL CHECK (voltage_source IN ('tag', 'decision', 'none')),
    hint             text   CHECK (hint IN ('hv_candidate', 'conflict', 'likely_low_voltage', 'no_evidence')),
    decision         text,
    decision_id      bigint REFERENCES core.review_decision (decision_id),
    in_strict        boolean NOT NULL,
    in_likely        boolean NOT NULL,
    in_inclusive     boolean NOT NULL,
    reason           text   NOT NULL,
    tier             text   CHECK (tier IN ('A', 'B', 'C', 'D')),
    wind_units_in_reach integer NOT NULL CHECK (wind_units_in_reach >= 0),
    impact           integer NOT NULL CHECK (impact >= 0),
    audit_sample     boolean NOT NULL,
    location         geometry(Point, 25832) NOT NULL,
    PRIMARY KEY (build_id, source_id, layer, osm_key),
    CHECK ((NOT in_strict OR in_likely) AND (NOT in_likely OR in_inclusive))
);
CREATE INDEX substation_location_idx ON core.substation USING gist (location);
CREATE INDEX substation_tier_idx ON core.substation (build_id, tier);

CREATE VIEW core.substation_current AS
    SELECT * FROM core.substation WHERE build_id = (SELECT max(build_id) FROM core.build);

-- Who still has to be looked at: substations without a tagged voltage and without a decision, that
-- are decision-relevant (tier A), unresolved (tier C) or drawn for the audit sample.
CREATE VIEW core.review_queue AS
    SELECT s.source_id, s.layer, s.osm_key, s.name, s.operator, s.substation_type, s.tier,
           s.impact, s.wind_units_in_reach, s.hint, s.audit_sample, s.reason,
           ST_X(ST_Transform(s.location, 4326)) AS lon, ST_Y(ST_Transform(s.location, 4326)) AS lat
    FROM core.substation_current s
    LEFT JOIN core.review_decision_current d
           ON d.source_id = s.source_id AND d.layer = s.layer AND d.osm_key = s.osm_key
    WHERE d.decision_id IS NULL
      AND s.voltage_v IS NULL
      AND (s.tier IN ('A', 'C') OR s.audit_sample);

COMMENT ON TABLE core.review_decision IS
    'Decisions of people about substations. Append-only: a change of mind is a new row.';
COMMENT ON TABLE core.rule_set IS 'Numbered rules and parameters of a core build. Never changed.';
COMMENT ON TABLE core.build IS 'One build of the core, with the inputs and a hash of the result.';
COMMENT ON TABLE core.substation IS
    'Substations of one build with their membership in the nested scenarios strict, likely, inclusive.';
COMMENT ON COLUMN core.substation.tier IS
    'D no impact on any wind unit; otherwise A hints conflict or say high voltage, B hints agree it is not, C no evidence; NULL when a tag or a decision settles it.';
COMMENT ON VIEW core.review_queue IS
    'Substations a person should look at: tier A and C, and the audit sample.';
