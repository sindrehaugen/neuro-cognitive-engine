-- 104_system_design_design_requests.sql
--
-- C12 DESIGN_REQUEST resource surface (charter Wave C-4, Lane E). Renumbered
-- twice on rebase: originally cut as 104, renumbered to 101 (2026-09-20)
-- when the reserved-range allocation across four concurrent lanes turned out
-- to be incompatible with a CI gate requiring a contiguous migration
-- sequence, then renumbered back to 104 (2026-09-20, later the same day)
-- once main had independently landed 100-103 (procurement_deal_registrations,
-- quote_templates_and_packages, and the two B-12 billing tables) while this
-- branch was held -- per the corrected standing rule (never pre-allocate
-- migration numbers across parallel lanes; take the next free contiguous
-- number as the last action before merge).
--
-- DESIGN_REQUEST (Solution Design intake queue, losningsdesign-ko) has run
-- since Wave C-4/#300 entirely on top of system_design_geometry -- a table
-- reserved for actual geometry payloads validated by validate_geometry()'s
-- single choke point -- using node_label='DESIGN_REQUEST:<id>' as a key into
-- that table's meta JSONB column. It never wrote a kg_nodes identity row at
-- all, and is absent from node-ownership.json entirely: neither a real node
-- type nor routable through SecondaryTable (which is off-limits for
-- system_design_geometry, same restriction DEVICE/RACK/CABLE and DESIGN
-- both hit). This migration gives it what every other owned node type has:
-- a kg_nodes identity row (entity_type='DESIGN_REQUEST') and a real
-- satellite table for its actual fields, following the exact pattern
-- migration 097 (sales_contacts / CONTACT) established: composite FK to
-- kg_nodes(label, namespace_id), RLS enabled AND forced, one policy with
-- both USING and WITH CHECK.
--
-- Status/priority CHECK constraints mirror the Python-side ALLOWED_STATUSES/
-- ALLOWED_PRIORITIES frozensets in
-- nce/vertical_modules/system_design/design_requests.py verbatim -- if
-- those ever diverge, this is the DDL that needs to change first.
--
-- functional_location_id is stored as the already-"FL:<id>"-prefixed label
-- (matching the existing meta_payload shape exactly, so a caller reading
-- this column sees the identical string the old JSONB blob returned);
-- quote_id is stored RAW/unprefixed, also matching the existing behavior
-- (an inconsistency inherited from the code being replaced, not introduced
-- here -- changing it would be a silent behavior change for from_quote.py's
-- consumers, which key off the raw value).
--
-- BACKFILL, not just a fresh table (ML-orch review, 2026-09-20): this repo
-- has no live database this session can query, so whether any real
-- namespace holds existing DESIGN_REQUEST rows under the old scheme is
-- genuinely unknown here -- not "confirmed empty". Rather than assume, this
-- migration copies every existing 'DESIGN_REQUEST:%'-labeled row out of
-- system_design_geometry.meta into the new table AND backfills the kg_nodes
-- identity row it never had (both idempotent, both a no-op on a database
-- with zero such rows -- same "no-op by design" shape as migration 081).
-- Every reader of the old key (design_requests.py's own six functions --
-- confirmed by grep to be the ONLY code touching DESIGN_REQUEST storage
-- anywhere in nce/) is being cut over to the new table in this same PR, so
-- no reader is left pointing at the old location. The old
-- system_design_geometry rows are deliberately NOT deleted here -- cleanup
-- of now-dead rows is a separate, later, lower-stakes concern than getting
-- the cutover itself right.

BEGIN;

CREATE TABLE IF NOT EXISTS system_design_design_requests (
    id                      UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id            UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label              TEXT        NOT NULL,
    title                   TEXT        NOT NULL,
    description             TEXT        NOT NULL DEFAULT '',
    quote_id                TEXT,
    functional_location_id  TEXT,
    status                  TEXT        NOT NULL DEFAULT 'pending',
    priority                TEXT        NOT NULL DEFAULT 'normal',
    owner_id                TEXT,
    design_id               TEXT,
    room_spec               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    metadata                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    completed_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label),
    CONSTRAINT fk_sddr_kg_nodes
        FOREIGN KEY (node_label, namespace_id)
        REFERENCES kg_nodes (label, namespace_id)
        ON DELETE CASCADE,
    CONSTRAINT system_design_design_requests_status_check
        CHECK (status IN ('pending', 'assigned', 'in_progress', 'completed', 'cancelled', 'rejected')),
    CONSTRAINT system_design_design_requests_priority_check
        CHECK (priority IN ('low', 'normal', 'high', 'urgent'))
);

CREATE INDEX IF NOT EXISTS idx_sddr_namespace_node_label
    ON system_design_design_requests (namespace_id, node_label);

CREATE INDEX IF NOT EXISTS idx_sddr_namespace_status
    ON system_design_design_requests (namespace_id, status);

CREATE INDEX IF NOT EXISTS idx_sddr_namespace_owner
    ON system_design_design_requests (namespace_id, owner_id);

CREATE INDEX IF NOT EXISTS idx_sddr_namespace_quote
    ON system_design_design_requests (namespace_id, quote_id);

ALTER TABLE system_design_design_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_design_requests FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'system_design_design_requests' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON system_design_design_requests
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- Backfill 1: a kg_nodes identity row for every existing DESIGN_REQUEST
-- label -- the old scheme never wrote one. ON CONFLICT DO NOTHING because a
-- row created fresh (post-cutover) may already have one.
INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
SELECT node_label, 'DESIGN_REQUEST', namespace_id, 'operator'
FROM system_design_geometry
WHERE node_label LIKE 'DESIGN_REQUEST:%'
ON CONFLICT (label, namespace_id) DO NOTHING;

-- Backfill 2: the satellite row itself, mapped field-for-field from the old
-- meta JSONB shape (see create_design_request's meta_payload in
-- design_requests.py for the shape this mirrors exactly). COALESCE covers a
-- key that predates a field being added to meta_payload; ON CONFLICT DO
-- NOTHING because a request created fresh after cutover already has a row
-- here and must not be overwritten by a stale geometry-table copy.
INSERT INTO system_design_design_requests
    (namespace_id, node_label, title, description, quote_id,
     functional_location_id, status, priority, owner_id, design_id,
     room_spec, metadata, completed_at, created_at, updated_at)
SELECT
    namespace_id,
    node_label,
    COALESCE(meta->>'title', ''),
    COALESCE(meta->>'description', ''),
    meta->>'quote_id',
    meta->>'functional_location_id',
    COALESCE(meta->>'status', 'pending'),
    COALESCE(meta->>'priority', 'normal'),
    meta->>'owner_id',
    meta->>'design_id',
    COALESCE(meta->'room_spec', '{}'::jsonb),
    COALESCE(meta->'metadata', '{}'::jsonb),
    NULLIF(meta->>'completed_at', '')::timestamptz,
    created_at,
    updated_at
FROM system_design_geometry
WHERE node_label LIKE 'DESIGN_REQUEST:%'
ON CONFLICT (namespace_id, node_label) DO NOTHING;

COMMIT;
