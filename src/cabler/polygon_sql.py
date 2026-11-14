"""SQL of the apply stage for polygon sources: CORINE land cover and the BfN protected areas.

Both are generic features (``raw.feature``) with a polygon geometry. They are reprojected to
EPSG:25832, made valid when they are not (and flagged), and stored as MultiPolygon with history.
A geometry that is missing or empty after repair is kept as such and flagged, not dropped.
"""

from __future__ import annotations

PROTECTED_KINDS = ("nature_reserve", "habitat_area", "bird_area", "national_park", "landscape_area")
_KIND_LIST = ", ".join(f"'{kind}'" for kind in PROTECTED_KINDS)

# Reprojects f.geom, repairs it if needed and exposes g.geom (MultiPolygon) and g.repaired.
_GEOMETRY = """
CROSS JOIN LATERAL (SELECT ST_Transform(f.geom, 25832) AS t) r
CROSS JOIN LATERAL (
    SELECT r.t IS NOT NULL AND NOT ST_IsValid(r.t) AS repaired,
           CASE WHEN r.t IS NULL THEN NULL
                WHEN ST_IsValid(r.t) THEN ST_Multi(r.t)
                ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(r.t), 3)) END AS geom
) g"""
_GEOMETRY_FLAGS = """CASE WHEN g.geom IS NULL THEN 'geometry_missing'
                WHEN ST_IsEmpty(g.geom) THEN 'geometry_empty' END,
           CASE WHEN g.repaired THEN 'geometry_repaired' END"""

# The area stated in the register and the area of the outline differ by rounding, by the projection
# (about 0.05 per cent) and, now and then, for real. One finding at most, the larger one first:
# over 10 per cent and 25 m2 is worth a look, over 1 per cent and 1 m2 is only counted.
_AREA_FLAG = """CASE WHEN g.geom IS NOT NULL AND official.v IS NOT NULL
                 AND abs(ST_Area(g.geom) - official.v) > greatest(25.0, 0.10 * official.v)
                THEN 'area_mismatch_large'
                WHEN g.geom IS NOT NULL AND official.v IS NOT NULL
                 AND abs(ST_Area(g.geom) - official.v) > greatest(1.0, 0.01 * official.v)
                THEN 'area_mismatch' END"""

LAND_COVER_CLOSE_SQL = """
UPDATE staging.land_cover u
SET valid_to_version_id = %(version)s
FROM raw.feature_change c
WHERE c.version_id = %(version)s
  AND c.layer = 'land_cover'
  AND c.change IN ('changed', 'removed')
  AND u.source_id = %(source)s
  AND u.feature_key = c.feature_key
  AND u.valid_to_version_id IS NULL
"""
LAND_COVER_OPEN_SQL = f"""
INSERT INTO staging.land_cover
    (source_id, feature_key, valid_from_version_id, clc_code, qa_flags, geom)
SELECT %(source)s, f.feature_key, %(version)s, f.attributes ->> 'CLC21',
       array_remove(ARRAY[
           CASE WHEN k.code IS NULL THEN 'clc_code_unknown' END,
           {_GEOMETRY_FLAGS}
       ], NULL),
       g.geom
FROM raw.feature_change c
JOIN raw.feature f
  ON f.version_id = c.version_id AND f.layer = c.layer AND f.feature_key = c.feature_key
LEFT JOIN staging.clc_class k ON k.code = f.attributes ->> 'CLC21'{_GEOMETRY}
WHERE c.version_id = %(version)s
  AND c.layer = 'land_cover'
  AND c.change IN ('added', 'changed')
"""  # noqa: S608 (the fragments are constants of this module)
LAND_COVER_CURRENT_SQL = """
SELECT count(*) FROM staging.land_cover
WHERE source_id = %s AND valid_to_version_id IS NULL
"""
LAND_COVER_KINDS_SQL = """
SELECT coalesce(c.level1, 'unknown'), count(*)
FROM staging.land_cover l LEFT JOIN staging.clc_class c ON c.code = l.clc_code
WHERE l.source_id = %s AND l.valid_to_version_id IS NULL
GROUP BY 1
"""
LAND_COVER_FLAGS_SQL = """
SELECT flag, count(*)
FROM staging.land_cover, unnest(qa_flags) AS flag
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY flag
"""

