-- Core: the rules of the wind unit builds get a table of their own, keyed by version.
--
-- Until now a rules version was defined by the parameters of the first build that used it, and a
-- build compared its parameters with that build. Two builds that started at the same time with a
-- new version could both pass that comparison. Now a version is a row of its own: the version is
-- the primary key, so a second, different set of parameters for it cannot be stored, and every
-- build names its version by a foreign key. The parameters move from the builds into this table.

CREATE TABLE core.wind_rule_set (
    version    text   PRIMARY KEY CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    parameters jsonb  NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER wind_rule_set_append_only BEFORE UPDATE OR DELETE ON core.wind_rule_set
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER wind_rule_set_no_truncate BEFORE TRUNCATE ON core.wind_rule_set
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
REVOKE UPDATE, DELETE, TRUNCATE ON core.wind_rule_set FROM cabler_app;

-- The versions of the builds made so far, with the parameters of the first build of each version.
INSERT INTO core.wind_rule_set (version, parameters, created_at)
SELECT DISTINCT ON (rules_version) rules_version, parameters, created_at
FROM core.wind_build
ORDER BY rules_version, wind_build_id;

-- A build whose parameters differ from those of its version would be lost by the move: refuse it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM core.wind_build b
        JOIN core.wind_rule_set r ON r.version = b.rules_version
        WHERE b.parameters <> r.parameters
    ) THEN
        RAISE EXCEPTION 'a wind build has other parameters than the first build of its version';
    END IF;
END
$$;

ALTER TABLE core.wind_build
    ADD CONSTRAINT wind_build_rules_version_fkey
        FOREIGN KEY (rules_version) REFERENCES core.wind_rule_set (version);
ALTER TABLE core.wind_build DROP COLUMN parameters;

COMMENT ON TABLE core.wind_rule_set IS
    'Numbered parameters of the wind unit builds. A version is never changed; a change is a new version.';
COMMENT ON COLUMN core.wind_rule_set.version IS
    'Version of the rules (major.minor.patch); the key that every wind build names.';
COMMENT ON COLUMN core.wind_rule_set.parameters IS
    'Every parameter of the rules except the version: sources, states, statuses, flags and distances in metres.';
COMMENT ON COLUMN core.wind_rule_set.created_at IS
    'When the version was first used.';
