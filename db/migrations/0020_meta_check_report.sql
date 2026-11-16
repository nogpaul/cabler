-- The reports of the exit check: one run of the check over every layer, and what each check found.
--
-- A report is evidence, not state: it records what was true when it was made, so it is never
-- changed. The tables refuse UPDATE, DELETE and TRUNCATE like the other records of this kind
-- (for the application role by privilege, for every role by trigger).

CREATE TABLE meta.check_run (
    check_run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    outcome      text   NOT NULL CHECK (outcome IN ('passed', 'failed')),
    errors       integer NOT NULL CHECK (errors >= 0),
    warnings     integer NOT NULL CHECK (warnings >= 0),
    options      jsonb  NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK ((outcome = 'failed') = (errors > 0))
);

CREATE TABLE meta.check_result (
    check_result_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_run_id    bigint NOT NULL REFERENCES meta.check_run (check_run_id),
    layer           text   NOT NULL,
    source_id       text   NOT NULL,
    check_name      text   NOT NULL,
    severity        text   NOT NULL CHECK (severity IN ('ok', 'info', 'warning', 'error')),
    bad             bigint NOT NULL CHECK (bad >= 0),
    total           bigint NOT NULL CHECK (total >= 0),
    message         text   NOT NULL DEFAULT ''
);
CREATE INDEX check_result_run_idx ON meta.check_result (check_run_id, layer, source_id);

CREATE TRIGGER check_run_append_only BEFORE UPDATE OR DELETE ON meta.check_run
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER check_run_no_truncate BEFORE TRUNCATE ON meta.check_run
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER check_result_append_only BEFORE UPDATE OR DELETE ON meta.check_result
    FOR EACH ROW EXECUTE FUNCTION core.refuse_change();
CREATE TRIGGER check_result_no_truncate BEFORE TRUNCATE ON meta.check_result
    FOR EACH STATEMENT EXECUTE FUNCTION core.refuse_change();
REVOKE UPDATE, DELETE, TRUNCATE ON meta.check_run, meta.check_result FROM cabler_app;

-- The newest report, one row per check.
CREATE VIEW meta.check_result_current AS
    SELECT * FROM meta.check_result
    WHERE check_run_id = (SELECT max(check_run_id) FROM meta.check_run);

COMMENT ON TABLE meta.check_run IS
    'One run of the exit check over every layer: the outcome and the number of errors and warnings.';
COMMENT ON TABLE meta.check_result IS
    'What one check found for one layer and source: how many rows were bad out of how many.';
COMMENT ON COLUMN meta.check_result.severity IS
    'ok (nothing found), info, warning (reported, never blocks) or error (the exit check fails).';
