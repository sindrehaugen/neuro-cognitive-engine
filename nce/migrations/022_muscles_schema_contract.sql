-- Migration: muscles schema contract freeze
-- A1. Provenance columns on memories, kg_nodes, kg_edges
ALTER TABLE memories ADD COLUMN IF NOT EXISTS change_origin TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_change_origin_chk;
ALTER TABLE memories ADD CONSTRAINT memories_change_origin_chk CHECK (change_origin IN
  ('sync','webhook','agent','operator','consolidation','replay','unknown')) NOT VALID;
ALTER TABLE memories VALIDATE CONSTRAINT memories_change_origin_chk;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS origin_event_id UUID;

ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS change_origin TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE kg_nodes DROP CONSTRAINT IF EXISTS kg_nodes_change_origin_chk;
ALTER TABLE kg_nodes ADD CONSTRAINT kg_nodes_change_origin_chk CHECK (change_origin IN
  ('sync','webhook','agent','operator','consolidation','replay','unknown')) NOT VALID;
ALTER TABLE kg_nodes VALIDATE CONSTRAINT kg_nodes_change_origin_chk;
ALTER TABLE kg_nodes ADD COLUMN IF NOT EXISTS origin_event_id UUID;

ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS change_origin TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE kg_edges DROP CONSTRAINT IF EXISTS kg_edges_change_origin_chk;
ALTER TABLE kg_edges ADD CONSTRAINT kg_edges_change_origin_chk CHECK (change_origin IN
  ('sync','webhook','agent','operator','consolidation','replay','unknown')) NOT VALID;
ALTER TABLE kg_edges VALIDATE CONSTRAINT kg_edges_change_origin_chk;
ALTER TABLE kg_edges ADD COLUMN IF NOT EXISTS origin_event_id UUID;

-- A2. Derivation depth on memories
ALTER TABLE memories ADD COLUMN IF NOT EXISTS derivation_depth SMALLINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_memories_ns_derivation_depth ON memories (namespace_id, derivation_depth);

-- A3. DLQ triage columns on dead_letter_queue
ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS error_fingerprint TEXT;
ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS quarantined_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_dlq_fingerprint ON dead_letter_queue (error_fingerprint);

-- A4. processed_outbox_events
CREATE TABLE IF NOT EXISTS processed_outbox_events (
    event_id     UUID PRIMARY KEY,
    namespace_id UUID NOT NULL REFERENCES namespaces(id),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_processed_outbox_events_namespace_id ON processed_outbox_events (namespace_id);

-- A5. actor_trust
CREATE TABLE IF NOT EXISTS actor_trust (
    namespace_id           UUID NOT NULL REFERENCES namespaces(id),
    actor_id               TEXT NOT NULL,
    actor_kind             TEXT NOT NULL CHECK (actor_kind IN ('agent','operator')),
    confirmations          INT NOT NULL DEFAULT 0,
    rejections             INT NOT NULL DEFAULT 0,
    contradictions_sourced INT NOT NULL DEFAULT 0,
    trust                  NUMERIC NOT NULL DEFAULT 0.65,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, actor_id, actor_kind)
);

-- A6. event_parents — APPEND-ONLY
CREATE TABLE IF NOT EXISTS event_parents (
    event_id        UUID NOT NULL,
    parent_event_id UUID NOT NULL,
    namespace_id    UUID NOT NULL REFERENCES namespaces(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, parent_event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_parents_parent_event_id ON event_parents (parent_event_id);
CREATE INDEX IF NOT EXISTS idx_event_parents_namespace_id ON event_parents (namespace_id);

-- Attach WORM trigger on event_parents (reusing prevent_mutation)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_event_parents_worm') THEN
        CREATE TRIGGER trg_event_parents_worm
            BEFORE UPDATE OR DELETE ON event_parents
            FOR EACH ROW EXECUTE FUNCTION prevent_mutation();
    END IF;
END $$;

-- A7. action_approval_queue
CREATE TABLE IF NOT EXISTS action_approval_queue (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id     UUID NOT NULL REFERENCES namespaces(id),
    agent_id         TEXT NOT NULL,
    action_type      TEXT NOT NULL,
    target_system    TEXT NOT NULL,
    target_entity_id TEXT,
    proposed_payload JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','executed','expired')),
    dry_run_result   JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_approval_queue_ns_status ON action_approval_queue (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_action_approval_queue_ns_created ON action_approval_queue (namespace_id, created_at);

-- A8. action_idempotency
CREATE TABLE IF NOT EXISTS action_idempotency (
    idempotency_key  TEXT NOT NULL,
    namespace_id     UUID NOT NULL REFERENCES namespaces(id),
    action_type      TEXT NOT NULL,
    target_entity_id TEXT,
    response_hash    BYTEA,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, idempotency_key)
);

-- RLS & Grants for new tables
DO $$
DECLARE
    t text;
    new_tables text[] := ARRAY[
        'processed_outbox_events',
        'actor_trust',
        'event_parents',
        'action_approval_queue',
        'action_idempotency'
    ];
BEGIN
    FOREACH t IN ARRAY new_tables
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation_policy ON public.%I '
            'FOR ALL TO nce_app '
            'USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace()) '
            'WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())',
            t
        );
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM nce_app', t);
        IF t IN ('event_parents') THEN
            EXECUTE format(
                'GRANT SELECT, INSERT ON TABLE public.%I TO nce_app',
                t
            );
        ELSE
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO nce_app',
                t
            );
        END IF;
    END LOOP;
END $$;
