-- Files of a source that publishes thousands of tiles listed in an index (terrain models).
-- One row per version of a tile; a tile changes when its size or time in the index changes.
-- Only the newest version of a tile has status 'fetched'; older ones are 'superseded', and a tile
-- that left the index is 'removed'. Files are never deleted.

CREATE TABLE meta.tile (
    source_id        text        NOT NULL REFERENCES meta.source (source_id),
    name             text        NOT NULL CHECK (name ~ '^[A-Za-z0-9._-]+$'),
    remote_timestamp timestamptz NOT NULL,
    size             bigint      NOT NULL CHECK (size >= 0),
    sha256           text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    local_path       text        NOT NULL,
    status           text        NOT NULL CHECK (status IN ('fetched', 'superseded', 'removed')),
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, name, remote_timestamp)
);
CREATE UNIQUE INDEX tile_current_uq ON meta.tile (source_id, name) WHERE status = 'fetched';

COMMENT ON TABLE meta.tile IS
    'Tiles fetched from an index-based source: checksum, location on disk and version history.';
