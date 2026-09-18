-- 086_geodata_osm_elements.sql
--
-- Geodata FEED module, OSM local store (Wave F-11). Numbered 086, not the
-- originally-claimed 085: Lane A's #240 (C15 legal-entity register) merged
-- to main as 085_legal_entity_register.sql while this PR was still open,
-- overtaking ML-orch's earlier "085 stays yours" ruling. Renumbered on this
-- rebase per the ascending-PR-number rule applied to what was actually left
-- free on main at rebase time.
--
-- A local, offline-imported copy of OpenStreetMap elements for a bounding-box
-- read path — the same shape decision as the host's own ADR 0046 ("Norway's
-- OSM data local in Postgres, not live against Overpass"): a shared-quota
-- Overpass API call per read does not scale, so the data lives here and is
-- refreshed by a batch import, never fetched live per request.
--
-- GLOBAL, not tenant-scoped: OpenStreetMap elements describe the physical
-- world, not a tenant's data — the same reasoning as product_catalog
-- (migration 064). No namespace_id column, RLS not applicable, registered in
-- nce.event_log.EXPECTED_GLOBAL_TABLES (same PR).
--
-- No PostGIS (re-derived, not assumed — this image does not carry the
-- extension, matching the host's own reason for the same choice): the read
-- this table exists to serve is a bounding-box intersection, which core
-- Postgres's box type + a GiST index answers without it.

BEGIN;

CREATE TABLE IF NOT EXISTS geodata_osm_elements (
    id          BIGSERIAL   NOT NULL,
    -- OSM's own three element kinds. A (osm_type, osm_id) pair is OSM's
    -- actual identity; osm_id alone is reused across the three kinds.
    osm_type    TEXT        NOT NULL,
    osm_id      BIGINT      NOT NULL,
    -- OSM's own key/value tags, unfiltered — a field this module does not
    -- recognise must not vanish silently on the way in.
    tags        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Overpass-shaped geometry (a node's point, or a way/relation's ordered
    -- ring(s)) — kept as the source's own shape rather than re-encoded,
    -- so a caller that already speaks Overpass's format needs no translation.
    geometry    JSONB       NOT NULL,
    -- The element's bounding box, precomputed at import time so a
    -- bbox-intersection read is an index probe, never a per-row geometry scan.
    bbox        BOX         NOT NULL,
    -- Which import run produced this row, and when — an OSM extract is a
    -- snapshot with a date, never assumed current (ADR 0046's own point:
    -- "the data now carries a date we own").
    source_file TEXT        NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT geodata_osm_elements_osm_uq UNIQUE (osm_type, osm_id),
    CONSTRAINT geodata_osm_elements_osm_type_check
        CHECK (osm_type IN ('node', 'way', 'relation')),
    CONSTRAINT geodata_osm_elements_source_file_not_blank
        CHECK (btrim(source_file) <> '')
);

-- The one query this table exists to serve: "which elements intersect this
-- viewport". && is core Postgres's box-overlap operator; no extension needed.
CREATE INDEX IF NOT EXISTS idx_geodata_osm_elements_bbox
    ON geodata_osm_elements USING gist (bbox);

-- Tag-filtered bbox reads (e.g. "buildings only") stay index-friendly on the
-- tag side too, rather than filtering the bbox match result row by row.
CREATE INDEX IF NOT EXISTS idx_geodata_osm_elements_tags
    ON geodata_osm_elements USING gin (tags);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE geodata_osm_elements FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE geodata_osm_elements TO nce_app;
        GRANT USAGE, SELECT ON SEQUENCE geodata_osm_elements_id_seq TO nce_app;
    END IF;
END $$;

COMMIT;
