-- 090_geodata_place_names.sql
--
-- Geodata FEED module, place-name nearest-point lookup (Wave F-13). Numbered
-- 090: main's highest was 086 (Wave F-11) when this branch was cut, with
-- 087/088/089 already reserved for Lane D (PR #242), Lane B (PR #245), and
-- Lane F Wave F-12 (PR #248) respectively -- confirmed via
-- `git ls-tree origin/main -- nce/migrations/` plus ML-orch's collision
-- ruling before taking this number.
--
-- Grounded in the host's own ADR ("Kartverket's place names local in
-- Postgres") -- a DIFFERENT read shape from Waves F-11/F-12's bbox
-- intersection: this is a nearest-point lookup ("what named place is
-- closest to this coordinate?"), not "what intersects this viewport?".
-- One anchor point per named place, not its full source geometry (a place
-- can be a point, a point-swarm, a curve or an area in the source; only the
-- node nearest that shape's own centroid is kept) -- the alternative was
-- tens of millions of rows for a marginally prettier answer.
--
-- GLOBAL, not tenant-scoped: place names describe the physical world, not
-- a tenant's data — the same reasoning as product_catalog (migration 064)
-- and the OSM/N50 geodata stores (migrations 086/089). No namespace_id
-- column; registered in nce.event_log.EXPECTED_GLOBAL_TABLES (same PR).
--
-- No PostGIS: a nearest-point lookup is core Postgres's own strength --
-- ``gist(point)`` supports K-nearest-neighbour ordering via the ``<->``
-- operator natively. GiST orders candidates by DEGREE distance, which is
-- not proportional to real distance except at the equator; this module's
-- own query re-ranks the GiST-returned candidate set by an actual
-- great-circle distance in metres before returning the true nearest, the
-- same two-stage shape the host's own ADR describes taking (measured, not
-- assumed: an un-reranked answer was wrong on land in every case the host
-- checked, not just imprecise).

BEGIN;

CREATE TABLE IF NOT EXISTS geodata_place_names (
    id          BIGSERIAL   NOT NULL,
    external_id TEXT        NOT NULL,
    navn        TEXT        NOT NULL,
    kategori    TEXT,
    sprak       TEXT,
    point       POINT       NOT NULL,
    source_file TEXT        NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT geodata_place_names_external_id_uq UNIQUE (external_id),
    CONSTRAINT geodata_place_names_navn_not_blank CHECK (btrim(navn) <> ''),
    CONSTRAINT geodata_place_names_source_file_not_blank CHECK (btrim(source_file) <> '')
);

CREATE INDEX IF NOT EXISTS idx_geodata_place_names_point
    ON geodata_place_names USING gist (point);

CREATE INDEX IF NOT EXISTS idx_geodata_place_names_kategori
    ON geodata_place_names (kategori);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE geodata_place_names FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE geodata_place_names TO nce_app;
        GRANT USAGE, SELECT ON SEQUENCE geodata_place_names_id_seq TO nce_app;
    END IF;
END $$;

COMMIT;
