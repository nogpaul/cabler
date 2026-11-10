-- A parcel whose register area is missing or unreadable now carries the finding area_missing; the
-- comment on the column lists every finding.

COMMENT ON COLUMN staging.parcel.qa_flags IS
    'Findings: geometry_missing, geometry_repaired, geometry_empty, area_missing, area_mismatch (over 1 per cent and 1 m2), area_mismatch_large (over 10 per cent and 25 m2).';
