-- 102_billing_runs.sql
--
-- C12 BILLING_RUN resource (B-12, Lane G, dispatched by ML-orch under Sindre's
-- extended Q-5 carve-out: a new node type is permitted when it arrives with
-- its own migration and a stated owner engine -- owner: economy).
--
-- BILLING_RUN is kg_nodes-primary (identity: label, entity_type,
-- change_origin, timestamps -- kg_nodes has no attribute column of its own,
-- same finding every prior graph-primary wave has made). Real fields (the
-- period this run covers, its status, when it was confirmed) live on this
-- satellite table, joined to kg_nodes by label -- the exact FK pattern
-- migration 097 (sales_contacts) and migration 079 (system_design
-- satellites) both established: FOREIGN KEY (node_label, namespace_id)
-- REFERENCES kg_nodes (label, namespace_id) ON DELETE CASCADE.
--
-- One BILLING_RUN groups zero or more BILLING_CANDIDATEs (migration 103)
-- generated together for one namespace + billing period. Governed
-- (do_generate_billing_run, @governed, economy/billing_runs.py):
-- confirm=False produces nothing; a run that hits a room with no priced
-- category (nce/vertical_modules/agreements/room_category_pricing.py)
-- refuses the WHOLE run rather than generating a partial one silently
-- missing a room.

BEGIN;

CREATE TABLE IF NOT EXISTS economy_billing_runs (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label        TEXT        NOT NULL,
    period_start      DATE        NOT NULL,
    period_end        DATE        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'confirmed'
                      CHECK (status IN ('confirmed', 'failed')),
    -- Populated only when status = 'failed' -- names the category and the
    -- room the run refused on (room_category_pricing.UnpricedRoomCategoryError's
    -- message), so a failed run is diagnosable from this row alone.
    failure_reason    TEXT,
    candidate_count   INTEGER     NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label),
    CONSTRAINT fk_economy_billing_runs_kg_nodes
        FOREIGN KEY (node_label, namespace_id)
        REFERENCES kg_nodes (label, namespace_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_economy_billing_runs_namespace_node_label
    ON economy_billing_runs (namespace_id, node_label);

CREATE INDEX IF NOT EXISTS idx_economy_billing_runs_namespace_period
    ON economy_billing_runs (namespace_id, period_start, period_end);

ALTER TABLE economy_billing_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_billing_runs FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'economy_billing_runs' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON economy_billing_runs
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