PROTECTED_CLOSE_SQL = f"""
UPDATE staging.protected_area u
SET valid_to_version_id = %(version)s
FROM raw.feature_change c
WHERE c.version_id = %(version)s
  AND c.layer IN ({_KIND_LIST})
  AND c.change IN ('changed', 'removed')
  AND u.source_id = %(source)s
  AND u.kind = c.layer
  AND u.feature_key = c.feature_key
  AND u.valid_to_version_id IS NULL
"""  # noqa: S608 (the fragments are constants of this module)
PROTECTED_OPEN_SQL = f"""
INSERT INTO staging.protected_area
    (source_id, feature_key, valid_from_version_id, kind, name, state_code, site_code,
     iucn_category, designated, legal_title, area_ha, qa_flags, geom)
SELECT %(source)s, f.feature_key, %(version)s, f.layer,
       f.attributes ->> 'NAME', f.attributes ->> 'BL', f.attributes ->> 'SITECODE',
       f.attributes ->> 'IUCN_KAT', staging.parse_date(f.attributes ->> 'LEG_DATE'),
       f.attributes ->> 'LEG_TITEL',
       CASE WHEN g.geom IS NOT NULL THEN round(ST_Area(g.geom)::numeric / 10000, 2) END,
       array_remove(ARRAY[
           {_GEOMETRY_FLAGS}
       ], NULL),
       g.geom
FROM raw.feature_change c
JOIN raw.feature f
  ON f.version_id = c.version_id AND f.layer = c.layer AND f.feature_key = c.feature_key{_GEOMETRY}
WHERE c.version_id = %(version)s
  AND c.layer IN ({_KIND_LIST})
  AND c.change IN ('added', 'changed')
"""  # noqa: S608 (the fragments are constants of this module)


def _parcel_close_sql(layer: str) -> str:
    return f"""
UPDATE staging.parcel u
SET valid_to_version_id = %(version)s
FROM raw.feature_change c
WHERE c.version_id = %(version)s
  AND c.layer = '{layer}'
  AND c.change IN ('changed', 'removed')
  AND u.source_id = %(source)s
  AND u.feature_key = c.feature_key
  AND u.valid_to_version_id IS NULL
"""  # noqa: S608 (the layer is a constant of this module)


PARCEL_CLOSE_SQL = _parcel_close_sql("parcel")
PARCEL_OPEN_SQL = f"""
INSERT INTO staging.parcel
    (source_id, feature_key, valid_from_version_id, state_key, district, municipality,
     municipality_key, cadastral_district, cadastral_district_key, field, number_numerator,
     number_denominator, location_text, area_official_m2, area_geometry_m2, updated, qa_flags,
     geom)
SELECT %(source)s, f.feature_key, %(version)s, f.attributes ->> 'landschl',
       f.attributes ->> 'kreis', f.attributes ->> 'gemeinde', f.attributes ->> 'gmdschl',
       f.attributes ->> 'gemarkung', f.attributes ->> 'gemaschl', f.attributes ->> 'flur',
       f.attributes ->> 'flstnrzae', f.attributes ->> 'flstnrnen', f.attributes ->> 'lagebeztxt',
       official.v,
       CASE WHEN g.geom IS NOT NULL THEN round(ST_Area(g.geom)::numeric, 2) END,
       staging.parse_date(f.attributes ->> 'aktualit'),
       array_remove(ARRAY[
           {_GEOMETRY_FLAGS},
           {_AREA_FLAG},
           CASE WHEN official.v IS NULL THEN 'area_missing' END
       ], NULL),
       g.geom
FROM raw.feature_change c
JOIN raw.feature f
  ON f.version_id = c.version_id AND f.layer = c.layer AND f.feature_key = c.feature_key
CROSS JOIN LATERAL (
    SELECT staging.parse_number(f.attributes ->> 'flaeche') AS v
) official{_GEOMETRY}
WHERE c.version_id = %(version)s
  AND c.layer = 'parcel'
  AND c.change IN ('added', 'changed')
"""  # noqa: S608 (the fragments are constants of this module)
PARCEL_NAS_CLOSE_SQL = _parcel_close_sql("parcel_nas")
# Hamburg's NAS export names things differently and has codes where NRW has names.
PARCEL_NAS_OPEN_SQL = f"""
INSERT INTO staging.parcel
    (source_id, feature_key, valid_from_version_id, state_key, municipality_key,
     cadastral_district_key, number_numerator, number_denominator, area_official_m2,
     area_geometry_m2, updated, qa_flags, geom)
SELECT %(source)s, f.feature_key, %(version)s, f.attributes ->> 'land', f.attributes ->> 'gemeinde',
       f.attributes ->> 'gemarkungsnummer', f.attributes ->> 'zaehler', f.attributes ->> 'nenner',
       official.v,
       CASE WHEN g.geom IS NOT NULL THEN round(ST_Area(g.geom)::numeric, 2) END,
       staging.parse_date(f.attributes ->> 'beginnt'),
       array_remove(ARRAY[
           {_GEOMETRY_FLAGS},
           {_AREA_FLAG},
           CASE WHEN official.v IS NULL THEN 'area_missing' END
       ], NULL),
       g.geom
FROM raw.feature_change c
JOIN raw.feature f
  ON f.version_id = c.version_id AND f.layer = c.layer AND f.feature_key = c.feature_key
CROSS JOIN LATERAL (
    SELECT staging.parse_number(f.attributes ->> 'amtlicheFlaeche') AS v
) official{_GEOMETRY}
WHERE c.version_id = %(version)s
  AND c.layer = 'parcel_nas'
  AND c.change IN ('added', 'changed')
"""  # noqa: S608 (the fragments are constants of this module)
PARCEL_NAS_KINDS_SQL = """
SELECT coalesce(state_key, 'unknown'), count(*) FROM staging.parcel
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY 1
"""
PARCEL_CURRENT_SQL = """
SELECT count(*) FROM staging.parcel
WHERE source_id = %s AND valid_to_version_id IS NULL
"""
PARCEL_KINDS_SQL = """
SELECT coalesce(district, 'unknown'), count(*) FROM staging.parcel
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY 1
"""
PARCEL_FLAGS_SQL = """
SELECT flag, count(*)
FROM staging.parcel, unnest(qa_flags) AS flag
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY flag
"""

