-- Catalogue of data sources, the versions fetched from them, and every pipeline run.
-- Runs as cabler_owner; default privileges give cabler_app read/write and cabler_ro read access.

CREATE TABLE meta.source (
    source_id    text PRIMARY KEY CHECK (source_id ~ '^[a-z0-9_]+$'),
    title        text NOT NULL,
    publisher    text NOT NULL,
    licence_id   text NOT NULL,
    licence_url  text NOT NULL,
    attribution  text NOT NULL,
    homepage     text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE meta.source IS 'One row per data source, with its licence and the credit line to print.';

CREATE TABLE meta.source_version (
    version_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id             text NOT NULL REFERENCES meta.source (source_id),
    remote_url            text NOT NULL,
    remote_etag           text,
    remote_last_modified  timestamptz,
    content_length        bigint CHECK (content_length >= 0),
    sha256                text CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    local_path            text,
    status                text NOT NULL DEFAULT 'discovered'
        CHECK (status IN ('discovered', 'fetched', 'loaded', 'applied', 'failed', 'quarantined')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, sha256)
);
CREATE INDEX source_version_latest_idx ON meta.source_version (source_id, created_at DESC);
COMMENT ON TABLE meta.source_version IS 'An immutable, checksummed snapshot of a source as fetched.';

CREATE TABLE meta.pipeline_run (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage        text NOT NULL CHECK (stage IN ('check', 'fetch', 'load', 'diff', 'apply', 'validate')),
    source_id    text NOT NULL REFERENCES meta.source (source_id),
    version_id   bigint REFERENCES meta.source_version (version_id),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    result       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text
);
CREATE INDEX pipeline_run_source_idx ON meta.pipeline_run (source_id, started_at DESC);
COMMENT ON TABLE meta.pipeline_run IS 'One row per pipeline stage execution, with its structured result.';
