-- Re-checking stored tiles against their recorded checksums: a tile whose file is damaged or gone
-- is marked 'corrupt' (the file is kept aside as evidence), and the run is recorded as 'verify'.
-- A corrupt tile is fetched again by the next tiles run, which restores the same row.

ALTER TABLE meta.tile DROP CONSTRAINT tile_status_check;
ALTER TABLE meta.tile ADD CONSTRAINT tile_status_check
    CHECK (status IN ('fetched', 'superseded', 'removed', 'corrupt'));

ALTER TABLE meta.pipeline_run DROP CONSTRAINT pipeline_run_stage_check;
ALTER TABLE meta.pipeline_run ADD CONSTRAINT pipeline_run_stage_check
    CHECK (stage IN ('check', 'fetch', 'load', 'diff', 'apply', 'validate', 'verify'));
