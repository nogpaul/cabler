-- Tile names come from a publisher's index and may contain letters of any language: ALKIS NRW has
-- "gru_vereinf_05117000_Mülheim_ad_Ruhr_EPSG25832_GeoPackage.zip". The code refuses unsafe names
-- before anything is downloaded (cabler.tiles.is_safe_name); this constraint is the second line of
-- defence and refuses what could leave a folder or a URL: an empty name, a path separator, a
-- control character, a space, a query or fragment mark, or a leading dot.

ALTER TABLE meta.tile DROP CONSTRAINT tile_name_check;
ALTER TABLE meta.tile ADD CONSTRAINT tile_name_check
    CHECK (name <> '' AND name !~ '[/\\?#%[:space:][:cntrl:]]' AND left(name, 1) <> '.');