ADMIN_LEVELS = ("municipality", "district")
_LEVEL_LIST = ", ".join(f"'{level}'" for level in ADMIN_LEVELS)

ADMIN_CLOSE_SQL = f"""
UPDATE staging.admin_area u
SET valid_to_version_id = %(version)s
FROM raw.feature_change c
WHERE c.version_id = %(version)s
  AND c.layer IN ({_LEVEL_LIST})
  AND c.change IN ('changed', 'removed')
  AND u.source_id = %(source)s
  AND u.level = c.layer
  AND u.feature_key = c.feature_key
  AND u.valid_to_version_id IS NULL
"""  # noqa: S608 (the fragments are constants of this module)
ADMIN_OPEN_SQL = f"""
INSERT INTO staging.admin_area
    (source_id, feature_key, valid_from_version_id, level, area_key, regional_key, name,
     designation, land_flag, state_key, qa_flags, geom)
SELECT %(source)s, f.feature_key, %(version)s, f.layer, f.attributes ->> 'AGS',
       f.attributes ->> 'ARS', f.attributes ->> 'GEN', f.attributes ->> 'BEZ',
       CASE WHEN f.attributes ->> 'GF' ~ '^[1-4]$' THEN (f.attributes ->> 'GF')::smallint END,
       f.attributes ->> 'SN_L',
       array_remove(ARRAY[
           CASE WHEN f.layer = 'municipality' AND f.attributes ->> 'AGS' !~ '^[0-9]{{8}}$'
                  OR f.layer = 'district' AND f.attributes ->> 'AGS' !~ '^[0-9]{{5}}$'
                  OR f.attributes ->> 'AGS' IS NULL THEN 'key_malformed' END,
           CASE WHEN f.attributes ->> 'GEN' IS NULL THEN 'name_missing' END,
           {_GEOMETRY_FLAGS}
       ], NULL),
       g.geom
FROM raw.feature_change c
JOIN raw.feature f
  ON f.version_id = c.version_id AND f.layer = c.layer AND f.feature_key = c.feature_key{_GEOMETRY}
WHERE c.version_id = %(version)s
  AND c.layer IN ({_LEVEL_LIST})
  AND c.change IN ('added', 'changed')
"""  # noqa: S608 (the fragments are constants of this module)
ADMIN_CURRENT_SQL = """
SELECT count(*) FROM staging.admin_area
WHERE source_id = %s AND valid_to_version_id IS NULL
"""
ADMIN_KINDS_SQL = """
SELECT level, count(*) FROM staging.admin_area
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY level
"""
ADMIN_FLAGS_SQL = """
SELECT flag, count(*)
FROM staging.admin_area, unnest(qa_flags) AS flag
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY flag
"""

PROTECTED_CURRENT_SQL = """
SELECT count(*) FROM staging.protected_area
WHERE source_id = %s AND valid_to_version_id IS NULL
"""
PROTECTED_KINDS_SQL = """
SELECT kind, count(*) FROM staging.protected_area
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY kind
"""
PROTECTED_FLAGS_SQL = """
SELECT flag, count(*)
FROM staging.protected_area, unnest(qa_flags) AS flag
WHERE source_id = %s AND valid_to_version_id IS NULL
GROUP BY flag
"""
