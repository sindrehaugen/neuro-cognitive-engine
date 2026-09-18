-- 089_geodata_n50_land_cover.sql
--
-- Geodata FEED module, N50 land-cover local store (Wave F-12). Numbered 089,
-- not the next-after-084 085: main's highest was 084 when four lanes each
-- independently took "the next one" within the same hour. ML-orch's ruling
-- (ascending PR number takes ascending migration number) settled it as
-- 085 -> Wave F-11's OSM store (PR #239), 086 -> Lane A's legal-entity
-- register (PR #240), 087 -> Lane D's assets_shell_product (PR #242), 088
-- -> Lane B's sales resource tables (PR #245). This migration has no PR
-- yet, so it takes 089 up front rather than risking a fifth collision.
-- Still not guaranteed to survive to merge unchanged -- confirm the number
-- is still free immediately before opening this wave's PR.
--
-- Same shape decision as the host's own ADR 0047 ("Kartverket's N50 land
-- cover for all of Norway, local in Postgres") — the same reasoning as
-- ADR 0046 (Wave F-11's OSM store): a live per-request call to an external
-- source does not scale, so the data lives here and is refreshed by a
-- batch import.
--
-- GLOBAL, not tenant-scoped: land-cover classification is public
-- Kartverket data (CC BY 4.0), not a tenant's data — same reasoning as
-- geodata_osm_elements (migration 085, Wave F-11) and product_catalog
-- (migration 064). No namespace_id column; registered in
-- nce.event_log.EXPECTED_GLOBAL_TABLES (same PR).
--
-- No PostGIS (re-derived, not assumed, same as Wave F-11): the source
-- format is confusingly named "the PostGIS dump" but reading it needs no
-- such extension — this table's own read is a bounding-box intersection,
-- which box + GiST answers in core Postgres.

BEGIN;

CREATE TABLE IF NOT EXISTS geodata_n50_land_cover (
    id          BIGSERIAL   NOT NULL,
    -- Kartverket's own class name, e.g. 'myr' (bog), 'innsjo' (lake),
    -- 'skog' (forest), 'dyrketmark' (farmland), 'havflate' (sea). Free
    -- text, deliberately NOT a CHECK-constrained enum: the source's own
    -- selection is documented at ~24 classes, but this migration's source
    -- of truth (ADR 0047) evidences only a handful by name, and inventing
    -- the rest would be exactly the kind of unevidenced field this lane
    -- has repeatedly flagged rather than guessed at.
    klasse      TEXT        NOT NULL,
    -- Kartverket's own per-feature object id. Unique WITHIN a class (the
    -- source dump is one table per class), not necessarily across classes.
    objid       BIGINT      NOT NULL,
    -- Kartverket's own type label within the class, when the source
    -- carries one.
    objtype     TEXT,
    -- Polygon ring(s) in [lon, lat] DEGREES, holes included where the
    -- source has them — already converted from the source's UTM33
    -- (EPSG:25833) projection; this table's contract is degrees, never a
    -- projected coordinate (see the module's own docstring for why the
    -- conversion is out of scope here).
    rings       JSONB       NOT NULL,
    -- Precomputed on the source's PROJECTED (UTM) coordinates, before the
    -- degree conversion above -- an accurate m2 area cannot be recovered
    -- from lon/lat degrees alone (they are not equal-area), so this
    -- column is a required input, never derived by this table's own code.
    area_m2     NUMERIC,
    bbox        BOX         NOT NULL,
    source_file TEXT        NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT geodata_n50_land_cover_uq UNIQUE (klasse, objid),
    CONSTRAINT geodata_n50_land_cover_klasse_not_blank
        CHECK (btrim(klasse) <> ''),
    CONSTRAINT geodata_n50_land_cover_source_file_not_blank
        CHECK (btrim(source_file) <> '')
);

-- The one query this table exists to serve: "which land-cover polygons
-- intersect this viewport".
CREATE INDEX IF NOT EXISTS idx_geodata_n50_land_cover_bbox
    ON geodata_n50_land_cover USING gist (bbox);

-- A class-filtered read (e.g. "lakes only") stays index-friendly.
CREATE INDEX IF NOT EXISTS idx_geodata_n50_land_cover_klasse
    ON geodata_n50_land_cover (klasse);

-- ADR 0047's own truncation rule: when a bbox read is capped, the largest
-- features survive (a lake never falls out in favour of small bog holes).
CREATE INDEX IF NOT EXISTS idx_geodata_n50_land_cover_area_desc
    ON geodata_n50_land_cover (area_m2 DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE geodata_n50_land_cover FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE geodata_n50_land_cover TO nce_app;
        GRANT USAGE, SELECT ON SEQUENCE geodata_n50_land_cover_id_seq TO nce_app;
    END IF;
END $$;

COMMIT;
