-- The isolation level a wind unit build ran at, so that a build that did not get one snapshot says so.
--
-- Builds made before this column existed ran at the level of their connection, which was the default
-- (read committed) when a transaction was already open and repeatable read otherwise; they are
-- marked 'unknown' because nothing recorded it.

ALTER TABLE core.wind_build
    ADD COLUMN isolation text NOT NULL DEFAULT 'unknown'
        CHECK (isolation IN ('unknown', 'read committed', 'repeatable read', 'serializable'));

COMMENT ON COLUMN core.wind_build.isolation IS
    'The transaction isolation level of the build: repeatable read means that every input was read from one snapshot.';
