-- ============================================================================
-- TriMCP — PostgreSQL Schema
-- Loaded by nce.orchestrator.NCEEngine._init_pg_schema on connect().
--
-- All statements are idempotent (IF NOT EXISTS). Safe to run on every startup.
-- Hardening applied: pgcrypto, HNSW cosine indexes, TIMESTAMPTZ, CHECK on
-- confidence, compound index for recall, updated_at on upserted KG tables,
-- CHAR(24) mongo_ref_id (MongoDB ObjectId hex length), NOT NULL where Saga
-- semantics forbid orphans.
-- ============================================================================

-- --- Extensions ---
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- --- Application roles (required before any RLS policy references nce_app) ---
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        CREATE ROLE nce_app WITH LOGIN PASSWORD 'nce_app_secret';
    ELSE
        ALTER ROLE nce_app WITH LOGIN PASSWORD 'nce_app_secret';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_gc') THEN
        CREATE ROLE nce_gc BYPASSRLS NOLOGIN;
    ELSE
        ALTER ROLE nce_gc BYPASSRLS NOLOGIN;
    END IF;
END $$;

-- --- Phase 0.1: Namespaces ---
CREATE TABLE IF NOT EXISTS namespaces (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug       TEXT UNIQUE NOT NULL,
    parent_id  UUID REFERENCES namespaces(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_namespaces_parent_id ON namespaces(parent_id);
CREATE INDEX IF NOT EXISTS idx_namespaces_created_at ON namespaces(created_at DESC);

-- --- Phase 0.2: Cryptographic Signing Keys ---
CREATE TABLE IF NOT EXISTS signing_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id        TEXT UNIQUE NOT NULL,
    encrypted_key BYTEA NOT NULL,
    status        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at    TIMESTAMPTZ
);

-- --- Unified Memories Table (Phase 0.1) ---
-- Replaces memory_metadata and code_metadata. Partitioned by RANGE(created_at).
CREATE TABLE IF NOT EXISTS memories (
    id                  UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id        UUID        REFERENCES namespaces(id) ON DELETE CASCADE,
    agent_id            TEXT        NOT NULL DEFAULT 'default',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    memory_type         TEXT        NOT NULL DEFAULT 'episodic',
    assertion_type      TEXT        NOT NULL DEFAULT 'fact',
    payload_ref         TEXT        NOT NULL,
    -- VI.5c D2: fp16 (halfvec) halves on-disk vector + HNSW index size and read
    -- I/O vs full fp32 storage, with negligible recall loss. fp32 casts to fp16.
    embedding           halfvec(768),
    embedding_model_id  UUID,
    derived_from        JSONB,
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to            TIMESTAMPTZ,
    signature           BYTEA,
    signature_key_id    TEXT,
    pii_redacted        BOOLEAN     NOT NULL DEFAULT false,
    change_origin       TEXT        NOT NULL DEFAULT 'unknown',
    origin_event_id     UUID,
    derivation_depth    SMALLINT    NOT NULL DEFAULT 0,
    
    -- Legacy compatibility fields (from memory_metadata and code_metadata)
    user_id             VARCHAR(128),
    session_id          VARCHAR(128),
    content_fts         TSVECTOR,
    filepath            TEXT,
    language            VARCHAR(64),
    node_type           VARCHAR(64),
    name                VARCHAR(255),
    start_line          INT,
    end_line            INT,
    file_hash           VARCHAR(64),
    
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS memories_default PARTITION OF memories DEFAULT;

ALTER TABLE memories ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Migration 018 (Part II.4 Provable Forgetting): envelope-encryption DEK columns.
-- wrapped_dek holds the AES-256-GCM-wrapped Data Encryption Key (envelope-encrypted
-- under NCE_MASTER_KEY via nce.envelope.wrap_dek); dek_key_id is an opaque, key-free
-- identifier used in deletion receipts/audit events.  Zeroing wrapped_dek crypto-shreds
-- the corresponding episodes.raw_data ciphertext.  Read-path wiring lands in Batch 46.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS wrapped_dek BYTEA;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS dek_key_id TEXT;

-- Data Migration from legacy tables
DO $$
DECLARE
    global_ns_id UUID;
BEGIN
    -- Ensure fallback namespace exists
    INSERT INTO namespaces (slug, metadata)
    VALUES ('_global_legacy', '{"description":"Fallback namespace for pre-RLS data"}'::jsonb)
    ON CONFLICT (slug) DO NOTHING;
    
    SELECT id INTO global_ns_id FROM namespaces WHERE slug = '_global_legacy';

    IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'memory_metadata') THEN
        INSERT INTO memories (
            id, user_id, session_id, embedding, payload_ref, created_at, content_fts, 
            namespace_id, agent_id, signature, signature_key_id, memory_type
        )
        SELECT 
            id, user_id, session_id, embedding, mongo_ref_id, created_at, content_fts,
            global_ns_id, 'default', NULL, NULL, 'episodic'
        FROM memory_metadata
        ON CONFLICT DO NOTHING;
        
        DROP TABLE memory_metadata CASCADE;
    END IF;

    IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'code_metadata') THEN
        INSERT INTO memories (
            id, filepath, language, node_type, name, start_line, end_line, file_hash, 
            embedding, payload_ref, created_at, user_id, content_fts, namespace_id, memory_type
        )
        SELECT 
            id, filepath, language, node_type, name, start_line, end_line, file_hash, 
            embedding, mongo_ref_id, created_at, NULL, content_fts, global_ns_id, 'code_chunk'
        FROM code_metadata
        ON CONFLICT DO NOTHING;
        
        DROP TABLE code_metadata CASCADE;
    END IF;
END $$;

-- memory_type / assertion_type — align with nce.models MemoryType / AssertionType
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'ck_memories_memory_type'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT ck_memories_memory_type
            CHECK (memory_type IN ('episodic', 'consolidated', 'decision', 'code_chunk'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'ck_memories_assertion_type'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT ck_memories_assertion_type
            CHECK (assertion_type IN ('fact', 'opinion', 'preference', 'observation'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'memories_change_origin_chk'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT memories_change_origin_chk
            CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'));
    END IF;
END $$;

-- Indexes for memories
CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING GIN (content_fts);
CREATE INDEX IF NOT EXISTS idx_memories_payload_ref ON memories (payload_ref);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user_session ON memories (user_id, session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_filepath ON memories (filepath);
CREATE INDEX IF NOT EXISTS idx_memories_user_path ON memories (user_id, filepath);
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw ON memories USING hnsw (embedding halfvec_cosine_ops);
-- Fleet admin: COUNT(*) / lookups by tenant without scanning all time partitions
CREATE INDEX IF NOT EXISTS idx_memories_namespace_id ON memories (namespace_id);
CREATE INDEX IF NOT EXISTS idx_memories_ns_derivation_depth ON memories (namespace_id, derivation_depth);

-- payload_ref CHECK constraint — enforce MongoDB ObjectId hex format (24 hex chars)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'ck_payload_ref_objectid_format'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT ck_payload_ref_objectid_format
            CHECK (payload_ref ~ '^[a-f0-9]{24}$');
    END IF;
END $$;

-- --- Knowledge-graph nodes (partitioned by HASH) ---
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = 'kg_nodes' AND c.relkind = 'r' AND c.relispartition = false AND NOT EXISTS (SELECT 1 FROM pg_partitioned_table WHERE partrelid = c.oid)) THEN
        ALTER TABLE kg_nodes RENAME TO kg_nodes_old;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS kg_nodes (
    id            UUID DEFAULT gen_random_uuid(),
    label         TEXT NOT NULL,
    entity_type   VARCHAR(64) NOT NULL DEFAULT 'UNKNOWN',
    -- VI.5c D2: fp16 (halfvec) — see memories.embedding above.
    embedding     halfvec(768),
    embedding_model_id UUID,
    namespace_id  UUID NOT NULL,
    payload_ref   CHAR(24),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    change_origin TEXT NOT NULL DEFAULT 'unknown',
    origin_event_id UUID,
    d365_source_id TEXT,
    procurement_source_id TEXT,
    UNIQUE (label, namespace_id),
    CONSTRAINT kg_nodes_change_origin_chk CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
) PARTITION BY HASH (label);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='kg_nodes' AND column_name='embedding_model_id') THEN
        ALTER TABLE kg_nodes ADD COLUMN embedding_model_id UUID;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='kg_nodes' AND column_name='procurement_source_id') THEN
        ALTER TABLE kg_nodes ADD COLUMN procurement_source_id TEXT;
    END IF;
END $$;

-- Phase 1 hardening: namespace_id + RLS for kg_nodes
DO $$
DECLARE
    global_ns_id UUID;
BEGIN
    -- Ensure a fallback global namespace exists for legacy data
    INSERT INTO namespaces (slug, metadata)
    VALUES ('_global_legacy', '{"description":"Fallback namespace for pre-RLS KG data"}'::jsonb)
    ON CONFLICT (slug) DO NOTHING;
    SELECT id INTO global_ns_id FROM namespaces WHERE slug = '_global_legacy';

    -- Add namespace_id column if missing
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='kg_nodes' AND column_name='namespace_id') THEN
        ALTER TABLE kg_nodes ADD COLUMN namespace_id UUID;
    END IF;

    -- Backfill existing NULL rows
    UPDATE kg_nodes SET namespace_id = global_ns_id WHERE namespace_id IS NULL;

    -- Make NOT NULL now that all rows have a value
    ALTER TABLE kg_nodes ALTER COLUMN namespace_id SET NOT NULL;

    -- Add FK to namespaces
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'kg_nodes_namespace_id_fkey'
    ) THEN
        ALTER TABLE kg_nodes ADD CONSTRAINT kg_nodes_namespace_id_fkey
            FOREIGN KEY (namespace_id) REFERENCES namespaces(id) ON DELETE CASCADE;
    END IF;

    -- Migrate UNIQUE constraint: (label) → (label, namespace_id)
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'kg_nodes' AND constraint_name = 'kg_nodes_label_key'
    ) THEN
        ALTER TABLE kg_nodes DROP CONSTRAINT kg_nodes_label_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'kg_nodes' AND constraint_name = 'kg_nodes_label_namespace_id_key'
    ) THEN
        ALTER TABLE kg_nodes ADD CONSTRAINT kg_nodes_label_namespace_id_key
            UNIQUE (label, namespace_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS kg_nodes_0 PARTITION OF kg_nodes FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS kg_nodes_1 PARTITION OF kg_nodes FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS kg_nodes_2 PARTITION OF kg_nodes FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS kg_nodes_3 PARTITION OF kg_nodes FOR VALUES WITH (MODULUS 4, REMAINDER 3);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'kg_nodes_old') THEN
        INSERT INTO kg_nodes (id, label, entity_type, embedding, payload_ref, created_at, updated_at, namespace_id)
        SELECT id, label, entity_type, embedding, mongo_ref_id, created_at, updated_at, (SELECT id FROM namespaces WHERE slug = '_global_legacy' LIMIT 1)
        FROM kg_nodes_old
        ON CONFLICT (label, namespace_id) DO NOTHING;
        DROP TABLE kg_nodes_old CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_nodes_embedding_hnsw ON kg_nodes USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_updated ON kg_nodes (updated_at);

-- --- Knowledge-graph edges (partitioned by HASH) ---
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = 'kg_edges' AND c.relkind = 'r' AND c.relispartition = false AND NOT EXISTS (SELECT 1 FROM pg_partitioned_table WHERE partrelid = c.oid)) THEN
        ALTER TABLE kg_edges RENAME TO kg_edges_old;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS kg_edges (
    id            UUID DEFAULT gen_random_uuid(),
    subject_label TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    object_label  TEXT NOT NULL,
    confidence    FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    namespace_id  UUID NOT NULL,
    payload_ref   CHAR(24),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    change_origin TEXT NOT NULL DEFAULT 'unknown',
    origin_event_id UUID,
    d365_source_id TEXT,
    procurement_source_id TEXT,
    UNIQUE (subject_label, predicate, object_label, namespace_id),
    CONSTRAINT kg_edges_change_origin_chk CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
) PARTITION BY HASH (subject_label, predicate, object_label);

CREATE TABLE IF NOT EXISTS kg_edges_0 PARTITION OF kg_edges FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS kg_edges_1 PARTITION OF kg_edges FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS kg_edges_2 PARTITION OF kg_edges FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS kg_edges_3 PARTITION OF kg_edges FOR VALUES WITH (MODULUS 4, REMAINDER 3);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'kg_edges_old') THEN
        INSERT INTO kg_edges (id, subject_label, predicate, object_label, confidence, payload_ref, created_at, updated_at, namespace_id)
        SELECT id, subject_label, predicate, object_label, confidence, mongo_ref_id, created_at, updated_at, (SELECT id FROM namespaces WHERE slug = '_global_legacy' LIMIT 1)
        FROM kg_edges_old
        -- FIX-038: 4-column conflict target matches the unique constraint on kg_edges.
        -- Do not revert to 3-column; namespace_id is required for multi-tenant isolation.
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING;
        DROP TABLE kg_edges_old CASCADE;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='kg_edges' AND column_name='procurement_source_id') THEN
        ALTER TABLE kg_edges ADD COLUMN procurement_source_id TEXT;
    END IF;
END $$;

-- Phase 1 hardening: namespace_id + RLS for kg_edges
DO $$
DECLARE
    global_ns_id UUID;
BEGIN
    SELECT id INTO global_ns_id FROM namespaces WHERE slug = '_global_legacy';

    -- Add namespace_id column if missing
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='kg_edges' AND column_name='namespace_id') THEN
        ALTER TABLE kg_edges ADD COLUMN namespace_id UUID;
    END IF;

    -- Backfill existing NULL rows
    UPDATE kg_edges SET namespace_id = global_ns_id WHERE namespace_id IS NULL;

    -- Make NOT NULL
    ALTER TABLE kg_edges ALTER COLUMN namespace_id SET NOT NULL;

    -- Add FK
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'kg_edges_namespace_id_fkey'
    ) THEN
        ALTER TABLE kg_edges ADD CONSTRAINT kg_edges_namespace_id_fkey
            FOREIGN KEY (namespace_id) REFERENCES namespaces(id) ON DELETE CASCADE;
    END IF;

    -- Migrate UNIQUE: (s,p,o) → (s,p,o,namespace_id)
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'kg_edges' AND constraint_name = 'kg_edges_subject_label_predicate_objec_key'
    ) THEN
        ALTER TABLE kg_edges DROP CONSTRAINT kg_edges_subject_label_predicate_objec_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'kg_edges' AND constraint_name = 'kg_edges_subject_label_predicate_object_label_namespace_id_key'
    ) THEN
        ALTER TABLE kg_edges ADD CONSTRAINT kg_edges_subject_label_predicate_object_label_namespace_id_key
            UNIQUE (subject_label, predicate, object_label, namespace_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_edges_subject ON kg_edges (subject_label);
CREATE INDEX IF NOT EXISTS idx_kg_edges_object  ON kg_edges (object_label);
CREATE INDEX IF NOT EXISTS idx_kg_edges_updated ON kg_edges (updated_at);

-- --- Phase 0.3: PII Redactions Vault ---
CREATE TABLE IF NOT EXISTS pii_redactions (
    id              UUID DEFAULT gen_random_uuid(),
    namespace_id    UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    memory_id       UUID NOT NULL,
    token           TEXT NOT NULL,
    encrypted_value BYTEA NOT NULL,
    entity_type     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS pii_redactions_default PARTITION OF pii_redactions DEFAULT;

CREATE INDEX IF NOT EXISTS idx_pii_redactions_memory ON pii_redactions (memory_id);
CREATE INDEX IF NOT EXISTS idx_pii_redactions_token ON pii_redactions (token);

-- FIX-054: namespace-scoped PII queries require this index to avoid full partition scans.
CREATE INDEX IF NOT EXISTS idx_pii_redactions_namespace_id
    ON pii_redactions (namespace_id);

-- --- Phase 1.1: Memory Salience ---
CREATE TABLE IF NOT EXISTS memory_salience (
    memory_id       UUID        NOT NULL,
    agent_id        TEXT        NOT NULL,
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    salience_score  REAL        NOT NULL DEFAULT 1.0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count    INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, agent_id)
) PARTITION BY HASH (memory_id, agent_id);

CREATE TABLE IF NOT EXISTS memory_salience_0 PARTITION OF memory_salience FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS memory_salience_1 PARTITION OF memory_salience FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS memory_salience_2 PARTITION OF memory_salience FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS memory_salience_3 PARTITION OF memory_salience FOR VALUES WITH (MODULUS 4, REMAINDER 3);

-- Fleet admin: salience-map + fleet rollup subqueries scoped by namespace_id
CREATE INDEX IF NOT EXISTS idx_memory_salience_namespace_id ON memory_salience (namespace_id);

-- --- Phase 1.3: Contradictions ---
CREATE TABLE IF NOT EXISTS contradictions (
    id             UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    memory_a_id    UUID        NOT NULL,
    memory_b_id    UUID        NOT NULL,
    agent_id       TEXT        NOT NULL DEFAULT 'system',
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    detection_path TEXT        NOT NULL,
    signals        JSONB       NOT NULL,
    confidence     REAL        NOT NULL,
    resolution     TEXT,
    resolved_at    TIMESTAMPTZ,
    resolved_by    TEXT,
    note           TEXT,
    PRIMARY KEY (id, detected_at)
) PARTITION BY RANGE (detected_at);

CREATE TABLE IF NOT EXISTS contradictions_default PARTITION OF contradictions DEFAULT;

-- Fleet admin: open contradiction counts per namespace
CREATE INDEX IF NOT EXISTS idx_contradictions_namespace_id ON contradictions (namespace_id);

-- --- Phase 2.1: Embedding Models & Migrations ---
CREATE TABLE IF NOT EXISTS embedding_models (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT UNIQUE NOT NULL,
    dimension  INTEGER NOT NULL,
    status     TEXT NOT NULL,   -- active | migrating | retired
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id    UUID NOT NULL,
    model_id     UUID NOT NULL REFERENCES embedding_models(id),
    embedding    vector, -- Unconstrained dimension to support any model
    namespace_id UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, model_id)
) PARTITION BY HASH (memory_id);

CREATE TABLE IF NOT EXISTS memory_embeddings_0 PARTITION OF memory_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS memory_embeddings_1 PARTITION OF memory_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS memory_embeddings_2 PARTITION OF memory_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS memory_embeddings_3 PARTITION OF memory_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 3);

-- Index for validate_migration emb_count query and model-scoped lookups
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model_id ON memory_embeddings(model_id);

CREATE TABLE IF NOT EXISTS embedding_aspects (
    memory_id    UUID NOT NULL,
    aspect       VARCHAR(64) NOT NULL,
    embedding    halfvec(768),
    namespace_id UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, aspect)
) PARTITION BY HASH (memory_id);

CREATE TABLE IF NOT EXISTS embedding_aspects_0 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS embedding_aspects_1 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS embedding_aspects_2 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS embedding_aspects_3 PARTITION OF embedding_aspects FOR VALUES WITH (MODULUS 4, REMAINDER 3);

CREATE INDEX IF NOT EXISTS idx_embedding_aspects_hnsw ON embedding_aspects USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_embedding_aspects_namespace_id ON embedding_aspects (namespace_id);


CREATE TABLE IF NOT EXISTS kg_node_embeddings (
    node_id    UUID NOT NULL,
    model_id   UUID NOT NULL REFERENCES embedding_models(id),
    embedding  vector,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, model_id)
) PARTITION BY HASH (node_id);

CREATE TABLE IF NOT EXISTS kg_node_embeddings_0 PARTITION OF kg_node_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE IF NOT EXISTS kg_node_embeddings_1 PARTITION OF kg_node_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE IF NOT EXISTS kg_node_embeddings_2 PARTITION OF kg_node_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE IF NOT EXISTS kg_node_embeddings_3 PARTITION OF kg_node_embeddings FOR VALUES WITH (MODULUS 4, REMAINDER 3);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_0 TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_1 TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_2 TO nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_3 TO nce_app;
        
        GRANT DELETE ON pii_redactions TO nce_app;
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'pii_redactions_default') THEN
            GRANT DELETE ON pii_redactions_default TO nce_app;
        END IF;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS embedding_migrations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id     UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    target_model_id  UUID NOT NULL REFERENCES embedding_models(id),
    status           TEXT NOT NULL DEFAULT 'running', -- running | validating | committed | aborted
    last_memory_id   UUID,
    last_node_id     UUID,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
);

-- --- Document bridge subscriptions ---
CREATE TABLE IF NOT EXISTS bridge_subscriptions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id    UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    provider        TEXT NOT NULL CHECK (provider IN ('sharepoint', 'gdrive', 'dropbox')),
    resource_id     TEXT NOT NULL,
    subscription_id TEXT,
    cursor          TEXT,
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('REQUESTED','VALIDATING','ACTIVE','DEGRADED','EXPIRED','DISCONNECTED')),
    expires_at      TIMESTAMPTZ,
    client_state    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bridge_subs_user_provider ON bridge_subscriptions (user_id, provider);
CREATE INDEX IF NOT EXISTS idx_bridge_subs_expires_active ON bridge_subscriptions (expires_at) WHERE status = 'ACTIVE';
-- Fleet admin: per-namespace ACTIVE counts / next expiry resolution
CREATE INDEX IF NOT EXISTS idx_bridge_subscriptions_namespace_id ON bridge_subscriptions (namespace_id);

ALTER TABLE bridge_subscriptions ADD COLUMN IF NOT EXISTS oauth_access_token_enc BYTEA;
ALTER TABLE bridge_subscriptions ADD COLUMN IF NOT EXISTS namespace_id UUID REFERENCES namespaces(id) ON DELETE CASCADE;

-- --- Phase 2.2: Time Travel Snapshots ---
CREATE TABLE IF NOT EXISTS snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    snapshot_at  TIMESTAMPTZ NOT NULL,    -- The point in time being snapshotted
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (namespace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ns ON snapshots (namespace_id);

DO $$
BEGIN
    REVOKE ALL ON snapshots FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON snapshots TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — snapshot GRANTs skipped (create role or run migrations)';
    END IF;
END $$;

-- --- Phase 2.3: Event Log (WORM) ---
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id      UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    agent_id          TEXT NOT NULL DEFAULT 'system',
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running',
    clusters_found    INTEGER DEFAULT 0,
    clusters_accepted INTEGER DEFAULT 0,
    clusters_rejected INTEGER DEFAULT 0,
    memories_synth    INTEGER DEFAULT 0,
    llm_provider      TEXT,
    llm_model         TEXT,
    llm_tokens_used   INTEGER DEFAULT 0,
    error             TEXT
);

-- Columns used by nce.consolidation (idempotent add for older DBs)
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS events_processed INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS clusters_formed INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS abstractions_created INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'ck_consolidation_runs_status'
    ) THEN
        ALTER TABLE consolidation_runs ADD CONSTRAINT ck_consolidation_runs_status
            CHECK (status IN ('running', 'completed', 'failed'));
    END IF;
END $$;

-- Fleet admin: latest consolidation_run per namespace
CREATE INDEX IF NOT EXISTS idx_consolidation_runs_namespace_id ON consolidation_runs (namespace_id);

CREATE TABLE IF NOT EXISTS event_log (
    id               UUID DEFAULT gen_random_uuid(),
    namespace_id     UUID NOT NULL REFERENCES namespaces(id),
    agent_id         TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    event_seq        BIGINT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    params           JSONB NOT NULL,
    result_summary   JSONB,
    parent_event_id  UUID,
    llm_payload_uri  TEXT,
    llm_payload_hash BYTEA,
    signature        BYTEA NOT NULL,
    signature_key_id TEXT NOT NULL,
    signature_version SMALLINT NOT NULL DEFAULT 1,
    chain_hash       BYTEA,
    PRIMARY KEY (id, occurred_at),
    UNIQUE (namespace_id, event_seq, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE IF NOT EXISTS event_log_default PARTITION OF event_log DEFAULT;

-- Per-namespace monotonic event_seq counter (single-row UPSERT avoids MAX(event_seq)
-- merge-append scans across event_log partitions on every append).
CREATE TABLE IF NOT EXISTS event_sequences (
    namespace_id UUID PRIMARY KEY REFERENCES namespaces(id) ON DELETE CASCADE,
    seq          BIGINT NOT NULL DEFAULT 0
);

-- --- Phase 2.3: Memory Replay Engine Sessions ---
CREATE TABLE IF NOT EXISTS replay_runs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_namespace_id  UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    target_namespace_id  UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    mode                 TEXT NOT NULL,          -- observational | reconstructive | forked
    replay_mode          TEXT NOT NULL DEFAULT 'deterministic',  -- deterministic | re-execute
    start_seq            BIGINT NOT NULL,
    end_seq              BIGINT,
    divergence_seq       BIGINT,
    config_overrides     JSONB,
    status               TEXT NOT NULL,          -- running | success | failed | aborted
    events_applied       BIGINT NOT NULL DEFAULT 0,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    error                TEXT,
    source_state_digest  TEXT,
    target_state_digest  TEXT,
    digest_match         BOOLEAN
);

DO $$
BEGIN
    REVOKE ALL ON replay_runs FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON replay_runs TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — replay_runs GRANTs skipped';
    END IF;
END $$;

-- Fail-fast session namespace for RLS policies (see nce/auth.set_namespace_context).
CREATE OR REPLACE FUNCTION get_nce_namespace() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    val := nullif(trim(current_setting('nce.namespace_id', true)), '');
    IF val IS NULL THEN
        RAISE EXCEPTION 'nce.namespace_id is not set for this transaction';
    END IF;
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'nce.namespace_id is not a valid UUID: %', val;
    END;
END;
$$ LANGUAGE plpgsql STABLE;

-- C3 external-principal scope accessor (migration 028).
-- Returns the nil UUID (deny-sentinel) when nce.external_scope_id is unset/empty,
-- guaranteeing zero rows are visible — deny-when-unset invariant.
-- See nce/migrations/028_c3_external_scope_rls.sql for full documentation.
-- INVARIANT: no real external_scope_id column may store the nil UUID.
CREATE OR REPLACE FUNCTION get_nce_external_scope() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    val := nullif(trim(current_setting('nce.external_scope_id', true)), '');
    IF val IS NULL THEN
        RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END IF;
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION get_nce_external_scope() IS
'C3 external-principal RLS accessor. Returns session external_scope_id or nil-UUID
deny-sentinel when unset. Use in external_isolation_policy USING expressions.';

ALTER TABLE replay_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE replay_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS namespace_isolation_policy ON replay_runs;
DROP POLICY IF EXISTS tenant_isolation_policy ON replay_runs;
CREATE POLICY tenant_isolation_policy ON replay_runs
    FOR ALL TO nce_app
    USING (
        source_namespace_id IS NOT NULL
        AND source_namespace_id = get_nce_namespace()
    )
    WITH CHECK (
        source_namespace_id IS NOT NULL
        AND source_namespace_id = get_nce_namespace()
    );


CREATE INDEX IF NOT EXISTS idx_event_log_ns_time ON event_log (namespace_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_log_ns_seq  ON event_log (namespace_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_event_log_parent  ON event_log (parent_event_id) WHERE parent_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_log_memory_id ON event_log (((params->>'memory_id')::uuid));
CREATE INDEX IF NOT EXISTS idx_event_log_event_type ON event_log (event_type);
CREATE INDEX IF NOT EXISTS idx_event_log_time_travel ON event_log (namespace_id, occurred_at)
    WHERE event_type IN ('store_memory', 'forget_memory');
CREATE INDEX IF NOT EXISTS idx_event_log_params_gin ON event_log USING GIN (params);

-- Monthly partition windows (UTC); keeps hot data off the DEFAULT catch-all partition
CREATE OR REPLACE FUNCTION nce_ensure_event_log_monthly_partitions(p_months_ahead int DEFAULT 3)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    m int;
    p_start timestamptz;
    p_end timestamptz;
    p_name text;
    violating_count int;
BEGIN
    IF p_months_ahead < 0 THEN
        RAISE EXCEPTION 'p_months_ahead must be >= 0';
    END IF;
    FOR m IN 0..p_months_ahead LOOP
        p_start := date_trunc('month', now() + make_interval(months => m));
        p_end := p_start + interval '1 month';
        p_name := 'event_log_' || to_char(p_start, 'YYYY_MM');
        IF to_regclass(format('public.%I', p_name)) IS NULL THEN
            -- Check if there are violating rows in event_log_default
            EXECUTE format(
                'SELECT count(*)::int FROM event_log_default WHERE occurred_at >= %L AND occurred_at < %L',
                p_start,
                p_end
            ) INTO violating_count;
            
            IF violating_count > 0 THEN
                -- Move violating rows from event_log_default to a temp table
                CREATE TEMP TABLE temp_event_log_migrate ON COMMIT DROP AS 
                    SELECT * FROM event_log_default 
                    WHERE occurred_at >= p_start AND occurred_at < p_end;
                    
                DELETE FROM event_log_default 
                WHERE occurred_at >= p_start AND occurred_at < p_end;
                
                -- Create partition
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF event_log FOR VALUES FROM (%L) TO (%L)',
                    p_name,
                    p_start,
                    p_end
                );
                
                -- Insert them back into event_log so they route to the new partition
                INSERT INTO event_log SELECT * FROM temp_event_log_migrate;
                
                -- Drop the temp table
                DROP TABLE temp_event_log_migrate;
            ELSE
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF event_log FOR VALUES FROM (%L) TO (%L)',
                    p_name,
                    p_start,
                    p_end
                );
            END IF;
        END IF;
    END LOOP;
END;
$fn$;

SELECT nce_ensure_event_log_monthly_partitions(3);

DO $$
BEGIN
    REVOKE ALL ON event_log FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT INSERT, SELECT ON event_log TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — event_log GRANTs skipped (create role or run migrations)';
    END IF;
END $$;

DO $$
BEGIN
    REVOKE ALL ON event_sequences FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT INSERT, SELECT, UPDATE ON event_sequences TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — event_sequences GRANTs skipped (create role or run migrations)';
    END IF;
END $$;

-- FIX-064 / FIX-065: parent_event_id triggers validate or UPDATE without a partition key,
-- causing partition merge-appends; SET NULL path is also incompatible with WORM.
-- Partition-safe policy is deferred (FIX-067); Merkle chain provides integrity.
DROP TRIGGER IF EXISTS trg_event_log_parent_fk ON event_log;
DROP TRIGGER IF EXISTS trg_event_log_parent_fk_insupd ON event_log;
DROP TRIGGER IF EXISTS trg_event_log_parent_fk_del ON event_log;
DROP TRIGGER IF EXISTS trg_event_log_parent_set_null ON event_log;
DROP FUNCTION IF EXISTS trg_event_log_parent_fk();
DROP FUNCTION IF EXISTS trg_event_log_parent_set_null();

-- WORM immutability: reject any UPDATE or DELETE on event_log.
CREATE OR REPLACE FUNCTION prevent_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'event_log is immutable (WORM). % operation is forbidden.', TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Salience decay UDF (Item B): pushes Ebbinghaus decay into PostgreSQL
CREATE OR REPLACE FUNCTION nce_decayed_score(
    s_last FLOAT,
    updated_at TIMESTAMPTZ,
    half_life_days FLOAT
) RETURNS FLOAT AS $$
DECLARE
    delta_t FLOAT;
    decay_constant FLOAT;
    exponent FLOAT;
    MAX_EXP CONSTANT FLOAT := 20.0;
BEGIN
    IF half_life_days <= 0 THEN
        RETURN s_last;
    END IF;
    delta_t := GREATEST(0.0, EXTRACT(EPOCH FROM (NOW() - updated_at)) / 86400.0);
    decay_constant := LN(2) / half_life_days;
    exponent := LEAST(decay_constant * delta_t, MAX_EXP);
    RETURN s_last * EXP(-exponent);
END;
$$ LANGUAGE plpgsql STABLE;

DO $$
BEGIN
    -- Install WORM immutability trigger (legacy parent-FK triggers dropped above).
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_event_log_worm') THEN
        CREATE TRIGGER trg_event_log_worm
            BEFORE UPDATE OR DELETE ON event_log
            FOR EACH ROW EXECUTE FUNCTION prevent_mutation();
    END IF;
END $$;

-- --- Phase 3.1: A2A (Agent-to-Agent) Sharing Grants ---
CREATE TABLE IF NOT EXISTS a2a_grants (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_namespace_id   UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    owner_agent_id       TEXT        NOT NULL,
    target_namespace_id  UUID,                       -- NULL = any bearer is valid
    target_agent_id      TEXT,                       -- NULL = any agent
    scopes               JSONB       NOT NULL,
    token_hash           BYTEA       NOT NULL UNIQUE, -- SHA-256 of sharing token
    status               TEXT        NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active', 'revoked', 'expired')),
    expires_at           TIMESTAMPTZ NOT NULL,
    can_delegate         BOOLEAN     NOT NULL DEFAULT false,
    one_time             BOOLEAN     NOT NULL DEFAULT false,
    usage_count          INTEGER     NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Active-token lookup (most frequent hot path)
CREATE INDEX IF NOT EXISTS idx_a2a_grants_token_active
    ON a2a_grants (token_hash)
    WHERE status = 'active';

-- Owner namespace list-grants query
CREATE INDEX IF NOT EXISTS idx_a2a_grants_owner
    ON a2a_grants (owner_namespace_id, status);

-- Expiry sweep (background janitor)
CREATE INDEX IF NOT EXISTS idx_a2a_grants_expires
    ON a2a_grants (expires_at)
    WHERE status = 'active';

DO $$
BEGIN
    REVOKE ALL ON a2a_grants FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT INSERT, SELECT, UPDATE ON a2a_grants TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — a2a_grants GRANTs skipped (create role or run migrations)';
    END IF;
END $$;

-- Enforce SHA-256 hash length (32 bytes) on token_hash
-- Diagnostic: if existing rows have invalid token_hash length, warn and skip
-- the constraint to prevent a hard crash on dirty legacy data.
DO $$
DECLARE
    invalid_count BIGINT;
BEGIN
    SELECT count(*) INTO invalid_count FROM a2a_grants WHERE length(token_hash) != 32;

    IF invalid_count > 0 THEN
        RAISE WARNING 'ck_a2a_grants_token_hash_len NOT ADDED: % row(s) in a2a_grants have token_hash length != 32. Repair these rows before the constraint can be enforced.', invalid_count;
    ELSE
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.check_constraints
            WHERE constraint_name = 'ck_a2a_grants_token_hash_len'
        ) THEN
            ALTER TABLE a2a_grants ADD CONSTRAINT ck_a2a_grants_token_hash_len
                CHECK (length(token_hash) = 32);
            RAISE NOTICE 'ck_a2a_grants_token_hash_len constraint added successfully.';
        END IF;
    END IF;
END $$;

-- --- Phase 3.2: Multi-namespace resource quotas ---
-- ``used_amount`` is the last flushed value in PostgreSQL. When
-- ``TRIMCP_QUOTA_REDIS_COUNTERS`` is enabled, the hot path increments a Redis
-- mirror (see nce.quotas) and a background task periodically runs
-- ``flush_quota_counters_to_postgres`` to persist counters without serializing
-- writers on this table.
-- Namespace-wide rows use agent_id IS NULL; per-agent rows set agent_id.
-- Enforcement applies only where matching rows exist (no row => no limit for that scope).
CREATE TABLE IF NOT EXISTS resource_quotas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id    UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    agent_id        TEXT,
    resource_type   TEXT NOT NULL,
    limit_amount    BIGINT NOT NULL CHECK (limit_amount >= 0),
    used_amount     BIGINT NOT NULL DEFAULT 0 CHECK (used_amount >= 0),
    reset_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (agent_id IS NULL OR (length(agent_id) >= 1 AND length(agent_id) <= 128)),
    CHECK (resource_type <> ''),
    CONSTRAINT chk_quota CHECK (used_amount <= limit_amount)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_quotas_ns_res
    ON resource_quotas (namespace_id, resource_type)
    WHERE agent_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_quotas_ns_agent_res
    ON resource_quotas (namespace_id, agent_id, resource_type)
    WHERE agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_resource_quotas_ns_type
    ON resource_quotas (namespace_id, resource_type);

DO $$
BEGIN
    REVOKE ALL ON resource_quotas FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON resource_quotas TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — resource_quotas GRANTs skipped (create role or run migrations)';
    END IF;
END $$;

-- --- Phase 3: Dead Letter Queue (Poison Pill) ---
-- Captures background-task payloads that exhaust their retry budget so they
-- are not re-enqueued indefinitely.  Admin UI / API can replay or purge.
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id   UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    task_name      TEXT NOT NULL,          -- e.g. 'process_code_indexing'
    job_id         TEXT NOT NULL,          -- RQ job id
    kwargs         JSONB NOT NULL,         -- frozen kwargs of the failed invocation
    error_message  TEXT NOT NULL,          -- last exception message (truncated to 1024)
    attempt_count  INTEGER NOT NULL CHECK (attempt_count > 0),
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'replayed', 'purged')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed_at    TIMESTAMPTZ,
    purged_at      TIMESTAMPTZ,
    error_fingerprint TEXT,
    quarantined_until TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dlq_task_status ON dead_letter_queue (task_name, status);
CREATE INDEX IF NOT EXISTS idx_dlq_fingerprint ON dead_letter_queue (error_fingerprint);
CREATE INDEX IF NOT EXISTS idx_dlq_created ON dead_letter_queue (created_at DESC);

DO $$
BEGIN
    REVOKE ALL ON dead_letter_queue FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE ON dead_letter_queue TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — dead_letter_queue GRANTs skipped';
    END IF;
END $$;

-- --- Phase 4: Transactional Outbox ---
-- Ordered, at-most-once delivery of domain events.
-- The relay process polls unpublished rows, delivers to downstream
-- consumers, and marks published_at.
CREATE TABLE IF NOT EXISTS outbox_events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id   UUID NOT NULL
                   REFERENCES namespaces(id) ON DELETE CASCADE,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    payload        JSONB NOT NULL,
    headers        JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    error_message  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON outbox_events (created_at)
    WHERE published_at IS NULL;

-- 59 of the 62 cascading tables carry a leading namespace_id index; without
-- one, every namespace DELETE sequentially scans this table for its cascade
-- targets. See migration 062.
CREATE INDEX IF NOT EXISTS idx_outbox_events_namespace_id
    ON outbox_events (namespace_id);

ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS namespace_isolation_policy ON outbox_events;
DROP POLICY IF EXISTS tenant_isolation_policy ON outbox_events;
CREATE POLICY tenant_isolation_policy ON outbox_events
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    REVOKE ALL ON outbox_events FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — outbox_events GRANTs skipped';
    END IF;
END $$;

-- --- Phase 3: Active Learning Queue ---
CREATE TABLE IF NOT EXISTS active_learning_queue (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id     UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL DEFAULT 'default',
    payload          JSONB NOT NULL,
    confidence_score REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_active_learning_queue_ns_status
    ON active_learning_queue (namespace_id, status);

-- --- Phase 4: Saga Execution Log ---
-- Durable saga state for crash-recovery.  If a worker dies between PG commit
-- and rollback completion, the recovery cron re-drives compensation from the
-- persisted payload.
CREATE TABLE IF NOT EXISTS saga_execution_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    saga_type    TEXT NOT NULL,           -- 'store_memory', 'forget_memory', etc.
    namespace_id UUID NOT NULL
                 REFERENCES namespaces(id) ON DELETE CASCADE,
    agent_id     TEXT NOT NULL,
    state        TEXT NOT NULL
                 CHECK (state IN ('started', 'pg_committed', 'completed', 'rolled_back', 'recovery_needed')),
    payload      JSONB NOT NULL,          -- enough to re-drive rollback
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_saga_state_created
    ON saga_execution_log (state, created_at)
    WHERE state IN ('started', 'pg_committed', 'recovery_needed');

-- Cascade-target lookup for namespace deletion. See migration 062.
CREATE INDEX IF NOT EXISTS idx_saga_execution_log_namespace_id
    ON saga_execution_log (namespace_id);

ALTER TABLE saga_execution_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE saga_execution_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS namespace_isolation_policy ON saga_execution_log;
DROP POLICY IF EXISTS tenant_isolation_policy ON saga_execution_log;
CREATE POLICY tenant_isolation_policy ON saga_execution_log
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    REVOKE ALL ON saga_execution_log FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE ON saga_execution_log TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — saga_execution_log GRANTs skipped';
    END IF;
END $$;



-- --- Dynamics 365 / Dataverse vertical module ---
CREATE TABLE IF NOT EXISTS d365_integrations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id        UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    org_url             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'DEGRADED', 'DISABLED')),
    token_enc           BYTEA,           -- AES-256-GCM encrypted access token JSON
    token_expires_at    TIMESTAMPTZ,
    webhook_secret_enc  BYTEA,           -- AES-256-GCM encrypted webhook secret
    last_sync_at        TIMESTAMPTZ,
    last_sync_stats     JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (namespace_id, org_url)
);

CREATE INDEX IF NOT EXISTS idx_d365_integrations_namespace
    ON d365_integrations (namespace_id);
CREATE INDEX IF NOT EXISTS idx_d365_integrations_status
    ON d365_integrations (status)
    WHERE status = 'ACTIVE';

-- D365 ↔ NetBox cross-reference mapping table.
-- Stores confirmed and inferred mappings between Dataverse entities
-- (Accounts, Functional Locations) and NetBox entities (Tenants, Sites, Locations).
-- Rows are upserted by the bridge cron tick and surfaced as kg_edges.
CREATE TABLE IF NOT EXISTS d365_netbox_mappings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id        UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    d365_entity_type    TEXT NOT NULL
                        CHECK (d365_entity_type IN ('account', 'functional_location')),
    d365_entity_id      TEXT NOT NULL,          -- Dataverse GUID string
    d365_entity_name    TEXT NOT NULL,
    nb_entity_type      TEXT NOT NULL
                        CHECK (nb_entity_type IN ('tenant', 'site', 'location')),
    nb_entity_id        INTEGER NOT NULL,       -- NetBox integer PK
    nb_entity_name      TEXT NOT NULL,
    nb_entity_slug      TEXT,
    -- How was this match made?
    match_method        TEXT NOT NULL
                        CHECK (match_method IN ('custom_field', 'exact', 'slug', 'fuzzy', 'manual')),
    match_confidence    FLOAT NOT NULL DEFAULT 1.0
                        CHECK (match_confidence BETWEEN 0.0 AND 1.0),
    -- Operator confirmation (false = inferred, true = human-confirmed)
    confirmed           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (namespace_id, d365_entity_type, d365_entity_id, nb_entity_type, nb_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_d365_netbox_mappings_namespace
    ON d365_netbox_mappings (namespace_id);
CREATE INDEX IF NOT EXISTS idx_d365_netbox_mappings_d365_type
    ON d365_netbox_mappings (namespace_id, d365_entity_type);
CREATE INDEX IF NOT EXISTS idx_d365_netbox_mappings_confirmed
    ON d365_netbox_mappings (namespace_id, confirmed)
    WHERE confirmed = TRUE;

-- D365 sync-run audit trail (per-entity). Tenant-scoped (RLS applied by the
-- tenant_tables loop below); surfaced by the d365_sync_status MCP tool. Migration 023.
CREATE TABLE IF NOT EXISTS d365_sync_runs (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    run_id        UUID        NOT NULL,
    entity        TEXT        NOT NULL,
    upserted      INTEGER     NOT NULL DEFAULT 0,
    incremental   BOOLEAN     NOT NULL DEFAULT FALSE,
    status        TEXT        NOT NULL DEFAULT 'ok'
                              CHECK (status IN ('ok', 'error')),
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_d365_sync_runs_namespace_time
    ON d365_sync_runs(namespace_id, started_at DESC);

-- D365 change-tracking deltaLink store (per namespace+entity). Tenant-scoped
-- (RLS via the tenant_tables loop below). See migration 024.
CREATE TABLE IF NOT EXISTS d365_delta_tokens (
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    entity        TEXT        NOT NULL,
    delta_link    TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, entity)
);

-- C5 divergence audit log (migration 030).
-- Append-only per-engine divergence records with materiality + FORCE RLS.
-- Underpins the flip-gate: both→nce is blocked while the log is dirty over the window.
CREATE TABLE IF NOT EXISTS divergence_log (
    id           UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine       TEXT        NOT NULL,
    entity       TEXT        NOT NULL,
    field        TEXT        NOT NULL,
    nce_value    TEXT,
    ext_value    TEXT,
    materiality  NUMERIC     NOT NULL,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_divergence_log_namespace_engine_detected
    ON divergence_log (namespace_id, engine, detected_at DESC);

-- Contract-A registry: per (namespace, node_type, transition), the sole-writer engine.
-- Consulted by the write-path to enforce single-writer invariant. Tenant-scoped (RLS).
-- See migration 026.
CREATE TABLE IF NOT EXISTS node_ownership_registry (
    id                    UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_type             TEXT        NOT NULL,
    transition            TEXT,
    owner_engine          TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_node_ownership_registry_namespace_type_transition
    ON node_ownership_registry(namespace_id, node_type, transition);

-- migration 032: unique index + non-empty owner_engine CHECK (idempotent).
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_ownership_registry_ns_type_transition
    ON node_ownership_registry (namespace_id, node_type, COALESCE(transition, ''));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'ck_node_ownership_owner_engine_nonempty'
          AND  conrelid = 'node_ownership_registry'::regclass
    ) THEN
        ALTER TABLE node_ownership_registry
            ADD CONSTRAINT ck_node_ownership_owner_engine_nonempty
            CHECK (owner_engine <> '');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS entity_merge_queue (
    id                    UUID            NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID            NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_type             TEXT            NOT NULL,
    candidate_payload     JSONB           NOT NULL,
    target_node_id        UUID,
    score                 DOUBLE PRECISION NOT NULL,
    status                TEXT            NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    decided_by            TEXT,
    decided_at            TIMESTAMPTZ,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_entity_merge_queue_namespace_status
    ON entity_merge_queue(namespace_id, status);

CREATE INDEX IF NOT EXISTS idx_entity_merge_queue_created_at
    ON entity_merge_queue(namespace_id, created_at DESC);

ALTER TABLE entity_merge_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_merge_queue FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON entity_merge_queue;
CREATE POLICY tenant_isolation_policy ON entity_merge_queue
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE entity_merge_queue FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE entity_merge_queue TO nce_app;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS source_mode_config (
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine       TEXT        NOT NULL,
    function     TEXT        NOT NULL,
    mode         TEXT        NOT NULL CHECK (mode IN ('d365', 'both', 'nce')),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, engine, function)
);

CREATE INDEX IF NOT EXISTS idx_source_mode_config_namespace_engine
    ON source_mode_config(namespace_id, engine);

-- --- Product Catalog (Migration 031) ---
CREATE TABLE IF NOT EXISTS product_catalog (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    gtin              TEXT,
    manufacturer      TEXT        NOT NULL,
    mfr_part_no       TEXT        NOT NULL,
    product_source_id TEXT        NOT NULL,
    lifecycle_status  TEXT        NOT NULL DEFAULT 'active',
    is_deleted        BOOLEAN     NOT NULL DEFAULT false,
    etim_specs        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (manufacturer, mfr_part_no)
);

CREATE TABLE IF NOT EXISTS product_prices (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    mfr_part_no   TEXT        NOT NULL,
    supplier      TEXT        NOT NULL,
    bid_id        TEXT        NOT NULL,
    list_price    NUMERIC,
    cost_price    NUMERIC,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, mfr_part_no, supplier, bid_id)
);

-- product_catalog is a GLOBAL shared parts library (Sindre's ruling, 2026-09-04)
-- -- see nce/migrations/064_product_catalog_global.sql. This block mirrors that
-- migration's end state for databases created before it, and EVERY statement is
-- idempotent because _init_pg_schema re-executes this file on every connect().
DROP POLICY IF EXISTS tenant_isolation_policy ON product_catalog;
DROP POLICY IF EXISTS namespace_isolation_policy ON product_catalog;
ALTER TABLE product_catalog NO FORCE ROW LEVEL SECURITY;
ALTER TABLE product_catalog DISABLE ROW LEVEL SECURITY;
ALTER TABLE product_catalog
    DROP CONSTRAINT IF EXISTS product_catalog_namespace_id_manufacturer_mfr_part_no_key;
DROP INDEX IF EXISTS idx_product_catalog_namespace_mfr_mfr_part_no;
DROP INDEX IF EXISTS idx_product_catalog_namespace_gtin;
DROP INDEX IF EXISTS idx_product_catalog_namespace_is_deleted;
ALTER TABLE product_catalog DROP COLUMN IF EXISTS namespace_id;

-- Indexes for product_catalog: identity is (manufacturer, mfr_part_no) --
-- one row per real part, shared by every tenant.
CREATE UNIQUE INDEX IF NOT EXISTS product_catalog_manufacturer_mfr_part_no_key
    ON product_catalog (manufacturer, mfr_part_no);

CREATE INDEX IF NOT EXISTS idx_product_catalog_gtin
    ON product_catalog (gtin)
    WHERE gtin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_catalog_is_deleted
    ON product_catalog (is_deleted);

-- product_catalog left the tenant_tables loop below, which was its only GRANT
-- site, so nce_app's privileges are granted explicitly here.
DO $BODY$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE product_catalog TO nce_app;
    END IF;
END $BODY$;

-- Indexes for product_prices
CREATE INDEX IF NOT EXISTS idx_product_prices_namespace_mfr_part_no
    ON product_prices (namespace_id, mfr_part_no);

CREATE INDEX IF NOT EXISTS idx_product_prices_namespace_supplier
    ON product_prices (namespace_id, supplier);

COMMENT ON TABLE product_catalog IS
'ETIM-coded product catalog: 552k-row streaming-upsert master for multi-source ingestion.
Deduped on (manufacturer, mfr_part_no); GTIN is the universal key (nullable).
etim_specs JSONB holds coded (etim_class, feature, value, unit) tuples with per-field
provenance and confidence inside the JSONB. product_source_id tracks per-source provenance
for multi-source dedup. is_deleted enables soft-delete. FORCE RLS isolates per tenant.';

COMMENT ON TABLE product_prices IS
'Product pricing: 1.57M cost/list/BID rows (mfr_part_no, supplier, bid_id) natural key.
Streaming-upsert target for price syncs. FORCE RLS isolates per tenant.';

-- Append-only learning table for BOM-line match decisions (migration 033).
CREATE TABLE IF NOT EXISTS product_match_feedback (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line      TEXT        NOT NULL,
    chosen_sku    TEXT,
    rejected_sku  TEXT,
    decision      TEXT        NOT NULL CHECK (decision IN ('accept', 'override')),
    matched_score NUMERIC,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_product_match_feedback_namespace_created
    ON product_match_feedback (namespace_id, created_at DESC);

ALTER TABLE product_match_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_match_feedback FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON product_match_feedback;
CREATE POLICY tenant_isolation_policy ON product_match_feedback
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE product_match_feedback FROM nce_app;
        GRANT SELECT, INSERT ON TABLE product_match_feedback TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE product_match_feedback IS
'Append-only learning table for BOM-line match decisions.
Records accept/override decisions from the product_match_bom_line tool so
the C1 resolve() primitive can be recalibrated over time.  Never UPDATE or
DELETE rows — event-sourced learning loop.  FORCE RLS isolates per tenant.';

-- Review-queue backing store for on-demand product enrichment proposals (migration 034).
CREATE TABLE IF NOT EXISTS product_enrichment_log (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    product_id        UUID        NOT NULL,
    trigger_context   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    field_name        TEXT        NOT NULL,
    field_value       TEXT,
    confidence        NUMERIC(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    needs_review      BOOLEAN     NOT NULL DEFAULT true,
    product_source_id TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_product_enrichment_log_namespace_product
    ON product_enrichment_log (namespace_id, product_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_product_enrichment_log_needs_review
    ON product_enrichment_log (namespace_id, needs_review, created_at DESC)
    WHERE needs_review = true;

ALTER TABLE product_enrichment_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_enrichment_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON product_enrichment_log;
CREATE POLICY tenant_isolation_policy ON product_enrichment_log
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE product_enrichment_log FROM nce_app;
        GRANT SELECT, INSERT ON TABLE product_enrichment_log TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE product_enrichment_log IS
'Append-only review-queue backing store for on-demand product enrichment proposals.
Each row is one field proposal: field_name + field_value + verbalized confidence (A4) +
needs_review flag.  Money/legal fields (§9.3) and sub-threshold proposals are always
written with needs_review=True.  High-confidence non-money/legal fields may additionally
be merged into product_catalog.etim_specs (the JSONB designed for per-field provenance).
Never UPDATE or DELETE rows — WORM review log.  FORCE RLS isolates per tenant.';

-- Procurement consumer cache for Product's BID/supplier-price projections (migration 035).
-- Fed via upsert_bid_projection() from Product's A2A projection push.
-- do_resolve_bids() reads this cache for best-BID-per-artnr resolution.
CREATE TABLE IF NOT EXISTS procurement_bid_prices (
    id           UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    artnr        TEXT        NOT NULL,
    leverandor   TEXT        NOT NULL,
    bid_id       TEXT        NOT NULL,
    prodid       TEXT,
    pris         NUMERIC,
    valid_to     TIMESTAMPTZ,
    raw          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, artnr, leverandor, bid_id)
);

CREATE INDEX IF NOT EXISTS idx_procurement_bid_prices_namespace_artnr
    ON procurement_bid_prices (namespace_id, artnr);

CREATE INDEX IF NOT EXISTS idx_procurement_bid_prices_namespace_leverandor
    ON procurement_bid_prices (namespace_id, leverandor);

ALTER TABLE procurement_bid_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE procurement_bid_prices FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON procurement_bid_prices;
CREATE POLICY tenant_isolation_policy ON procurement_bid_prices
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE procurement_bid_prices FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procurement_bid_prices TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE procurement_bid_prices IS
'Procurement consumer cache for Product''s BID/supplier-price projections.
Fed via upsert_bid_projection() from Product''s A2A projection push — not a
primary Nettailer ingest (§9.1: Product owns the single feed).  Natural key is
(namespace_id, artnr, leverandor, bid_id); ON CONFLICT DO UPDATE keeps cache
current.  do_resolve_bids() reads this cache for best-BID-per-artnr resolution.
FORCE RLS isolates per tenant.';

-- --- System Design Phase-2: device capability attributes (migration 038) ---
-- Typed/queryable AVIXA Revit Parameter attributes for DEVICE/PORT nodes.
-- kg_nodes has no payload column; this table holds the capability fields keyed
-- by (namespace_id, node_label).  FORCE RLS isolates per tenant.
CREATE TABLE IF NOT EXISTS system_design_device_capabilities (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_label      TEXT        NOT NULL,
    signal_format   TEXT,
    signal_version  TEXT,
    port_direction  TEXT
        CHECK (port_direction IS NULL OR port_direction IN ('input', 'output', 'bidirectional')),
    poe_class       SMALLINT,
    poe_watts       NUMERIC,
    dante_rx_channels  SMALLINT,
    dante_tx_channels  SMALLINT,
    power_draw_watts   NUMERIC,
    heat_btu_hr        NUMERIC,
    redundancy_role TEXT
        CHECK (redundancy_role IS NULL OR redundancy_role IN ('primary', 'secondary', 'standalone')),
    device_category TEXT,
    manufacturer    TEXT,
    model_number    TEXT,
    extra           JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label)
);

CREATE INDEX IF NOT EXISTS idx_sddc_namespace_node_label
    ON system_design_device_capabilities (namespace_id, node_label);

CREATE INDEX IF NOT EXISTS idx_sddc_namespace_signal_format
    ON system_design_device_capabilities (namespace_id, signal_format)
    WHERE signal_format IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sddc_namespace_redundancy_role
    ON system_design_device_capabilities (namespace_id, redundancy_role)
    WHERE redundancy_role IS NOT NULL;

ALTER TABLE system_design_device_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_device_capabilities FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON system_design_device_capabilities;
CREATE POLICY tenant_isolation_policy ON system_design_device_capabilities
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE system_design_device_capabilities FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_design_device_capabilities TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE system_design_device_capabilities IS
'Phase-2 device capability attributes for the System Design engine.
Keyed by (namespace_id, node_label) — node_label matches kg_nodes.label.
Column schema follows the AVIXA AV Device Revit Parameter List (Phase 2.1).
kg_nodes has no payload column; typed/queryable capability fields live here.
FORCE RLS isolates per tenant (mirrors procurement_bid_prices pattern).';

-- --- Sales read model & targets ---
CREATE TABLE IF NOT EXISTS sales_read_model (
    id             BIGSERIAL   PRIMARY KEY,
    namespace_id   UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    entity         TEXT        NOT NULL,
    source_id      TEXT        NOT NULL,
    name           TEXT,
    modifiedon     TIMESTAMPTZ,
    source_json    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    manual         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    source         TEXT        NOT NULL DEFAULT 'direct',
    is_deleted     BOOLEAN     NOT NULL DEFAULT false,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sales_read_model_natural_key UNIQUE (namespace_id, entity, source_id)
);

CREATE INDEX IF NOT EXISTS idx_sales_read_model_entity ON sales_read_model (namespace_id, entity);
CREATE INDEX IF NOT EXISTS idx_sales_read_model_modified ON sales_read_model (namespace_id, entity, modifiedon DESC);
CREATE INDEX IF NOT EXISTS idx_sales_read_model_name ON sales_read_model (namespace_id, entity, lower(name));
CREATE INDEX IF NOT EXISTS idx_sales_read_model_deleted ON sales_read_model (namespace_id, entity, is_deleted);
CREATE INDEX IF NOT EXISTS idx_sales_read_model_opp_customer ON sales_read_model (namespace_id, (source_json->>'_customerid_value')) WHERE entity='opportunities';
CREATE INDEX IF NOT EXISTS idx_sales_read_model_contact_parent ON sales_read_model (namespace_id, (source_json->>'_parentcustomerid_value')) WHERE entity='contacts';
CREATE INDEX IF NOT EXISTS idx_sales_read_model_asset_account ON sales_read_model (namespace_id, (source_json->>'_msdyn_account_value')) WHERE entity='customerassets';
CREATE INDEX IF NOT EXISTS idx_sales_read_model_owner ON sales_read_model (namespace_id, entity, (source_json->>'_ownerid_value')) WHERE is_deleted=false;

CREATE TABLE IF NOT EXISTS sales_targets (
    namespace_id UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    owner_slug   TEXT        NOT NULL,
    metric       TEXT        NOT NULL,
    value        NUMERIC,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, owner_slug, metric)
);

CREATE TABLE IF NOT EXISTS sales_signed_baselines (
    id                 BIGSERIAL   PRIMARY KEY,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    quote_id           TEXT        NOT NULL,
    signed_margin_pct  NUMERIC     NOT NULL, -- signed gross-margin percentage (0–1)
    signed_total_nok   NUMERIC     NOT NULL, -- total signed value in NOK
    signed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sales_signed_baselines_natural_key UNIQUE (namespace_id, quote_id)
);

CREATE INDEX IF NOT EXISTS idx_sales_signed_baselines_quote ON sales_signed_baselines (namespace_id, quote_id);

CREATE TABLE IF NOT EXISTS vendor_scorecards (
    vendor_id         TEXT        NOT NULL,
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    on_time_pct       NUMERIC,
    defect_rma_rate   NUMERIC,
    substitution_rate NUMERIC,
    reliability       NUMERIC,
    current_tier      TEXT,
    ytd_progress      NUMERIC,
    sample_n          INTEGER     NOT NULL DEFAULT 0,
    raw               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vendor_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_vendor_scorecards_namespace ON vendor_scorecards (namespace_id);

CREATE TABLE IF NOT EXISTS contractor_profiles (
    contractor_id      TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id   UUID        NOT NULL,
    profile            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    rates              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    skills             TEXT[]      NOT NULL DEFAULT '{}'::text[],
    availability       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    performance_score  NUMERIC,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (contractor_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_contractor_profiles_namespace ON contractor_profiles (namespace_id);
CREATE INDEX IF NOT EXISTS idx_contractor_profiles_partner_scope ON contractor_profiles (partner_scope_id);

CREATE TABLE IF NOT EXISTS agreement_review_queue (
    agreement_id           UUID        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    source_doc_ref         TEXT        NOT NULL,
    extraction_confidence  NUMERIC     NOT NULL,
    review_status          TEXT        NOT NULL DEFAULT 'needs_review_yellow'
                                       CHECK (review_status IN ('auto_green', 'needs_review_yellow', 'manual_red')),
    extracted              JSONB       NOT NULL,
    flagged_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by            TEXT,
    reviewed_at            TIMESTAMPTZ,
    PRIMARY KEY (agreement_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_agreement_review_queue_namespace ON agreement_review_queue (namespace_id);

CREATE TABLE IF NOT EXISTS agreement_extraction_runs (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    run_id                 UUID        NOT NULL,
    source_doc_ref         TEXT        NOT NULL,
    extraction_confidence  NUMERIC,
    status                 TEXT        NOT NULL DEFAULT 'ok'
                                       CHECK (status IN ('ok', 'error')),
    error                  TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_agreement_extraction_runs_namespace_time
    ON agreement_extraction_runs (namespace_id, started_at DESC);

-- Economy engine (Module 8) Wave 5: BOM_LINE.actual_cost (migration 047).
-- kg_nodes has no payload column; do_cascade_on_approval is the SOLE writer
-- of this table (roadmap §9.1 "5-writer BOM_LINE" worked example) — mirrors
-- the procurement_bid_prices / system_design_device_capabilities pattern.
-- Round 2: one row per (line, approval) -- see migration 047's header
-- comment for the round-1 overwrite bug this fixes.
CREATE TABLE IF NOT EXISTS economy_bom_actual_costs (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line_label     TEXT        NOT NULL,
    actual_cost        NUMERIC(18,2) NOT NULL,
    source_approval_id TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

ALTER TABLE economy_bom_actual_costs ALTER COLUMN source_approval_id SET NOT NULL;
ALTER TABLE economy_bom_actual_costs ALTER COLUMN actual_cost TYPE NUMERIC(18,2);

ALTER TABLE economy_bom_actual_costs DROP CONSTRAINT IF EXISTS economy_bom_actual_costs_natural_key;
ALTER TABLE economy_bom_actual_costs
    ADD CONSTRAINT economy_bom_actual_costs_natural_key
    UNIQUE (namespace_id, bom_line_label, source_approval_id);

CREATE INDEX IF NOT EXISTS idx_economy_bom_actual_costs_namespace_label
    ON economy_bom_actual_costs (namespace_id, bom_line_label);

ALTER TABLE economy_bom_actual_costs ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_bom_actual_costs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON economy_bom_actual_costs;
CREATE POLICY tenant_isolation_policy ON economy_bom_actual_costs
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE economy_bom_actual_costs FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE economy_bom_actual_costs TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE economy_bom_actual_costs IS
'Economy-owned actual-cost-per-BOM-line table (Module 8, Wave 5, round 2).
do_cascade_on_approval (nce/vertical_modules/economy/cascade.py) is the SOLE
writer -- the clean decomposition of the BOM_LINE "5-writer race" (roadmap
§9.1): content is Sales-frozen, status transitions belong to
Procurement/Warehouse/Field Tech, and actual_cost belongs to the Economy
cascade alone. Natural-keyed (namespace_id, bom_line_label,
source_approval_id) -- ONE ROW PER (line, approval); INSERT ... ON CONFLICT
DO NOTHING, so a replay of the same approval is a no-op by construction and a
DIFFERENT approval against the same line is a new row rather than an
overwrite (round 1''s `DO UPDATE SET actual_cost = EXCLUDED.actual_cost`
silently lost an earlier approval''s cost -- see migration 047''s header
comment). The line''s actual cost is SUM(actual_cost) grouped by
(namespace_id, bom_line_label), computed by the cascade -- never stored as a
single scalar. A negative actual_cost row is a legitimate credit note.
FORCE RLS isolates per tenant (mirrors procurement_bid_prices pattern).';

-- Economy engine (Module 8) Wave 6: the balanced-ledger table behind the
-- POSTING kg_node (migration 048). One row per posting LINE within a
-- balanced financial event; event_id is the event's own content hash, shared
-- with the POSTING:{event_id} kg_node label. `amount` is a single signed
-- column (direction = sign), never a debit/credit column pair.
CREATE TABLE IF NOT EXISTS economy_postings (
    id                 UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    event_id           TEXT          NOT NULL,
    event_type         TEXT          NOT NULL,
    line_no            INTEGER       NOT NULL,
    account            TEXT          NOT NULL,
    amount             NUMERIC(18,2) NOT NULL,
    period_id          TEXT,
    economy_source_id  TEXT,
    change_origin      TEXT          NOT NULL DEFAULT 'agent'
                                      CHECK (change_origin IN
                                          ('sync','webhook','agent','operator','consolidation','replay','unknown')),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

ALTER TABLE economy_postings DROP CONSTRAINT IF EXISTS economy_postings_natural_key;
ALTER TABLE economy_postings
    ADD CONSTRAINT economy_postings_natural_key
    UNIQUE (namespace_id, event_id, line_no);

-- Non-empty `account` CHECK (storage-level backstop, round-3 fix): an
-- account-less posting can be arithmetically perfect (sums to zero) and
-- still be financially meaningless -- graph.py's persist_financial_event
-- already refuses this at the Python level, but Batch 118's lesson is that
-- balancing to zero is necessary, never sufficient, so the storage layer
-- must not depend solely on the application to enforce it. Mirrors
-- migration 033's `owner_engine <> ''` precedent; TRIM() also catches
-- whitespace-only accounts, which a bare `<> ''` would miss. Idempotent via
-- the pg_constraint existence guard (safe to re-run).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'ck_economy_postings_account_nonempty'
          AND  conrelid = 'economy_postings'::regclass
    ) THEN
        ALTER TABLE economy_postings
            ADD CONSTRAINT ck_economy_postings_account_nonempty
            CHECK (TRIM(account) <> '');
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_economy_postings_namespace_event
    ON economy_postings (namespace_id, event_id);

ALTER TABLE economy_postings ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_postings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON economy_postings;
CREATE POLICY tenant_isolation_policy ON economy_postings
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE economy_postings FROM nce_app;
        -- Append-only ledger (WORM), round-3 fix: withhold UPDATE/DELETE
        -- from nce_app at the grant level, following this repo's own
        -- precedent -- event_log grants nce_app only INSERT, SELECT
        -- (schema.sql), deliberately withholding UPDATE/DELETE. No shipped
        -- code path issues UPDATE/DELETE against economy_postings (graph.py's
        -- persist_financial_event is the sole writer and is INSERT-only),
        -- but nce_app is the application's general role, so any future bug,
        -- admin raw-SQL tool, or correction script would otherwise hit an
        -- unguarded table. Corrections must instead go through compensating
        -- reversal postings -- standard ledger practice, now enforced
        -- structurally rather than left to convention. Idempotent: REVOKE
        -- ALL first means a re-run always converges on exactly SELECT,
        -- INSERT regardless of what an earlier version of this migration
        -- granted.
        GRANT SELECT, INSERT ON TABLE economy_postings TO nce_app;
    END IF;
END $$;

-- Storage-level sum=0 backstop (migration 048) -- AFTER INSERT STATEMENT-level
-- trigger using a transition table so a multi-row insert for one event is
-- checked once against the full stored set for that (namespace_id, event_id).
-- Tolerance matches the application-level epsilon (0.01 NOK), not bit-exact
-- zero -- see migration 048's header comment.
CREATE OR REPLACE FUNCTION economy_postings_assert_balanced() RETURNS TRIGGER AS $BODY$
DECLARE
    bad RECORD;
BEGIN
    FOR bad IN
        SELECT ep.namespace_id AS ns, ep.event_id AS eid, SUM(ep.amount) AS total
        FROM economy_postings ep
        JOIN (SELECT DISTINCT namespace_id, event_id FROM new_postings) np
          ON np.namespace_id = ep.namespace_id AND np.event_id = ep.event_id
        GROUP BY ep.namespace_id, ep.event_id
        HAVING ABS(SUM(ep.amount)) > 0.01
    LOOP
        RAISE EXCEPTION
            'economy_postings: event % (namespace %) does not balance to zero (sum=%, tolerance=+/-0.01)',
            bad.eid, bad.ns, bad.total;
    END LOOP;
    RETURN NULL;
END;
$BODY$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_economy_postings_assert_balanced ON economy_postings;
CREATE TRIGGER trg_economy_postings_assert_balanced
    AFTER INSERT ON economy_postings
    REFERENCING NEW TABLE AS new_postings
    FOR EACH STATEMENT
    EXECUTE FUNCTION economy_postings_assert_balanced();

COMMENT ON TABLE economy_postings IS
'Economy-owned balanced-ledger table (Module 8, Wave 6). One row per posting
LINE within a balanced financial event (do_emit_financial_event validates the
whole event balances to zero within epsilon BEFORE any write reaches here --
see nce/vertical_modules/economy/events.py). event_id is the event''s own
deterministic content hash, shared with the corresponding POSTING kg_node
label (POSTING:{event_id}). Natural-keyed (namespace_id, event_id, line_no) --
ONE ROW PER (event, line); INSERT ... ON CONFLICT DO NOTHING, so a replay of
the identical event is a no-op by construction, never a silent overwrite
(mirrors economy_bom_actual_costs, migration 047). `amount` is signed --
direction follows the sign, never a separate debit/credit column pair.
trg_economy_postings_assert_balanced is a STORAGE-level backstop (not a
replacement for the application-level guard in events.py): it re-checks
SUM(amount)=0 within +/-0.01 NOK per (namespace_id, event_id) after every
INSERT, using a transition table so a multi-row insert for one event is
checked once against the full stored set for that event. FORCE RLS isolates
per tenant (mirrors procurement_bid_prices / economy_bom_actual_costs).
Round-3 fixes: nce_app is granted only SELECT, INSERT (append-only/WORM,
mirrors event_log) -- corrections must be compensating reversal postings,
never an UPDATE/DELETE; and ck_economy_postings_account_nonempty rejects an
empty or whitespace-only `account` at the DB level, independently of
graph.py''s own guard (the same non-empty-after-TRIM CHECK pattern as
migration 033''s owner_engine constraint).';



-- Provenance for D365-derived graph rows (source Dataverse GUID) — enables exact
-- retirement of a deleted record's edges/nodes. See migration 024.
CREATE INDEX IF NOT EXISTS idx_kg_edges_d365_source
    ON kg_edges (namespace_id, d365_source_id)
    WHERE d365_source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_d365_source
    ON kg_nodes (namespace_id, d365_source_id)
    WHERE d365_source_id IS NOT NULL;

-- Provenance for Procurement-derived graph rows (per-vertical source id) — enables
-- exact retirement of a procurement-sourced record. See migration 036.
CREATE INDEX IF NOT EXISTS idx_kg_edges_procurement_source
    ON kg_edges (namespace_id, procurement_source_id)
    WHERE procurement_source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_nodes_procurement_source
    ON kg_nodes (namespace_id, procurement_source_id)
    WHERE procurement_source_id IS NOT NULL;

-- Provenance for System-Design-derived graph rows (per-vertical source id) — enables
-- exact retirement of a system-design-sourced record. See migration 037.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'system_design_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN system_design_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'system_design_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN system_design_source_id TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_nodes_system_design_source
    ON kg_nodes (namespace_id, system_design_source_id)
    WHERE system_design_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_system_design_source
    ON kg_edges (namespace_id, system_design_source_id)
    WHERE system_design_source_id IS NOT NULL;

-- Provenance for Vendors-derived graph rows (per-vertical source id) — enables
-- exact retirement of a vendors-sourced record. See migration 042.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'vendors_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN vendors_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'vendors_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN vendors_source_id TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_nodes_vendors_source
    ON kg_nodes (namespace_id, vendors_source_id)
    WHERE vendors_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_vendors_source
    ON kg_edges (namespace_id, vendors_source_id)
    WHERE vendors_source_id IS NOT NULL;

-- Provenance for Agreements-derived graph rows (per-vertical source id) — enables
-- exact retirement of an agreements-sourced record. See migration 046.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'agreements_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN agreements_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'agreements_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN agreements_source_id TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_nodes_agreements_source
    ON kg_nodes (namespace_id, agreements_source_id)
    WHERE agreements_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_agreements_source
    ON kg_edges (namespace_id, agreements_source_id)
    WHERE agreements_source_id IS NOT NULL;

-- Provenance for Economy-derived graph rows (per-vertical source id) —
-- enables exact retirement of an economy-sourced record. See migration 048.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'economy_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN economy_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'economy_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN economy_source_id TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_nodes_economy_source
    ON kg_nodes (namespace_id, economy_source_id)
    WHERE economy_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_economy_source
    ON kg_edges (namespace_id, economy_source_id)
    WHERE economy_source_id IS NOT NULL;

-- --- Phase 5: DB-backed runtime settings (V.1a) ---
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       JSONB,
    secret_enc  BYTEA,
    is_secret   BOOLEAN NOT NULL DEFAULT false,
    section     TEXT,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    REVOKE ALL ON settings FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON settings TO nce_app;
    ELSE
        RAISE NOTICE 'nce_app role not found — settings GRANTs skipped';
    END IF;
END $$;

-- --- Muscles Schema Contract (Batch C0) ---
CREATE TABLE IF NOT EXISTS processed_outbox_events (
    event_id     UUID PRIMARY KEY,
    namespace_id UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_processed_outbox_events_namespace_id ON processed_outbox_events (namespace_id);

CREATE TABLE IF NOT EXISTS actor_trust (
    namespace_id           UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    actor_id               TEXT NOT NULL,
    actor_kind             TEXT NOT NULL CHECK (actor_kind IN ('agent','operator')),
    confirmations          INT NOT NULL DEFAULT 0,
    rejections             INT NOT NULL DEFAULT 0,
    contradictions_sourced INT NOT NULL DEFAULT 0,
    trust                  NUMERIC NOT NULL DEFAULT 0.65,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, actor_id, actor_kind)
);

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

CREATE TABLE IF NOT EXISTS action_approval_queue (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id     UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS action_idempotency (
    idempotency_key  TEXT NOT NULL,
    namespace_id     UUID NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    action_type      TEXT NOT NULL,
    target_entity_id TEXT,
    response_hash    BYTEA,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, idempotency_key)
);

-- =============================================================================
-- Diagnostics tables (migration 025 — Batch 67 diag-schema)
-- =============================================================================

-- diag_ingestions: per-upload/API/ticketing ingestion record
CREATE TABLE IF NOT EXISTS diag_ingestions (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    ingest_id          TEXT        NOT NULL,
    source             TEXT        CHECK (source IN ('upload', 'api', 'ticketing')),
    vendor_profile     TEXT,
    device_slug        TEXT,
    landing_uri        TEXT,
    status             TEXT        NOT NULL DEFAULT 'PENDING'
                                   CHECK (status IN ('PENDING', 'PROCESSING', 'DIGESTED', 'FAILED')),
    bytes              BIGINT,
    processed_lines    BIGINT,
    anomaly_count      INT,
    digest_payload_ref TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (id),
    UNIQUE (namespace_id, ingest_id)
);

CREATE INDEX IF NOT EXISTS idx_diag_ingestions_namespace_id
    ON diag_ingestions (namespace_id);

-- diag_anomalies: anomalies extracted from a single ingestion
-- sample MUST be truncated to ≤200 chars by the writer (no PII)
CREATE TABLE IF NOT EXISTS diag_anomalies (
    id             UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    ingestion_id   UUID        NOT NULL REFERENCES diag_ingestions(id) ON DELETE CASCADE,
    device_slug    TEXT,
    anomaly_type   TEXT,
    severity       INT,
    first_line     BIGINT,
    occurrences    INT,
    sample         TEXT,
    window_start   TIMESTAMPTZ,
    window_end     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_diag_anomalies_namespace_id
    ON diag_anomalies (namespace_id);

CREATE INDEX IF NOT EXISTS idx_diag_anomalies_ingestion_id
    ON diag_anomalies (ingestion_id);

-- device_health_rollup: latest health aggregate per (namespace_id, device_slug)
CREATE TABLE IF NOT EXISTS device_health_rollup (
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    device_slug       TEXT        NOT NULL,
    health_state      TEXT        CHECK (health_state IN ('HEALTHY', 'DEGRADED', 'CRITICAL')),
    top_anomaly_type  TEXT,
    anomaly_score     FLOAT8,
    last_ingestion_id UUID,
    last_seen_at      TIMESTAMPTZ,
    PRIMARY KEY (namespace_id, device_slug)
);

CREATE INDEX IF NOT EXISTS idx_device_health_rollup_namespace_id
    ON device_health_rollup (namespace_id);

-- topology_graph: knowledge-graph topology edges. Defined here (local form) so schema.sql
-- is self-contained on a FRESH install -- the uq_topology_edge index and the FORCE ROW
-- LEVEL SECURITY below both reference it, and migration 010 (which distributes it under
-- Citus, or its non-Citus local fallback in orchestrator._apply_pg_migrations) runs only
-- AFTER schema.sql at boot. CREATE TABLE IF NOT EXISTS keeps this idempotent and harmless
-- when migration 010 later (re)asserts the table.
CREATE TABLE IF NOT EXISTS topology_graph (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id      UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    source_node_id    TEXT        NOT NULL,
    source_node_type  TEXT        NOT NULL,
    target_node_id    TEXT        NOT NULL,
    target_node_type  TEXT        NOT NULL,
    edge_type         TEXT        NOT NULL,
    decay_coefficient FLOAT8      NOT NULL DEFAULT 0.001,
    confidence_score  FLOAT8      NOT NULL DEFAULT 0.9,
    last_verified     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, namespace_id)
);
ALTER TABLE topology_graph ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS topology_graph_tenant_isolation ON topology_graph;
CREATE POLICY topology_graph_tenant_isolation ON topology_graph
    FOR ALL
    USING (namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_gc;

-- topology_graph: close duplicate-edge gap (see migration 025 for dedup guard note)
CREATE UNIQUE INDEX IF NOT EXISTS uq_topology_edge
    ON topology_graph (namespace_id, source_node_id, target_node_id, edge_type);

-- --- Row Level Security (Phase 0.1 Hardening) ---
-- Applied after all tenant tables exist. Policies use get_nce_namespace() (fail-fast).
-- kg_node_embeddings remain global (no namespace_id). kg_nodes/kg_edges are tenant-scoped.

-- Backfill nullable namespace_id on tables that gained the column after first deploy.
DO $$
DECLARE
    legacy_ns UUID;
BEGIN
    SELECT id INTO legacy_ns FROM namespaces WHERE slug = '_global_legacy' LIMIT 1;
    IF legacy_ns IS NOT NULL THEN
        UPDATE bridge_subscriptions SET namespace_id = legacy_ns WHERE namespace_id IS NULL;
        UPDATE dead_letter_queue SET namespace_id = legacy_ns WHERE namespace_id IS NULL;
        UPDATE embedding_migrations SET namespace_id = legacy_ns WHERE namespace_id IS NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_dead_letter_queue_namespace_id
    ON dead_letter_queue (namespace_id);
CREATE INDEX IF NOT EXISTS idx_embedding_migrations_namespace_id
    ON embedding_migrations (namespace_id);

-- FIX-055: kg_node_embeddings are global (not namespace-scoped).
ALTER TABLE kg_node_embeddings DISABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    t text;
    tenant_tables text[] := ARRAY[
        'memories',
        'kg_nodes',
        'kg_edges',
        'pii_redactions',
        'memory_salience',
        'contradictions',
        'snapshots',
        'event_log',
        'resource_quotas',
        'consolidation_runs',
        'bridge_subscriptions',
        'dead_letter_queue',
        'embedding_migrations',
        'memory_embeddings',
        'embedding_aspects',
        'active_learning_queue',
        'd365_integrations',
        'd365_netbox_mappings',
        'd365_sync_runs',
        'd365_delta_tokens',
        'node_ownership_registry',
        'entity_merge_queue',
        'source_mode_config',
        'processed_outbox_events',
        'actor_trust',
        'event_parents',
        'action_approval_queue',
        'action_idempotency',
        -- diagnostics tables (migration 025 — Batch 67)
        'diag_ingestions',
        'diag_anomalies',
        'device_health_rollup',
        -- vertical-engine tables (ml/foundation)
        'divergence_log',
        'product_prices',
        'procurement_bid_prices',
        'system_design_device_capabilities',
        'sales_read_model',
        'sales_targets',
        'sales_signed_baselines',
        'vendor_scorecards',
        'agreement_review_queue',
        'agreement_extraction_runs'
    ];
BEGIN
    FOREACH t IN ARRAY tenant_tables
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS namespace_isolation_policy ON public.%I', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation_policy ON public.%I '
            'FOR ALL TO nce_app '
            'USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace()) '
            'WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())',
            t
        );
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM nce_app', t);
        IF t IN ('event_log', 'event_parents', 'divergence_log', 'sales_signed_baselines') THEN
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
        -- Pre-BIGSERIAL databases created this table with a UUID key, so the
        -- sequence may not exist; grant only when it does.
        IF t = 'sales_signed_baselines'
           AND EXISTS (SELECT 1 FROM pg_class WHERE relname = 'sales_signed_baselines_id_seq') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.sales_signed_baselines_id_seq TO nce_app';
        END IF;
    END LOOP;
END $$;


ALTER TABLE a2a_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE a2a_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS namespace_isolation_policy ON a2a_grants;
DROP POLICY IF EXISTS tenant_isolation_policy ON a2a_grants;
CREATE POLICY tenant_isolation_policy ON a2a_grants
    FOR ALL TO nce_app
    USING (
        owner_namespace_id = get_nce_namespace()
        OR target_namespace_id = get_nce_namespace()
    )
    WITH CHECK (owner_namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE a2a_grants TO nce_app;

ALTER TABLE contractor_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE contractor_profiles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS external_isolation_policy ON contractor_profiles;
CREATE POLICY external_isolation_policy ON contractor_profiles
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND partner_scope_id IS NOT NULL
        AND partner_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND partner_scope_id IS NOT NULL
        AND partner_scope_id = get_nce_external_scope()
    );
REVOKE ALL ON TABLE contractor_profiles FROM nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE contractor_profiles TO nce_app;


-- =============================================================================
-- Security fix — Domain 5 / D1 HIGH (migration 025 — Batch 67 diag-schema)
-- topology_graph FORCE ROW LEVEL SECURITY
-- =============================================================================
-- topology_graph has had ENABLE ROW LEVEL SECURITY since migration 010, but
-- was never FORCEd.  The table-owner / nce_gc role (BYPASSRLS-adjacent) could
-- therefore bypass the topology_graph_tenant_isolation policy when the session
-- GUC nce.namespace_id is unset — exposing every tenant's infrastructure graph.
--
-- This ALTER is idempotent (safe to re-run).  Kept standalone (not folded into
-- the tenant_tables loop) because topology_graph carries policy name
-- topology_graph_tenant_isolation (from migration 010) whereas the loop creates
-- tenant_isolation_policy; folding would silently rename that policy.
ALTER TABLE topology_graph FORCE ROW LEVEL SECURITY;

-- =============================================================================
-- Economy engine (Module 8) Wave 10: recurring-revenue contract store,
-- CPI-cap validator + 90-day renewal scan (migration 049).
-- =============================================================================

CREATE TABLE IF NOT EXISTS economy_contracts (
    id                 UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    contract_id        TEXT          NOT NULL,
    status             TEXT          NOT NULL
                                      CHECK (status IN ('active', 'churned')),
    -- NOK, 2-decimal (oere) precision -- mirrors economy_bom_actual_costs.actual_cost
    -- (migration 047) / economy_postings.amount (migration 048). The
    -- contract's annual value; do_recognize_recurring (recurring.py, Wave 9)
    -- ratably recognises 1/12 of this per period.
    annual_amount      NUMERIC(18,2) NOT NULL CHECK (annual_amount > 0),
    -- 'YYYY-MM' -- first recognised month, passed straight through to
    -- do_compute_recognition_schedule (recurring.py). Format validated at the
    -- Python boundary (contracts.py's _parse_period), not here -- mirrors
    -- economy_postings.period_id's own bare-TEXT precedent.
    start_period       TEXT          NOT NULL,
    -- CPI uplift ceiling for this contract's renewal quote, as a fraction
    -- (0.05 = 5%). The CHECK is the wave's "CPI cap is a money ceiling"
    -- requirement enforced STRUCTURALLY: no row -- not even one written by a
    -- future bug or a raw-SQL admin fix -- can ever carry a cap above 5%,
    -- independent of whatever the application layer (do_validate_contract)
    -- separately enforces per proposed uplift.
    cpi_cap            NUMERIC(5,4)  NOT NULL DEFAULT 0.05
                                      CHECK (cpi_cap >= 0 AND cpi_cap <= 0.05),
    -- Date the renewal-engine's 90-day scan (do_scan_renewals) compares
    -- against "today". Required: a contract with no renewal date can never
    -- be meaningfully scanned, so this table refuses to represent that
    -- ambiguous state rather than silently excluding the row from every scan.
    next_renewal_date  DATE          NOT NULL,
    raw                JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Natural key: ONE ROW PER (namespace, contract). Unlike economy_postings /
-- economy_bom_actual_costs (append-only ledger lines, ON CONFLICT DO
-- NOTHING), a contract is a live, mutable record -- status changes to
-- 'churned', a renewal moves next_renewal_date forward, an amendment changes
-- annual_amount -- so this table's sole writer (contracts.py's
-- do_upsert_contract) uses ON CONFLICT DO UPDATE against this key.
ALTER TABLE economy_contracts DROP CONSTRAINT IF EXISTS economy_contracts_natural_key;
ALTER TABLE economy_contracts
    ADD CONSTRAINT economy_contracts_natural_key
    UNIQUE (namespace_id, contract_id);

-- Supports the recognition tick's per-namespace read (WHERE namespace_id=...)
-- and both engines' status filtering (WHERE status = 'active').
CREATE INDEX IF NOT EXISTS idx_economy_contracts_namespace_status
    ON economy_contracts (namespace_id, status);

-- Supports the renewal scan's per-namespace active-contracts read, ordered
-- for the "which renews soonest" query shape.
CREATE INDEX IF NOT EXISTS idx_economy_contracts_renewal_date
    ON economy_contracts (namespace_id, next_renewal_date)
    WHERE status = 'active';

ALTER TABLE economy_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_contracts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON economy_contracts;
CREATE POLICY tenant_isolation_policy ON economy_contracts
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE economy_contracts FROM nce_app;
        -- Live mutable record (not a WORM ledger) -- nce_app gets the full
        -- CRUD set, mirroring economy_bom_actual_costs (migration 047), not
        -- economy_postings' append-only grant (migration 048).
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE economy_contracts TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE economy_contracts IS
'Economy-owned recurring-revenue contract store (Module 8, Wave 10). One row
per (namespace, contract) -- a LIVE mutable record (status/next_renewal_date/
annual_amount change over the contract''s life), unlike the append-only
economy_postings / economy_bom_actual_costs ledgers. Natural-keyed
(namespace_id, contract_id); contracts.py''s do_upsert_contract is the SOLE
writer, using ON CONFLICT DO UPDATE. annual_amount is the single source of
truth for the contract''s value (do_recognize_recurring ratably recognises
1/12 of it per period, Wave 9) -- no separate mrr column, to avoid two money
fields drifting apart. cpi_cap is bounded 0-0.05 by CHECK -- a structural
ceiling no row can exceed, backing do_validate_contract''s per-proposal
enforcement. next_renewal_date drives do_scan_renewals''s 90-day scan.
Retires the Wave-9 namespaces.metadata->economy->recurring_contracts shim
(see recurring.py + cron.py). FORCE RLS isolates per tenant (mirrors
economy_postings / economy_bom_actual_costs).';

CREATE TABLE IF NOT EXISTS stock_locations (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- warehouse/van are flat top-level (parent_id IS NULL, level = 0); zone/bin
    -- are the hierarchical children a warehouse can have. Enforced structurally
    -- below (stock_locations_hierarchy_shape), not just by convention -- a van
    -- can never gain a parent, and a zone/bin can never float parentless.
    kind          TEXT        NOT NULL
                              CHECK (kind IN ('warehouse', 'van', 'zone', 'bin')),
    name          TEXT        NOT NULL,
    parent_id     UUID,
    level         INT         NOT NULL DEFAULT 0 CHECK (level >= 0),
    -- Only meaningful for kind='van' -- the VEHICLE+STOCK_LOCATION shared-node
    -- link (roadmap §4 graph contract). NULL for warehouse/zone/bin; nothing
    -- enforces that narrower rule here since the link is populated by a later
    -- wave's field-tech wiring, not this one.
    vehicle_ref   TEXT,
    raw           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite-unique target so the self-referencing parent_id FK below can
    -- also pin namespace_id -- a hierarchy edge can never cross a tenant
    -- boundary (same-namespace-hierarchy invariant, structurally enforced).
    CONSTRAINT stock_locations_id_ns_uq UNIQUE (id, namespace_id),
    CONSTRAINT stock_locations_parent_fk
        FOREIGN KEY (parent_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id)
        ON DELETE CASCADE,
    CONSTRAINT stock_locations_hierarchy_shape CHECK (
        (kind IN ('warehouse', 'van') AND parent_id IS NULL AND level = 0)
        OR
        (kind IN ('zone', 'bin') AND parent_id IS NOT NULL AND level > 0)
    )
);

-- Idempotent-seed arbiter: one row per (namespace, kind, name) among the
-- parentless (top-level) rows -- schema_seed.py's ON CONFLICT target for the
-- warehouse + van seed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_locations_top_level_name
    ON stock_locations (namespace_id, kind, name)
    WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_stock_locations_namespace_kind
    ON stock_locations (namespace_id, kind);

CREATE INDEX IF NOT EXISTS idx_stock_locations_parent
    ON stock_locations (namespace_id, parent_id);

ALTER TABLE stock_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_locations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON stock_locations;
CREATE POLICY tenant_isolation_policy ON stock_locations
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE stock_locations FROM nce_app;
        -- Live mutable record (not a WORM ledger) -- nce_app gets the full
        -- CRUD set, mirroring economy_contracts (migration 049).
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE stock_locations TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE stock_locations IS
'Inventory-owned internal LOGISTICS location tree (Module 11, Wave 1). NOT a
customer FUNCTIONAL_LOCATION (system_design/D365''s customer-site tree) --
two trees, not one; see the file header. Hierarchical: warehouse -> zone ->
bin (parent_id chains, level increments); a van is a flat top-level location
(parent_id IS NULL, level = 0), just like a warehouse -- both shapes are
enforced by stock_locations_hierarchy_shape, not left to caller discipline.
schema_seed.py''s seed_warehouse_and_vans is the reference writer: one
warehouse + N vans per namespace, idempotent via the partial unique index
uq_stock_locations_top_level_name. FORCE RLS isolates per tenant; the
self-referencing parent_id FK is composite on (parent_id, namespace_id) so a
hierarchy edge can never cross a tenant boundary.';


CREATE TABLE IF NOT EXISTS inventory_items (
    id            UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sku           TEXT          NOT NULL,
    location_id   UUID          NOT NULL,
    -- NUMERIC (not INT/FLOAT) -- some SKUs are sold/stocked by fractional unit
    -- (e.g. cable by the metre); Decimal-safe end-to-end, same discipline as
    -- economy_contracts.annual_amount's money precision.
    qty_on_hand   NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (qty_on_hand >= 0),
    qty_reserved  NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (qty_reserved >= 0),
    qty_blocked   NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (qty_blocked >= 0),
    reorder_point NUMERIC(18,3) NOT NULL DEFAULT 0 CHECK (reorder_point >= 0),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite FK on (location_id, namespace_id) -- mirrors
    -- stock_locations_parent_fk's reasoning: an inventory_items row can never
    -- point at a stock_locations row in a different tenant's namespace.
    CONSTRAINT inventory_items_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id)
        ON DELETE CASCADE
);

-- One row per (namespace, sku, location) -- the hot read/atomic-decrement
-- path (docs' "Authority model": this row is authoritative; a future graph
-- INVENTORY_ITEM node is only an eventually-consistent projection of it).
ALTER TABLE inventory_items DROP CONSTRAINT IF EXISTS inventory_items_natural_key;
ALTER TABLE inventory_items
    ADD CONSTRAINT inventory_items_natural_key
    UNIQUE (namespace_id, sku, location_id);

CREATE INDEX IF NOT EXISTS idx_inventory_items_namespace_sku
    ON inventory_items (namespace_id, sku);

ALTER TABLE inventory_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_items FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON inventory_items;
CREATE POLICY tenant_isolation_policy ON inventory_items
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE inventory_items FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE inventory_items TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE inventory_items IS
'Per-SKU-per-location stock row (Module 11, Wave 1) -- the hot
read/atomic-decrement path. One row per (namespace, sku, location)
(inventory_items_natural_key). available = qty_on_hand - qty_reserved -
qty_blocked is computed at read time by a later wave''s do_stock_levels, not
stored here. This row is the SOURCE OF TRUTH for stock-truth reads
(Procurement''s "own stock first", forecast, reservation) -- a future
INVENTORY_ITEM graph node is only an eventually-consistent projection of it,
never the other way around. FORCE RLS isolates per tenant; location_id is a
composite FK on (location_id, namespace_id) into stock_locations so a row can
never reference another tenant''s location.';

CREATE TABLE IF NOT EXISTS inventory_transactions (
    id               UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id     UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sku              TEXT          NOT NULL,
    location_id      UUID          NOT NULL,
    -- Same NUMERIC(18,3) scale as inventory_items.qty_on_hand (migration
    -- 050) -- signed: positive = stock entering this location, negative =
    -- stock leaving it. Never zero -- a zero-quantity "movement" recorded
    -- nothing and must not be appended.
    delta            NUMERIC(18,3) NOT NULL CHECK (delta <> 0),
    -- Typed, not free text (Rackbeat's movements-vs-adjustments split;
    -- docs/vertical_engines/11-inventory-engine.md A5). Widened by migration
    -- 052 (Batch 132 goods-receipt) to add 'goods_receipt' via an idempotent
    -- ALTER ... DROP/ADD CONSTRAINT against an already-existing table --
    -- never widened by dropping the CHECK outright. This CHECK is a NAMED
    -- table-level constraint (not an inline/anonymous column CHECK) so a
    -- fresh install (this file alone) and a migrated install (this file +
    -- migration 052's DROP/ADD CONSTRAINT) produce the SAME catalog identity
    -- -- inventory_transactions_reason_category -- not just the same body.
    -- An anonymous column CHECK here would let Postgres auto-name it
    -- inventory_transactions_reason_category_check on a from-scratch boot,
    -- silently diverging from the migrated path's explicit name even though
    -- the enforced expression is byte-identical either way.
    reason_category  TEXT          NOT NULL,
    -- Money, NOT quantity -- mirrors economy_postings.amount / economy_bom_
    -- actual_costs.actual_cost's NUMERIC(18,2) precision (migrations 047/048).
    -- NULL is the honest default for the transfer/consumption rows this wave's
    -- writers append (see header) -- only a caller that actually knows a cost
    -- (a future goods-receipt writer, or a seeded test row simulating one)
    -- supplies one.
    unit_cost        NUMERIC(18,2),
    -- Free-form caller reference (e.g. do_record_consumption's work_order, or
    -- a transfer's counterpart location id) -- optional, never interpreted by
    -- this table.
    ref              TEXT,
    change_origin    TEXT          NOT NULL DEFAULT 'agent'
                                    CHECK (change_origin IN
                                        ('sync','webhook','agent','operator','consolidation','replay','unknown')),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite FK target is stock_locations_id_ns_uq (migration 050) -- a
    -- ledger row can never reference another tenant's location. No ON DELETE
    -- CASCADE: unlike inventory_items (a live mutable balance), this is a WORM
    -- audit trail -- a location with recorded history must not be able to
    -- take its ledger down with it.
    CONSTRAINT inventory_transactions_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id),
    -- Named explicitly (see the reason_category column comment above) so
    -- this constraint's catalog name matches migration 052's post-ALTER
    -- name on BOTH a fresh install and a migrated one.
    CONSTRAINT inventory_transactions_reason_category CHECK (reason_category IN
        ('transfer_in', 'transfer_out', 'consumption', 'adjustment', 'goods_receipt')),
    -- Sign must agree with the category (storage-level backstop, mirrors
    -- economy_postings' non-empty-account CHECK reasoning -- migration 048):
    -- a 'transfer_out' row with a positive delta would be silently wrong and
    -- an application-level bug must not be able to write it. 'adjustment' is
    -- deliberately unconstrained in sign -- a manual correction can go
    -- either way.
    CONSTRAINT inventory_transactions_sign_matches_category CHECK (
        (reason_category = 'transfer_in' AND delta > 0)
        OR (reason_category IN ('transfer_out', 'consumption') AND delta < 0)
        OR (reason_category = 'adjustment')
        OR (reason_category = 'goods_receipt' AND delta > 0)
    )
);

-- FIFO/average valuation's read pattern: every row for one
-- (namespace, sku, location), oldest first.
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_namespace_sku_location
    ON inventory_transactions (namespace_id, sku, location_id, created_at);

ALTER TABLE inventory_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_transactions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON inventory_transactions;
CREATE POLICY tenant_isolation_policy ON inventory_transactions
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE inventory_transactions FROM nce_app;
        -- Append-only ledger (WORM), mirrors event_log / economy_postings /
        -- audit_log's precedent: withhold UPDATE/DELETE from nce_app at the
        -- grant level so no application code path -- buggy or future -- can
        -- rewrite history. A correction is a NEW row, never an edit.
        GRANT SELECT, INSERT ON TABLE inventory_transactions TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE inventory_transactions IS
'Append-only movement ledger (Module 11, Wave 11 -- transactions-valuation).
One row per qty change at one (sku, location): do_transfer_stock
(nce/vertical_modules/inventory/stock.py) writes a transfer_out row at
from_location and a transfer_in row at to_location in the SAME transaction as
the inventory_items row write it reflects; do_record_consumption writes one
consumption row. unit_cost rides on this row (inventory_items itself has no
cost column) and enters at inbound -- NULL until a real cost source exists
(Batch 132 goods-receipt); do_valuation
(nce/vertical_modules/inventory/transactions.py) computes FIFO/average value
from these rows per nce/config_data/inventory-valuation.json, and is the
number Inventory hands to Economy to post -- this table and its reader never
post to the GL themselves. FORCE RLS isolates per tenant; nce_app is granted
only SELECT, INSERT -- corrections are new rows, never an UPDATE/DELETE.';

CREATE TABLE IF NOT EXISTS inventory_rma (
    id                    UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- Caller-supplied natural key -- the idempotency handle for do_record_rma
    -- (re-recording the same rma_ref returns the existing row, unchanged) and
    -- Batch 138b's stable handle for the two stock legs it performs later.
    rma_ref               TEXT          NOT NULL,
    sku                   TEXT          NOT NULL,
    -- Serialised units; a non-serialised return has none.
    serial                TEXT,
    -- Where the returned stock physically is. Batch 138b restocks TO this
    -- location on the restock leg and disposes FROM it on the WEEE-disposal
    -- leg. Provisioned here so Batch 138b needs no DDL of its own.
    location_id           UUID          NOT NULL,
    -- Same NUMERIC(18,3) scale as inventory_items.qty_on_hand /
    -- inventory_transactions.delta (migrations 050/051). Provisioned here for
    -- the same reason as location_id above.
    qty                   NUMERIC(18,3) NOT NULL CHECK (qty > 0),
    -- Free-form return reason; never interpreted by this table.
    reason                TEXT          NOT NULL,
    -- The WEEE compliance lifecycle. do_record_rma only ever writes
    -- 'not_applicable' or a caller-supplied value from this set at INSERT
    -- time -- there is no UPDATE path in this module (see rma.py's module
    -- docstring); a future wave owns the state's own transitions.
    weee_state            TEXT          NOT NULL DEFAULT 'not_applicable'
                                         CHECK (weee_state IN
                                             ('not_applicable', 'pending', 'awaiting_collection', 'disposed')),
    -- The approved take-back scheme's documentation reference. Required the
    -- moment weee_state = 'disposed' -- see the CHECK constraint below.
    disposal_ref          TEXT,
    -- The stock-leg lifecycle, independent of weee_state. This wave writes
    -- ONLY 'pending' here -- Batch 138b performs both transitions
    -- (restocked / disposed).
    stock_movement_state  TEXT          NOT NULL DEFAULT 'pending'
                                         CHECK (stock_movement_state IN ('pending', 'restocked', 'disposed')),
    change_origin         TEXT          NOT NULL DEFAULT 'agent'
                                         CHECK (change_origin IN
                                             ('sync','webhook','agent','operator','consolidation','replay','unknown')),
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- The natural key -- precedent: economy_contracts' (ns, contract_id),
    -- migration 049.
    CONSTRAINT inventory_rma_ns_ref_uq UNIQUE (namespace_id, rma_ref),
    -- Composite, so an RMA can never point at another tenant's location.
    -- Target index is stock_locations_id_ns_uq (migration 050). No
    -- ON DELETE CASCADE: an RMA is compliance evidence and must not be taken
    -- down by a location deletion (same reasoning as
    -- inventory_transactions_location_fk).
    CONSTRAINT inventory_rma_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id),
    -- The compliance claim of this wave: a WEEE item cannot be recorded as
    -- disposed without the take-back scheme's documentation reference.
    -- Storage-level, so no application bug -- present or future -- can record
    -- an undocumented disposal.
    CONSTRAINT inventory_rma_disposed_requires_ref
        CHECK (weee_state <> 'disposed' OR disposal_ref IS NOT NULL)
);

-- Read pattern: every RMA row for one (namespace, sku), newest first.
CREATE INDEX IF NOT EXISTS idx_inventory_rma_namespace_sku
    ON inventory_rma (namespace_id, sku, created_at);

-- Batch 138b's worklist read: every RMA row still awaiting its stock leg.
CREATE INDEX IF NOT EXISTS idx_inventory_rma_pending
    ON inventory_rma (namespace_id)
    WHERE stock_movement_state = 'pending';

ALTER TABLE inventory_rma ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_rma FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON inventory_rma;
CREATE POLICY tenant_isolation_policy ON inventory_rma
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE inventory_rma FROM nce_app;
        -- UPDATE is granted because Batch 138b transitions the two state
        -- columns (weee_state, stock_movement_state) on this same row. No
        -- DELETE: an RMA row is compliance evidence and nothing in the
        -- application may erase a WEEE disposal record -- a correction is a
        -- new row, never an erased one.
        GRANT SELECT, INSERT, UPDATE ON TABLE inventory_rma TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE inventory_rma IS
'Returns/RMA + WEEE disposal state (Module 11, Wave 10 -- rma-table).
One row per return: do_record_rma (nce/vertical_modules/inventory/rma.py)
INSERT-only creates this row with stock_movement_state = ''pending'' and
records the WEEE compliance lifecycle for the returned item. This table
records; it does not move stock -- no inventory_transactions row is written
on this path and inventory_items is left untouched. Batch 138b performs both
stock legs (restock-on-return and permanent WEEE disposal), transitioning
weee_state / stock_movement_state on this same row via the UPDATE grant
below; Batch 138c (dead-stock-reconcile) reads this table''s settled rows
against the ledger. FORCE RLS isolates per tenant; nce_app is granted
SELECT, INSERT, UPDATE but never DELETE -- an RMA is compliance evidence and
nothing in the application may erase a WEEE disposal record.';


-- Assets engine (Module 9, Wave 2 -- seed-from-bom): the relational asset
-- register behind nce/vertical_modules/assets/seed.py's
-- do_seed_asset_from_bom (migration 054).
--
-- MIRROR OF nce/migrations/054_assets.sql -- the DDL statements below are
-- byte-identical to that file's. A fresh install boots from this file alone;
-- an existing install runs the migration. Both paths must produce not merely
-- the same enforced expressions but the same catalog IDENTITY, which is why
-- every CHECK and UNIQUE carries an explicit name (an anonymous column CHECK
-- is auto-named, and the auto-name differs between the two paths -- the
-- divergence that caused a rejection on Batch 132). See migration 054's file
-- header for the full rationale: no graph writes in this wave (Batch 142b
-- owns the ASSET node + installed_as/lives_in edges), no FK on bom_line_id
-- or functional_location_id (neither target has a relational home yet), and
-- no enumerated CHECK on lifecycle_state (the state vocabulary is
-- config-as-IP in nce/config_data/asset-lifecycle.json).

CREATE TABLE IF NOT EXISTS assets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- The originating BOM line, as a caller-supplied identifier. This is the
    -- idempotency handle for do_seed_asset_from_bom (see the UNIQUE below)
    -- and Batch 142b's handle for the BOM_LINE -[installed_as]-> ASSET edge.
    -- NOT an FK: no BOM_LINE table or node exists yet (file header).
    bom_line_id            TEXT        NOT NULL,
    -- Serialised units; a seed made before the installer scans a serial has
    -- none. Nullable on purpose -- an absent serial is an honest "not
    -- captured yet", never an empty string (assets_serial_not_blank).
    serial                 TEXT,
    -- The room the asset lives in. Nullable for the same reason, and NOT an
    -- FK (file header). Batch 142b builds ASSET -[lives_in]->
    -- FUNCTIONAL_LOCATION from this column.
    functional_location_id TEXT,
    -- The 14-state lifecycle position. Written once here, at seed time, from
    -- asset-lifecycle.json's entry state; transitions belong to a later
    -- wave's do_advance_lifecycle, which is why nce_app holds UPDATE below.
    -- No enumerated CHECK -- see the file header.
    lifecycle_state        TEXT        NOT NULL,
    change_origin          TEXT        NOT NULL DEFAULT 'agent',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- THE idempotency arbiter: one asset per (namespace, BOM line). Seeding
    -- the same line twice is refused HERE, by the database, not by a Python
    -- "does it exist?" pre-check -- two concurrent identical seeds would both
    -- pass such a pre-check and both insert. Precedent: migration 053's
    -- inventory_rma_ns_ref_uq, migration 052's goods_receipts_idempotency_uq.
    CONSTRAINT assets_ns_bom_line_uq UNIQUE (namespace_id, bom_line_id),
    -- Structural non-blank guards: a whitespace-only identifier is not an
    -- identifier, and must not be able to occupy the idempotency key.
    CONSTRAINT assets_bom_line_id_not_blank
        CHECK (btrim(bom_line_id) <> ''),
    CONSTRAINT assets_lifecycle_state_not_blank
        CHECK (btrim(lifecycle_state) <> ''),
    CONSTRAINT assets_serial_not_blank
        CHECK (serial IS NULL OR btrim(serial) <> ''),
    CONSTRAINT assets_functional_location_id_not_blank
        CHECK (functional_location_id IS NULL OR btrim(functional_location_id) <> ''),
    CONSTRAINT assets_change_origin_check
        CHECK (change_origin IN
            ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

-- The room-centric register read named in 09-assets-engine.md's REST surface
-- (api_assets_register: "assets by FUNCTIONAL_LOCATION"). The
-- (namespace_id, bom_line_id) read is already served by the unique index
-- behind assets_ns_bom_line_uq. Deliberately NOT indexed here: serial,
-- lifecycle_state -- nothing in this wave or Batch 142b reads by either, and
-- the wave that does owns its own index.
CREATE INDEX IF NOT EXISTS idx_assets_namespace_functional_location
    ON assets (namespace_id, functional_location_id);

ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE assets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON assets;
CREATE POLICY tenant_isolation_policy ON assets
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE assets FROM nce_app;
        -- UPDATE is granted for do_advance_lifecycle, which transitions
        -- lifecycle_state on this same row (09-assets-engine.md, build phase
        -- B1) -- the same forward-provisioning migration 053 did for Batch
        -- 138b. No DELETE: retirement is a lifecycle STATE (RETIRED), never a
        -- deleted row -- an asset register that can forget a device is not a
        -- register. A namespace teardown still removes rows via the
        -- namespace_id FK's ON DELETE CASCADE, which RLS grants do not gate.
        GRANT SELECT, INSERT, UPDATE ON TABLE assets TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE assets IS
'Relational asset register (Module 9, Wave 2 -- seed-from-bom). One row per
originating BOM line: do_seed_asset_from_bom
(nce/vertical_modules/assets/seed.py) is the SOLE writer and is INSERT-only,
creating the row with lifecycle_state taken from
nce/config_data/asset-lifecycle.json''s entry state via Batch 141''s pure
lifecycle module. Idempotency is by DB constraint
(assets_ns_bom_line_uq + INSERT ... ON CONFLICT DO NOTHING), never a
check-then-write. This table is the RELATIONAL half only: the ASSET kg_node
and the BOM_LINE -[installed_as]-> ASSET / ASSET -[lives_in]->
FUNCTIONAL_LOCATION edges are Batch 142b''s, as is ASSET''s row in
node-ownership.json -- no code in this wave writes kg_nodes or kg_edges.
bom_line_id and functional_location_id are identifier columns with NO foreign
key: neither target has a relational home yet (BOM_LINE nodes are Batch
132a, unbuilt; FUNCTIONAL_LOCATION is the unresolved intent->as-built spine
gap, roadmap §9.1). lifecycle_state carries no enumerated CHECK because the
state vocabulary is config-as-IP in asset-lifecycle.json, tuned per tenant.
FORCE RLS isolates per tenant; nce_app is granted SELECT, INSERT, UPDATE
(the UPDATE is for a later wave''s do_advance_lifecycle) but never DELETE --
retirement is the RETIRED lifecycle state, not an erased row.';

-- ============================================================================
-- Module 11, Wave 4 (goods-receipt, Batch 132): the record of one inbound
-- delivery. Idempotent on (namespace_id, receipt_hash) -- see migration 052
-- for the full rationale. inventory_items remains authoritative stock;
-- inventory_transactions remains the movement ledger; this table is neither
-- -- it is the delivery's own record. No kg_node/kg_edge is written from
-- here (that is Batch 132b).
-- ============================================================================

CREATE TABLE IF NOT EXISTS goods_receipts (
    id             UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- Stored in the SAME normal form the receipt hash uses: stripped and
    -- UPPER-CASED by goods_receipt.py's _as_po_ref, once, at the boundary.
    -- See the COMMENT ON COLUMN below -- Batch 133's matcher queries this
    -- column and must not have to guess the case.
    po_ref         TEXT          NOT NULL,
    -- OPTIONAL delivery-note / packing-slip number, same normal form as
    -- po_ref (stripped + upper-cased; blank collapses to NULL). PARTICIPATES
    -- IN receipt_hash, which is the whole point: two genuine PARTIAL
    -- deliveries against one PO -- same location, byte-identical line set,
    -- no scans -- would otherwise hash identically and the second would be
    -- swallowed as a replay, silently losing stock. NULL is legal and means
    -- "no note supplied": hashing then behaves exactly as it did before this
    -- column existed, collision included.
    delivery_note_ref TEXT,
    location_id    UUID          NOT NULL,
    -- Aggregated, sku-sorted line list -- see goods_receipt.py's
    -- _compute_receipt_hash for the exact canonical shape this is hashed
    -- from. Never mutated after insert (WORM-adjacent: a correction is a NEW
    -- receipt, never an edit of this row's lines).
    lines          JSONB         NOT NULL,
    -- Per-unit barcode/serial capture, optional. A SECOND way to distinguish
    -- two deliveries with an identical line set (delivery_note_ref above is
    -- the primary one): real serials differ between real deliveries even
    -- when the aggregate line set does not.
    scans          JSONB         NOT NULL DEFAULT '[]'::jsonb,
    -- Reserved for Batch 133's Receive->Match->Cascade verdict -- NULL until
    -- that wave lands. This wave creates the column and writes nothing to
    -- it; do not populate it here.
    match_result   JSONB,
    -- sha256 hex over the canonically normalised (po_ref, delivery_note_ref,
    -- location_id, lines, scans) payload -- see goods_receipt.py's
    -- _compute_receipt_hash. Deliberately excludes received_at/created_at/id:
    -- including a timestamp would make every retry a new receipt and defeat
    -- idempotency entirely.
    receipt_hash   TEXT          NOT NULL,
    received_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    -- Composite FK target is stock_locations_id_ns_uq (migration 050) -- a
    -- receipt can never point at another tenant's location, refused by
    -- Postgres, not by careful code.
    CONSTRAINT goods_receipts_location_fk
        FOREIGN KEY (location_id, namespace_id)
        REFERENCES stock_locations (id, namespace_id)
        ON DELETE CASCADE,
    -- THE idempotency arbiter. A replay's INSERT ... ON CONFLICT (namespace_id,
    -- receipt_hash) DO NOTHING returns no row, and every subsequent effect in
    -- do_record_goods_receipt is gated on that row having been returned --
    -- that gating IS the idempotency, refused by Postgres at the DB level,
    -- never a Python-side check-then-write.
    CONSTRAINT goods_receipts_idempotency_uq UNIQUE (namespace_id, receipt_hash)
);

-- Idempotent re-run safety for a database that already received an EARLIER
-- revision of this migration (the table exists, so CREATE TABLE IF NOT EXISTS
-- above is a no-op and would leave the column missing). On both audited
-- install paths -- schema.sql alone, and origin/main's schema.sql + this file
-- -- the column already came from the CREATE TABLE above and this statement
-- does nothing, so the two catalogs stay identical. Same statement, verbatim,
-- in nce/schema.sql.
ALTER TABLE goods_receipts ADD COLUMN IF NOT EXISTS delivery_note_ref TEXT;

COMMENT ON COLUMN goods_receipts.po_ref IS
'Purchase-order reference, stored in ONE normal form: stripped and
UPPER-CASED (goods_receipt.py''s _as_po_ref), the same value hashed into
receipt_hash. Normalising for the hash but storing verbatim -- an earlier
revision of this wave -- made idempotency case-insensitive while this column
and idx_goods_receipts_namespace_po stayed case-sensitive, so a replay was
correctly detected yet a lookup by the canonical case found nothing. Batch
133''s matcher queries this column: match against the upper-cased form.';

COMMENT ON COLUMN goods_receipts.delivery_note_ref IS
'OPTIONAL delivery-note / packing-slip number from the paperwork that arrived
with the goods; stripped and UPPER-CASED like po_ref, blank collapsed to
NULL. PARTICIPATES IN receipt_hash so two genuine PARTIAL deliveries against
the same PO line -- identical location, identical aggregated lines, no scans
-- are two receipts instead of one swallowed replay, while a true retry of
the SAME note remains idempotent. NULL (no note supplied) reproduces the
pre-existing behaviour exactly, collision included.';

COMMENT ON COLUMN goods_receipts.match_result IS
'Reserved for Batch 133''s Receive->Match->Cascade verdict. NULL until that
wave lands; this wave (Batch 132) creates the column and never writes to it.';

CREATE INDEX IF NOT EXISTS idx_goods_receipts_namespace_po
    ON goods_receipts (namespace_id, po_ref);

CREATE INDEX IF NOT EXISTS idx_goods_receipts_namespace_location
    ON goods_receipts (namespace_id, location_id);

ALTER TABLE goods_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE goods_receipts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON goods_receipts;
CREATE POLICY tenant_isolation_policy ON goods_receipts
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE goods_receipts FROM nce_app;
        -- Live mutable-shape record (not a WORM ledger like inventory_transactions)
        -- -- nce_app gets the full CRUD set, mirroring stock_locations/inventory_items
        -- (migration 050). match_result is the one column a LATER wave (Batch 133)
        -- will UPDATE; this wave never does.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE goods_receipts TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE goods_receipts IS
'Record of one inbound delivery (Module 11, Wave 4 -- goods-receipt, Batch
132). Idempotent on (namespace_id, receipt_hash) -- goods_receipts_idempotency_uq
refuses a duplicate INSERT at the DB level, so a replay (including two
concurrent identical submissions) increments stock exactly once.
receipt_hash covers po_ref, delivery_note_ref, location_id, lines and scans;
supplying delivery_note_ref is how two GENUINE partial deliveries against one
PO line stay two receipts. This row is
the RECORD of the delivery; inventory_items (migration 050) remains the
AUTHORITATIVE stock row and inventory_transactions (migration 051) the
movement ledger -- do_record_goods_receipt increments the former and appends
one goods_receipt-category row per line to the latter, inside the SAME
transaction as this row''s own INSERT. match_result is reserved for Batch
133''s Receive->Match->Cascade verdict and is NULL until then. The graph
projection (GOODS_RECEIPT kg_node, -[against]->PO / -[of]->SKU edges) is
Batch 132b''s -- no kg_node or kg_edge is written from this table or its
writer. FORCE RLS isolates per tenant; location_id is a composite FK on
(location_id, namespace_id) into stock_locations so a receipt can never
reference another tenant''s location.';

-- ============================================================================
-- System Design engine (Module 6, Wave 14 -- B067e): canvas geometry AND the
-- per-DESIGN optimistic-concurrency token, behind
-- nce/vertical_modules/system_design/geometry.py.
--
-- MIRROR OF nce/migrations/060_system_design_geometry.sql -- the DDL
-- statements below are byte-identical to that file's. A fresh install boots
-- from this file alone; an existing install runs the migration. Both paths
-- must produce not merely the same enforced expressions but the same catalog
-- IDENTITY, which is why every CHECK and UNIQUE carries an explicit name (an
-- anonymous column CHECK is auto-named, and the auto-name differs between the
-- two paths -- the divergence that caused a rejection on Batch 132).
--
-- See migration 060's file header for the full rationale, in particular:
--   * the TWO KEY GRAINS (node-geometry rows vs the one DESIGN version row)
--     and how to tell them apart -- version IS NOT NULL;
--   * that x/y are CANVAS GRID UNITS, origin TOP-LEFT, y-down, and that room
--     dimensions live in meta under copper.room.w/d/h in METERS;
--   * that rack_position / rack_face carry NetBox's contractual vocabulary and
--     may not be renamed.
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_design_geometry (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,

    -- Grain key.  A NODE label for a geometry row; the DESIGN label for the
    -- one version row.  See the file header.
    node_label      TEXT        NOT NULL,

    -- Canvas placement.  Grid units, origin top-left, y-down (Rev 2 section 4).
    x               NUMERIC,
    y               NUMERIC,

    -- Rack elevation (NetBox vocabulary -- do not rename).
    -- Half-U granularity: 0.0, 0.5, 1.0 ... 999.5 -- enforced by
    -- validate_geometry(), not by this column (see the file header).
    -- 999.9 fits the column but is NOT a legal value: it is not a half-U.
    rack_position   NUMERIC(4,1),
    rack_face       TEXT,

    -- Cable run.  Length in METERS; cable_type is free text (Cat6A, OM4, ...)
    -- and is deliberately not enumerated -- the vocabulary is the installer's.
    cable_length_m  NUMERIC,
    cable_type      TEXT,

    -- Escape hatch.  Room dimensions live here under copper.room.w/d/h in
    -- METERS.  Reserved copper.* keys are stored verbatim and interpreted by
    -- Copper, never by NCE (Rev 2 section 5).
    meta            JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Per-DESIGN optimistic-concurrency token.  NULL on every geometry row;
    -- set only on the design version row.  Monotonic, starts at 0 when the
    -- row is created and is incremented by every authoring write.
    version         BIGINT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT system_design_geometry_pkey PRIMARY KEY (id),
    CONSTRAINT system_design_geometry_ns_node_uq UNIQUE (namespace_id, node_label),
    CONSTRAINT system_design_geometry_node_label_not_blank
        CHECK (btrim(node_label) <> ''),
    CONSTRAINT system_design_geometry_rack_face_check
        CHECK (rack_face IS NULL OR rack_face IN ('front', 'rear')),
    -- Every stored coordinate must be a REAL number.
    --
    -- NOT written as `x = x`. That idiom catches NaN for IEEE floats but is a
    -- NO-OP on NUMERIC: PostgreSQL defines NUMERIC 'NaN' = 'NaN' as TRUE so
    -- that NaN sorts and groups deterministically. Verified on PG 16.14.
    -- The three special values are therefore excluded by name.
    --
    -- validate_geometry() already refuses these at the write boundary; this
    -- is the structural backstop for a writer that does not go through it --
    -- psql, a repair script, a future core. A stored NaN cannot be undone
    -- (there is no delete path) and makes the WHOLE design's topology
    -- response raise for every reader, so it is worth a constraint.
    CONSTRAINT system_design_geometry_numerics_finite
        CHECK (
            (x IS NULL OR x NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
            AND (y IS NULL OR y NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
            AND (rack_position IS NULL OR rack_position NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
            AND (cable_length_m IS NULL OR cable_length_m NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
        ),
    -- And it must survive the JSON round trip.
    --
    -- NUMERIC stores 10^400 happily; the read path converts back with
    -- float(Decimal), which does NOT raise for an over-large Decimal -- it
    -- returns inf -- and JSONResponse.render (allow_nan=False) then raises on
    -- that. So a merely LARGE finite value poisons a design exactly as NaN
    -- does. The bound is the IEEE double maximum.
    --
    -- It is spelled as the EXACT 309-digit expansion of that double, not as
    -- the familiar 1.7976931348623157e308, and the difference is load-bearing:
    -- the short form is the 17-digit ROUNDED decimal and is strictly SMALLER
    -- than the real maximum. Using it here put every value in the gap into a
    -- state the application accepted and the database refused -- a
    -- CheckViolationError, i.e. a 500, which is the exact defect class this
    -- constraint exists to prevent. Caught by the test below before it shipped.
    --
    -- The application bound is Decimal(sys.float_info.max) -- this same exact
    -- value. Agreement between the two is NOT automatic just because the
    -- numbers match: an earlier revision claimed here that "the two agree on
    -- every input" while the application compared with Python's abs(), which
    -- ROUNDS a Decimal to the context precision (28 significant digits against
    -- this value's 309) and so accepted a ~1.8e280-wide band of values above
    -- the true maximum -- which this constraint then refused as a 500. The
    -- application now compares with Decimal.copy_abs(), which does no
    -- rounding. THAT is what makes the two agree, and it is a property of the
    -- comparison, not of the constant: anything here that reintroduces a
    -- rounding operation on either side reopens the gap.
    --
    -- This is a serialisation limit expressed in the schema, which is a real
    -- trade-off: a future consumer reading NUMERIC natively would not need
    -- it. It is here anyway because EVERY consumer of this table today goes
    -- through do_get_topology's JSON, and a silent 500 for every reader of a
    -- design is worse than a visible ALTER TABLE for the wave that one day
    -- needs bigger numbers.
    CONSTRAINT system_design_geometry_numerics_in_double_range
        CHECK (
            (x IS NULL OR abs(x) <= '179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368'::numeric)
            AND (y IS NULL OR abs(y) <= '179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368'::numeric)
            AND (cable_length_m IS NULL OR abs(cable_length_m) <= '179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368'::numeric)
        ),
    CONSTRAINT system_design_geometry_version_non_negative
        CHECK (version IS NULL OR version >= 0)
);

-- Index: the primary read path -- geometry for a batch of node labels within
-- one namespace, and the single-row version lookup, are the same shape.
-- (namespace_id, node_label) is already unique-indexed by the UNIQUE above;
-- no second index is added for it.

-- Row-Level Security.
ALTER TABLE system_design_geometry ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_geometry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON system_design_geometry;
CREATE POLICY tenant_isolation_policy ON system_design_geometry
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE system_design_geometry FROM nce_app;
        -- UPDATE is required: geometry is re-authored in place on every canvas
        -- save, and the version row is incremented rather than appended.
        -- DELETE is granted for parity with the sibling capability table
        -- (migration 039); no code in this wave issues one.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_design_geometry TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE system_design_geometry IS
'Canvas geometry AND the per-DESIGN optimistic-concurrency token for the System
Design engine (Module 6, Wave 14).

TWO KEY GRAINS IN ONE TABLE -- deliberate, and the only exception in this
engine. Most rows are GEOMETRY rows keyed by a NODE label (DEVICE:/PORT:/RACK:/
CABLE:/FL:) carrying x/y, rack_position/rack_face, cable_length_m/cable_type
and meta, with version NULL. Exactly one row per design is the DESIGN VERSION
row, keyed by the DESIGN label (DESIGN:<ID>), carrying version and no geometry.
Distinguish them by version IS NOT NULL (equivalently node_label LIKE
''DESIGN:%''); a geometry row is any row with version NULL. They share a table
because they share the natural key, the tenancy boundary, the lifecycle and the
writer transaction, and because a DESIGN node is never placed on the canvas so
the two grains cannot collide on (namespace_id, node_label).

UNITS AND AXES ARE NORMATIVE (Rev 2 section 4): x/y are CANVAS GRID UNITS with
the origin TOP-LEFT and y increasing DOWNWARD. NCE converts nothing; exporters
convert. Room dimensions are NOT x/y -- they live in meta under
copper.room.w / copper.room.d / copper.room.h, in METERS.

NAMING IS CONTRACTUAL: rack_position and rack_face carry the NetBox vocabulary
(position/face) that Copper follows as a binding ADR. Renaming either breaks
Copper.

version is monotonic per design, starts at 0 and is incremented inside the
authoring write''s own transaction. A caller that supplies expected_version
gets a compare-and-swap; a caller that omits it gets last-writer-wins and the
increment still happens.

SCOPE OF THAT PROMISE: it covers writes made through the two authoring
adapters in nce/vertical_modules/system_design/mcp_handlers.py -- the
system_design_author_topology and system_design_author_functional_location
tools and their REST twins -- and ONLY those. Three other modules under
system_design/ write kg_nodes/kg_edges for a design without passing through
them and never move the token: from_quote.py, to_quote.py and
netbox_bridge.py. All three are unwired today (no non-test callers), so this
is latent rather than exploitable -- but it is not hypothetical, because
read.py''s edge projection filters on subject_label only, so to_quote.py''s
DESIGN -[becomes]-> QUOTE edge would appear in a topology read while version
stood still. Do not read this token as covering every change to a design.

FORCE RLS isolates per tenant, but the pools that serve requests are owner
pools and bypass it -- the real boundary is the explicit namespace_id predicate
in nce/vertical_modules/system_design/geometry.py.';

COMMENT ON COLUMN system_design_geometry.node_label IS
'GRAIN KEY. A node label (DEVICE:/PORT:/RACK:/CABLE:/FL:) on a geometry row;
the DESIGN label on the one version row. Matches kg_nodes.label for geometry
rows.';

COMMENT ON COLUMN system_design_geometry.x IS
'Canvas X in GRID UNITS. Origin TOP-LEFT (Rev 2 section 4). Not meters.';

COMMENT ON COLUMN system_design_geometry.y IS
'Canvas Y in GRID UNITS, increasing DOWNWARD (y-down), origin TOP-LEFT
(Rev 2 section 4). Not meters.';

COMMENT ON COLUMN system_design_geometry.rack_position IS
'NetBox "position": the lowest rack unit this device occupies. NUMERIC(4,1)
carries one decimal place; the HALF-U STEP (a multiple of 0.5) is enforced by
validate_geometry() at the write boundary, NOT by this column -- a direct
INSERT of 1.27 is still rounded to 1.3 silently. Legal range is 0.0 to 999.5
in 0.5 steps; 999.9 fits the column but is not a half-U and is refused.
CONTRACTUAL NAME -- renaming breaks Copper.';

COMMENT ON COLUMN system_design_geometry.rack_face IS
'NetBox "face": ''front'' or ''rear''. CONTRACTUAL NAME AND VOCABULARY --
renaming or extending breaks Copper.';

COMMENT ON COLUMN system_design_geometry.cable_length_m IS
'Cable run length in METERS.';

COMMENT ON COLUMN system_design_geometry.meta IS
'Verbatim passthrough store. Room dimensions live here under copper.room.w /
copper.room.d / copper.room.h, in METERS. NCE stores, Copper interprets
(Rev 2 section 5).';

COMMENT ON COLUMN system_design_geometry.version IS
'Per-DESIGN optimistic-concurrency token. NULL on every geometry row; set only
on the design version row, where it starts at 0 and is incremented by every
authoring write inside that write''s own transaction. Its presence is the
discriminator between the two key grains.';

-- ============================================================================
-- System Design engine (Module 6, Wave 16 -- B067g): per-node LIFECYCLE STATE
-- (status / revision / salience) for a DEVICE, a RACK or a CABLE, behind
-- nce/vertical_modules/system_design/devices.py.
--
-- MIRROR OF nce/migrations/061_system_design_node_state.sql -- the DDL
-- statements below are byte-identical to that file's. A fresh install boots
-- from this file alone; an existing install runs the migration. Both paths
-- must produce not merely the same enforced expressions but the same catalog
-- IDENTITY, which is why every CHECK and UNIQUE carries an explicit name (an
-- anonymous column CHECK is auto-named, and the auto-name differs between the
-- two paths -- the divergence that caused a rejection on Batch 132).
--
-- See migration 061's file header for the full rationale, in particular:
--   * why this is a SIBLING table and not a column on system_design_geometry,
--     and that the accepted cost is a second join in do_get_topology;
--   * why the status CHECK is COMPOSITE per node_type rather than a union --
--     a union would let a CABLE be 'inventory' -- and why its ELSE branch is
--     FALSE, which is what refuses a PORT state row structurally;
--   * that NOTHING backfills this table and nothing may COALESCE the absence
--     of a row to 'planned', because the W17 retirement guard denies on an
--     absent state and needs "no row" to stay distinguishable from a stored
--     'planned';
--   * that revision is inert storage and is NOT the PolyForm-licensed
--     netbox-branching model.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Module 0, Wave 31 (Batch 132a): bom_line_content -- mirror of migration 058.
-- See that file's header for the full design rationale. Inserted BEFORE the
-- system_design_node_state block for historical reasons -- NOT because that
-- block is the last one in this file (it no longer is; telemetry_samples was
-- appended after it by the telemetry merge). See
-- tests/test_system_design_node_state.py's bounded-mirror-slice control for
-- why the mirror check does not depend on this table being last.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bom_line_content (
    id                 UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line_label     TEXT          NOT NULL,
    quote_id           TEXT          NOT NULL,
    line_ref           TEXT          NOT NULL,
    qty                NUMERIC(14,4) NOT NULL,
    unit_price         NUMERIC(18,2) NOT NULL,
    line_total         NUMERIC(18,2) NOT NULL,
    currency           CHAR(3)       NOT NULL DEFAULT 'NOK',
    -- Open-by-construction provenance (no CHECK) -- see header comment.
    -- Trust boundary: set ONLY by nce/bom_lines.py's own flow-to-origin_kind
    -- mapping, never from a caller-supplied tool argument.
    origin_kind        TEXT          NOT NULL,
    origin_ref         TEXT,
    writer_engine      TEXT          NOT NULL,
    status             TEXT          NOT NULL DEFAULT 'DRAFT',
    status_changed_at  TIMESTAMPTZ,
    -- Immutable once set -- enforced by the trigger below, not by DDL alone
    -- (a CHECK cannot see OLD).
    frozen_at          TIMESTAMPTZ,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Every CHECK/UNIQUE is explicitly named -- an anonymous column CHECK is
-- auto-named on one path (CREATE TABLE) and can diverge from the name a
-- second path (e.g. a later ALTER) would pick, which is exactly the
-- fresh-install-vs-migrated divergence that caused a prior rejection on this
-- table family (Batch 132's own history).
ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_natural_key;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_natural_key UNIQUE (namespace_id, bom_line_label);

ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_qty_positive_chk;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_qty_positive_chk CHECK (qty > 0);

ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_unit_price_nonneg_chk;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_unit_price_nonneg_chk CHECK (unit_price >= 0);

ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_line_total_nonneg_chk;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_line_total_nonneg_chk CHECK (line_total >= 0);

-- Non-unique: supports both the per-line lookup (namespace_id + bom_line_label,
-- covered by the natural key above) and the per-quote listing/read
-- (namespace_id + quote_id) that 132f and Batch 142 will use.
CREATE INDEX IF NOT EXISTS idx_bom_line_content_namespace_quote
    ON bom_line_content (namespace_id, quote_id);

-- ----------------------------------------------------------------------------
-- Freeze semantics: a BEFORE UPDATE trigger, not a GRANT and not the registry.
-- status / status_changed_at are deliberately OUTSIDE the protected set --
-- content freezes, status keeps advancing. frozen_at itself is immutable
-- once set, independent of the content freeze check.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reject_frozen_bom_line_mutation() RETURNS TRIGGER AS $BODY$
BEGIN
    IF OLD.frozen_at IS NOT NULL AND NEW.frozen_at IS DISTINCT FROM OLD.frozen_at THEN
        RAISE EXCEPTION
            'bom_line_content.frozen_at is immutable once set (label=%)', OLD.bom_line_label;
    END IF;

    IF OLD.frozen_at IS NOT NULL THEN
        IF NEW.quote_id       IS DISTINCT FROM OLD.quote_id
        OR NEW.line_ref       IS DISTINCT FROM OLD.line_ref
        OR NEW.qty            IS DISTINCT FROM OLD.qty
        OR NEW.unit_price     IS DISTINCT FROM OLD.unit_price
        OR NEW.line_total     IS DISTINCT FROM OLD.line_total
        OR NEW.currency       IS DISTINCT FROM OLD.currency
        OR NEW.origin_kind    IS DISTINCT FROM OLD.origin_kind
        OR NEW.origin_ref     IS DISTINCT FROM OLD.origin_ref
        OR NEW.writer_engine  IS DISTINCT FROM OLD.writer_engine
        THEN
            RAISE EXCEPTION
                'bom_line_content: content is frozen and cannot be mutated (label=%)',
                OLD.bom_line_label;
        END IF;
    END IF;

    RETURN NEW;
END;
$BODY$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reject_frozen_bom_line_mutation ON bom_line_content;
CREATE TRIGGER trg_reject_frozen_bom_line_mutation
    BEFORE UPDATE ON bom_line_content
    FOR EACH ROW EXECUTE FUNCTION reject_frozen_bom_line_mutation();

-- ---------------------------------------------------------------------------
-- 063_bom_line_priced.sql mirror -- D48: unpriced is a STATE, not a value.
-- See that migration's header for the full rationale.
-- ---------------------------------------------------------------------------
ALTER TABLE bom_line_content ADD COLUMN IF NOT EXISTS priced BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE bom_line_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE bom_line_content FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON bom_line_content;
CREATE POLICY tenant_isolation_policy ON bom_line_content
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE bom_line_content FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE bom_line_content TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE bom_line_content IS
'Shared, top-level BOM_LINE content store (Module 0, Wave 31 / Batch 132a).
Written by system_design (content:create:design, content:update:design) and
by sales (content:create:manual/package/external,
content:update:manual/package/external, content:freeze) -- guarded per-flow
by node_ownership_registry via nce/bom_lines.py, never engine-prefixed
because both engines write it. Natural-keyed (namespace_id, bom_line_label);
INSERT ... ON CONFLICT DO NOTHING makes a replay of the same
(namespace, label) a no-op by construction. Content (qty/unit_price/
line_total/currency/origin_*) freezes via trg_reject_frozen_bom_line_mutation
once frozen_at is set (from do_freeze_baseline, sales/baseline.py); status
stays mutable after freeze -- see the header comment for the full field-
ownership split. actual_cost is NOT this table''s column -- see
economy_bom_actual_costs (migration 047). FORCE RLS isolates per tenant;
every query must ALSO carry an explicit namespace_id predicate because the
owner/superuser pool used by background jobs bypasses FORCE RLS (bitten three
prior waves: B67, B120, B130).';

CREATE TABLE IF NOT EXISTS system_design_node_state (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,

    -- Graph key -- matches (label, namespace_id) in kg_nodes, exactly as
    -- system_design_device_capabilities and system_design_geometry do.
    node_label      TEXT        NOT NULL,

    -- The node's entity_type. On the row because the status vocabulary is per
    -- node type and the CHECK below has to see it. DEVICE | RACK | CABLE only:
    -- the CHECK's ELSE FALSE refuses everything else, PORT included.
    node_type       TEXT        NOT NULL,

    -- NetBox lifecycle status. NULLABLE AND WITHOUT A DEFAULT, deliberately --
    -- see the file header. NULL means "we hold data for this node, nobody has
    -- declared its lifecycle", which W17 denies on exactly as it denies on a
    -- missing row.
    status          TEXT,

    -- INERT STORAGE this wave. Free text; NCE interprets nothing.
    revision        TEXT,

    -- Per-node salience. kg_nodes has no salience column; this is it.
    -- Finite and non-negative -- see the file header for why NaN is the
    -- dangerous case and why one clause catches all four bad shapes.
    salience        NUMERIC,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT system_design_node_state_pkey PRIMARY KEY (id),
    CONSTRAINT system_design_node_state_ns_node_uq UNIQUE (namespace_id, node_label),
    CONSTRAINT system_design_node_state_node_label_not_blank
        CHECK (btrim(node_label) <> ''),
    -- COMPOSITE, per node_type. See the header: a union CHECK would let a
    -- CABLE be 'inventory'. ELSE FALSE denies unknown node types by default.
    --
    -- The `status IS NULL` allowance is INSIDE each arm, never in front of the
    -- CASE: in front, a NULL status short-circuits the whole expression and a
    -- PORT row slips past ELSE FALSE. THAT PLACEMENT IS LOAD-BEARING.
    --
    -- The disjunct itself is NOT. `NULL IN ('planned', ...)` evaluates to NULL
    -- and a CHECK that evaluates to NULL PASSES, so a NULL status is accepted
    -- with or without it. The mutation sweep proved that: removing it left the
    -- whole suite green. It is kept as DOCUMENTATION -- it says a NULL status
    -- is permitted on purpose rather than by three-valued accident -- and no
    -- test can gate it, which is recorded rather than papered over. Do NOT
    -- "simplify" it to `status IS NOT NULL AND ...`: that DOES change
    -- behaviour and breaks the revision-only row.
    CONSTRAINT system_design_node_state_status_per_node_type
        CHECK (
            CASE node_type
                WHEN 'DEVICE' THEN status IS NULL OR status IN (
                    'planned', 'staged', 'active', 'offline',
                    'decommissioning', 'inventory', 'failed'
                )
                WHEN 'CABLE' THEN status IS NULL OR status IN (
                    'planned', 'connected', 'decommissioning'
                )
                WHEN 'RACK' THEN status IS NULL OR status IN (
                    'reserved', 'available', 'planned', 'active', 'deprecated'
                )
                ELSE FALSE
            END
        ),
    -- Finite and non-negative. NaN passes >= 0 (numeric NaN sorts above
    -- everything) and is caught by < Infinity; +Infinity is caught by
    -- < Infinity; -Infinity and any negative are caught by >= 0.
    CONSTRAINT system_design_node_state_salience_finite_non_negative
        CHECK (
            salience IS NULL
            OR (salience >= 0 AND salience < 'Infinity'::numeric)
        )
);

-- Index: the primary read path -- state for a batch of node labels within one
-- namespace. (namespace_id, node_label) is already unique-indexed by the
-- UNIQUE above; no second index is added for it, and none is added for a
-- status filter: that filter (B067g2) narrows an already-narrow label set.

-- Row-Level Security.
ALTER TABLE system_design_node_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_node_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON system_design_node_state;
CREATE POLICY tenant_isolation_policy ON system_design_node_state
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE system_design_node_state FROM nce_app;
        -- UPDATE is required: a node's status is re-authored in place.
        -- DELETE is granted because W17 MUST delete a node's state row in the
        -- same transaction as the node itself -- see the orphan obligation in
        -- the file header. No code in THIS wave issues one.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_design_node_state TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE system_design_node_state IS
'Per-node lifecycle state for the System Design engine (Module 6, Wave 16):
status, revision and salience for a DEVICE, a RACK or a CABLE.

A SIBLING of system_design_geometry, not a column on it: geometry already
carries two key grains, it does not carry node_type (which the per-type status
CHECK needs on the row), and the wave that reads state never reads geometry.
The accepted cost is that do_get_topology joins two side tables instead of one.

THREE DISTINGUISHABLE STATES, and W17''s retirement guard needs all three:
NO ROW = nothing was ever declared about this node (every pre-W16 node, and it
stays that way until somebody declares something); status IS NULL = we hold
data for this node but nobody declared a lifecycle; status = a value = a
lifecycle was declared. W17 denies on the first two. The writer creates a row
only when the node is genuinely NEW to the authoring call or the caller
supplied an explicit lifecycle key, so an ordinary re-author, a geometry-only
canvas save and a re-author-shaped data-fix all leave a pre-existing node with
no row. NOTHING backfills this table.

status is NULLABLE AND HAS NO COLUMN DEFAULT on purpose: a DEFAULT ''planned''
would be a second, independent source of the one dangerous value, mintable by
any future writer or manual data-fix that never touches the write path.

THE STATUS CHECK IS COMPOSITE, PER node_type. A union CHECK would accept a
CABLE whose status is ''inventory''. The vocabulary is NetBox''s and Copper
follows it as a binding ADR:
  DEVICE -> planned | staged | active | offline | decommissioning | inventory |
            failed
  CABLE  -> planned | connected | decommissioning
  RACK   -> reserved | available | planned | active | deprecated
The CASE''s ELSE branch is FALSE, so an unknown node_type is refused. PORT is
deliberately among the refused: NetBox has no lifecycle status for a port and
none is invented here.

salience is FINITE and NON-NEGATIVE. PostgreSQL numeric NaN is not IEEE NaN --
it compares GREATER than every finite value and equal to itself -- so a stored
NaN would sort as the largest salience in the tenant and silently flip any W17
threshold predicate. Negative is refused because this engine''s own salience
decay clamps at a floor of zero, so a negative has no meaning in NCE.

W17 OBLIGATION: no FK ties a state row to its node (kg_nodes is HASH-partitioned
on label). W17 must delete a node''s state row in the same transaction as the
node, or a later re-author of the same deterministic label inherits the
orphan''s status through ON CONFLICT DO UPDATE.

revision is INERT STORAGE this wave (Copper-side sibling-retirement flow, Rev 2
section 7); it is explicitly NOT the PolyForm-licensed netbox-branching design.

FORCE RLS isolates per tenant, but the pools that serve requests are owner
pools and bypass it -- the real boundary is the explicit namespace_id predicate
in nce/vertical_modules/system_design/devices.py.';

COMMENT ON COLUMN system_design_node_state.node_label IS
'Graph key. Matches kg_nodes.label. One row per node, at most. No FK -- see the
W17 orphan obligation on the table comment.';

COMMENT ON COLUMN system_design_node_state.node_type IS
'The node''s entity_type, on the row because the status vocabulary is per node
type. DEVICE | RACK | CABLE -- the composite CHECK refuses anything else,
including PORT.';

COMMENT ON COLUMN system_design_node_state.status IS
'NetBox lifecycle status, validated per node_type by the composite CHECK.
CONTRACTUAL VOCABULARY -- adding or renaming a value is a Copper contract
change. NULLABLE AND WITHOUT A DEFAULT: NULL means "we hold data for this node,
nobody has declared its lifecycle", which W17 denies on exactly as it denies on
a missing row. A column DEFAULT would be a second source of ''planned'' that no
review of the write path could catch.';

COMMENT ON COLUMN system_design_node_state.revision IS
'Inert storage. Free text, stored verbatim, interpreted by Copper (Rev 2
section 7). NOT the PolyForm-licensed netbox-branching model.';

COMMENT ON COLUMN system_design_node_state.salience IS
'Per-node salience. Stored here because kg_nodes has no salience column.
FINITE and NON-NEGATIVE: PostgreSQL numeric NaN sorts ABOVE every finite value,
so a stored NaN would silently win every W17 threshold comparison.';

-- ============================================================================
-- Assets engine (Module 9, Wave 5 -- telemetry-adapter): the manufacturer
-- telemetry reading stream behind
-- nce/vertical_modules/assets/telemetry.py's do_pull_telemetry
-- (migration 057).
--
-- MIRROR OF nce/migrations/057_telemetry_samples.sql -- the DDL statements
-- below are byte-identical to that file's. A fresh install boots from this
-- file alone; an existing install runs the migration. Both paths must produce
-- not merely the same enforced expressions but the same catalog IDENTITY,
-- which is why every CHECK, UNIQUE and FK carries an explicit name (an
-- anonymous column CHECK is auto-named, and the auto-name differs between the
-- two paths -- the divergence that caused a rejection on Batch 132). See
-- migration 057's file header for the full rationale: no graph writes in this
-- wave (TELEMETRY has no node-ownership.json row and no node/edge is
-- written), a SINGLE-column FK on asset_id that proves existence but NOT
-- namespace membership, and no UPDATE/DELETE grant (a reading is not
-- revisable, and retention is a later wave's decision).
--
-- ORDERING: this block must follow the `assets` block above -- telemetry_
-- samples_asset_fk references assets(id).
-- ============================================================================

CREATE TABLE IF NOT EXISTS telemetry_samples (
    id            UUID             NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID             NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    -- The asset this reading was taken from. Single-column FK -- see the file
    -- header for exactly what that does and does not guarantee.
    asset_id      UUID             NOT NULL,
    -- Vendor metric name, verbatim from the adapter (e.g. 'uptime_seconds').
    -- No enumerated CHECK: the metric vocabulary is whatever the manufacturer
    -- platform emits and differs per vendor, so freezing a list in DDL would
    -- make onboarding a new platform require a migration.
    metric        TEXT             NOT NULL,
    -- The reading. DOUBLE PRECISION because vendor telemetry is float-valued;
    -- the finite CHECK below is what keeps a NaN/Infinity out of the
    -- healthScore inputs a later wave will average over.
    value         DOUBLE PRECISION NOT NULL,
    -- The instant the VENDOR sampled it, not the instant we pulled it. This
    -- is a component of the idempotency key precisely because re-pulling an
    -- overlapping window must re-deliver the same instants (created_at is the
    -- pull time and is deliberately NOT in that key).
    sampled_at    TIMESTAMPTZ      NOT NULL,
    -- The adapter's untouched payload for this sample, so a later health
    -- writer can recover vendor fields this table does not model.
    raw           JSONB            NOT NULL DEFAULT '{}'::jsonb,
    change_origin TEXT             NOT NULL DEFAULT 'agent',
    created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT telemetry_samples_asset_fk
        FOREIGN KEY (asset_id) REFERENCES assets (id) ON DELETE CASCADE,
    -- THE idempotency arbiter. A telemetry pull is a cron that re-reads
    -- overlapping windows, so the SAME reading arrives repeatedly by design;
    -- one (namespace, asset, metric, instant) is one row. Refused HERE, by
    -- the database, not by a Python "have I seen this?" pre-check -- two
    -- concurrent pulls would both pass such a pre-check and both insert.
    -- Precedent: migration 054's assets_ns_bom_line_uq.
    CONSTRAINT telemetry_samples_idempotency_uq
        UNIQUE (namespace_id, asset_id, metric, sampled_at),
    -- A blank metric name is not a metric, and must not be able to occupy the
    -- idempotency key.
    CONSTRAINT telemetry_samples_metric_not_blank
        CHECK (btrim(metric) <> ''),
    -- NaN and +/-Infinity are storable in DOUBLE PRECISION and would poison
    -- any average taken over this column. Note NaN is NOT caught by
    -- `value = value` in PostgreSQL -- unlike IEEE-754, PostgreSQL defines
    -- NaN = NaN as TRUE so its btree ordering is total -- so the comparison
    -- against 'NaN'::float8 below is the form that actually rejects it.
    CONSTRAINT telemetry_samples_value_finite
        CHECK (value <> 'NaN'::float8
               AND value <> 'Infinity'::float8
               AND value <> '-Infinity'::float8),
    CONSTRAINT telemetry_samples_change_origin_check
        CHECK (change_origin IN
            ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

-- No further index in this wave, deliberately. The one read this wave
-- performs -- "samples for this asset in this namespace" -- is already served
-- by the leading columns of the unique index behind
-- telemetry_samples_idempotency_uq. The "latest reading per metric" scan that
-- 09-assets-engine.md's do_compute_health will want is a
-- (namespace_id, asset_id, sampled_at DESC) index, and the wave that performs
-- that read owns it -- the same rule migration 054 applied to serial /
-- lifecycle_state.

ALTER TABLE telemetry_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry_samples FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON telemetry_samples;
CREATE POLICY tenant_isolation_policy ON telemetry_samples
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE telemetry_samples FROM nce_app;
        -- SELECT + INSERT only. No UPDATE: a reading that was taken is not
        -- revisable. No DELETE: nothing in the application may erase an
        -- observation, and retention is a later wave's explicit decision (see
        -- the file header). A namespace teardown still removes rows via the
        -- namespace_id FK's ON DELETE CASCADE, which RLS grants do not gate.
        GRANT SELECT, INSERT ON TABLE telemetry_samples TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE telemetry_samples IS
'Manufacturer-telemetry reading stream (Module 9, Wave 5 -- telemetry-adapter).
One row per (namespace, asset, metric, vendor sample instant):
do_pull_telemetry (nce/vertical_modules/assets/telemetry.py) is the SOLE
writer and is INSERT-only. Samples reach it through the TelemetryAdapter
interface; `mock` is the only adapter with real behaviour in this wave and the
five vendor platforms (crestron/qsys/neat/huddly/poly) are env-swap stubs
selected by NCE_ASSETS_TELEMETRY_<PLATFORM>_REAL that raise NotImplementedError
-- no vendor HTTP client, credential or dependency exists yet. Idempotency is
by DB constraint (telemetry_samples_idempotency_uq + INSERT ... ON CONFLICT DO
NOTHING), never a check-then-write, because a telemetry cron re-reads
overlapping windows by design; sampled_at is the VENDOR instant and created_at
the pull instant, and only the former is in the key. NO graph is written from
this table or its writer: the TELEMETRY kg_node and the
ASSET -[monitored_by]-> TELEMETRY edge are a later projection wave''s, and
TELEMETRY has no row in node-ownership.json. asset_id has a SINGLE-column FK
to assets(id) -- it proves the asset exists, NOT that it belongs to this
row''s namespace; that binding comes from FORCE RLS plus do_pull_telemetry''s
namespace-scoped pre-check, and closing the residual needs a
UNIQUE (id, namespace_id) on `assets` that migration 054 does not provide.
FORCE RLS isolates per tenant; nce_app is granted SELECT and INSERT only --
never UPDATE (a reading is not revisable) and never DELETE (retention is a
later wave''s decision).';

-- ============================================================================
-- Support Engine (Module 10): service_tickets, sla_clocks, customer_health
-- ============================================================================

CREATE TABLE IF NOT EXISTS service_tickets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    source                 TEXT        NOT NULL DEFAULT 'nce',
    source_id              TEXT,
    asset_id               UUID        REFERENCES assets(id) ON DELETE SET NULL,
    room_id                TEXT,
    customer_id            TEXT,
    status                 TEXT        NOT NULL DEFAULT 'open',
    priority               TEXT        NOT NULL DEFAULT 'medium',
    summary                TEXT        NOT NULL,
    description            TEXT,
    sla_profile            TEXT        NOT NULL DEFAULT 'standard',
    first_response_at      TIMESTAMPTZ,
    resolved_at            TIMESTAMPTZ,
    ai_diagnosis           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    events                 JSONB       NOT NULL DEFAULT '[]'::jsonb,
    support_source_id      TEXT,
    change_origin          TEXT        NOT NULL DEFAULT 'agent',
    synced_at              TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT service_tickets_summary_not_blank
        CHECK (btrim(summary) <> ''),
    CONSTRAINT service_tickets_source_check
        CHECK (source IN ('nce', 'd365')),
    CONSTRAINT service_tickets_status_check
        CHECK (status IN ('open', 'in_progress', 'waiting_customer', 'waiting_parts', 'resolved', 'closed', 'cancelled')),
    CONSTRAINT service_tickets_priority_check
        CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT service_tickets_change_origin_check
        CHECK (change_origin IN ('sync','webhook','agent','operator','consolidation','replay','unknown'))
);

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_status
    ON service_tickets (namespace_id, status);

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_room
    ON service_tickets (namespace_id, room_id)
    WHERE room_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_customer
    ON service_tickets (namespace_id, customer_id)
    WHERE customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_service_tickets_ns_asset
    ON service_tickets (namespace_id, asset_id)
    WHERE asset_id IS NOT NULL;

ALTER TABLE service_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_tickets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON service_tickets;
CREATE POLICY tenant_isolation_policy ON service_tickets
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE service_tickets FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE service_tickets TO nce_app;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS sla_clocks (
    ticket_id              UUID        NOT NULL REFERENCES service_tickets(id) ON DELETE CASCADE,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    sla_profile            TEXT        NOT NULL,
    first_response_due     TIMESTAMPTZ,
    resolution_due         TIMESTAMPTZ,
    breached               BOOLEAN     NOT NULL DEFAULT FALSE,
    breach_type            TEXT,
    paused_intervals       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticket_id),
    CONSTRAINT sla_clocks_breach_type_check
        CHECK (breach_type IS NULL OR breach_type IN ('first_response', 'resolution', 'both'))
);

CREATE INDEX IF NOT EXISTS idx_sla_clocks_ns_breached
    ON sla_clocks (namespace_id, breached);

CREATE INDEX IF NOT EXISTS idx_sla_clocks_ns_resolution_due
    ON sla_clocks (namespace_id, resolution_due)
    WHERE resolution_due IS NOT NULL;

ALTER TABLE sla_clocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE sla_clocks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON sla_clocks;
CREATE POLICY tenant_isolation_policy ON sla_clocks
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE sla_clocks FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE sla_clocks TO nce_app;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS customer_health (
    customer_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    score                  NUMERIC(5,2) NOT NULL,
    trend                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    churn_risk             TEXT        NOT NULL,
    drivers                JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_touchpoint_at     TIMESTAMPTZ,
    computed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, customer_id),
    CONSTRAINT customer_health_customer_id_not_blank
        CHECK (btrim(customer_id) <> ''),
    CONSTRAINT customer_health_score_range
        CHECK (score >= 0.00 AND score <= 100.00),
    CONSTRAINT customer_health_churn_risk_check
        CHECK (churn_risk IN ('low', 'medium', 'high', 'critical'))
);

CREATE INDEX IF NOT EXISTS idx_customer_health_ns_churn_risk
    ON customer_health (namespace_id, churn_risk);

ALTER TABLE customer_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_health FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON customer_health;
CREATE POLICY tenant_isolation_policy ON customer_health
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE customer_health FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE customer_health TO nce_app;
    END IF;
END $$;



-- ============================================================================
-- Field Tech Engine (Module 12) Tables
-- ============================================================================
-- ============================================================================
-- Field Tech Engine (Module 12, Wave 1 -- field-tech-schema):
-- Tables backing nce/vertical_modules/field_tech/** and unblocking Copper waves B196-B197:
--   1. work_orders (the physical unit of field work -- install or service)
--   2. checklists (checklist instances bound to a WO; ISO9001 verification records)
--   3. time_entries (GPS-derived or manual labor spans with offline-sync op_id dedup)
--
-- --------------------------------------------------------------------------
-- main runs through 064. PR #209 reserves 066 (066_audit_signing_key_rotation.sql).
--
-- STRICT ROW LEVEL SECURITY + EXPLICIT NAMESPACE PREDICATE ENFORCEMENT
-- --------------------------------------------------------------------------
-- All three tables enable and force RLS for nce_app.
-- As documented in Charter section 4.4, the live environment connects as mcp_user
-- (rolsuper=true, rolbypassrls=true). Therefore, RLS policies are defense-in-depth;
-- EVERY application query must carry explicit WHERE namespace_id =  predicates.
-- Partner views additionally carry partner_scope_id =  predicates.
-- ============================================================================

CREATE TABLE IF NOT EXISTS work_orders (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    work_order_id          TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id       UUID,
    kind                   TEXT        NOT NULL DEFAULT 'install',
    source_kind            TEXT        NOT NULL DEFAULT 'project',
    source_ref             TEXT        NOT NULL,
    location_id            TEXT,
    assignee_id            TEXT,
    assignee_kind          TEXT,
    status                 TEXT        NOT NULL DEFAULT 'draft',
    priority               TEXT        NOT NULL DEFAULT 'medium',
    summary                TEXT        NOT NULL DEFAULT '',
    due_at                 TIMESTAMPTZ,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    field_tech_source_id   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT work_orders_id_ns_unique UNIQUE (work_order_id, namespace_id),
    CONSTRAINT work_orders_kind_check CHECK (kind IN ('install', 'service')),
    CONSTRAINT work_orders_source_kind_check CHECK (source_kind IN ('project', 'ticket', 'manual')),
    CONSTRAINT work_orders_status_check CHECK (status IN ('draft', 'scheduled', 'dispatched', 'in_progress', 'completed', 'cancelled')),
    CONSTRAINT work_orders_priority_check CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT work_orders_assignee_kind_check CHECK (assignee_kind IS NULL OR assignee_kind IN ('employee', 'contractor'))
);

CREATE INDEX IF NOT EXISTS idx_work_orders_ns_status ON work_orders (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_work_orders_ns_assignee ON work_orders (namespace_id, assignee_id) WHERE assignee_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_orders_ns_partner ON work_orders (namespace_id, partner_scope_id) WHERE partner_scope_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_orders_ns_location ON work_orders (namespace_id, location_id) WHERE location_id IS NOT NULL;

ALTER TABLE work_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_orders FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON work_orders;
CREATE POLICY tenant_isolation_policy ON work_orders
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE work_orders FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE work_orders TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 2. checklists
-- ============================================================================

CREATE TABLE IF NOT EXISTS checklists (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    checklist_id           TEXT        NOT NULL,
    work_order_id          TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id       UUID,
    template_id            TEXT        NOT NULL,
    items                  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    completed_at           TIMESTAMPTZ,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT checklists_id_ns_unique UNIQUE (checklist_id, namespace_id),
    CONSTRAINT fk_checklists_work_orders FOREIGN KEY (work_order_id, namespace_id)
        REFERENCES work_orders (work_order_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checklists_ns_wo ON checklists (namespace_id, work_order_id);
CREATE INDEX IF NOT EXISTS idx_checklists_ns_partner ON checklists (namespace_id, partner_scope_id) WHERE partner_scope_id IS NOT NULL;

ALTER TABLE checklists ENABLE ROW LEVEL SECURITY;
ALTER TABLE checklists FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON checklists;
CREATE POLICY tenant_isolation_policy ON checklists
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE checklists FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE checklists TO nce_app;
    END IF;
END $$;


-- ============================================================================
-- 3. time_entries
-- ============================================================================

CREATE TABLE IF NOT EXISTS time_entries (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    time_entry_id          TEXT        NOT NULL,
    work_order_id          TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    partner_scope_id       UUID,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at               TIMESTAMPTZ,
    source                 TEXT        NOT NULL DEFAULT 'manual',
    approved               BOOLEAN     NOT NULL DEFAULT FALSE,
    op_id                  TEXT,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT time_entries_id_ns_unique UNIQUE (time_entry_id, namespace_id),
    CONSTRAINT time_entries_op_id_unique UNIQUE (op_id, namespace_id),
    CONSTRAINT time_entries_source_check CHECK (source IN ('gps', 'manual')),
    CONSTRAINT fk_time_entries_work_orders FOREIGN KEY (work_order_id, namespace_id)
        REFERENCES work_orders (work_order_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_time_entries_ns_wo ON time_entries (namespace_id, work_order_id);
CREATE INDEX IF NOT EXISTS idx_time_entries_ns_approved ON time_entries (namespace_id, approved);
CREATE INDEX IF NOT EXISTS idx_time_entries_ns_partner ON time_entries (namespace_id, partner_scope_id) WHERE partner_scope_id IS NOT NULL;

ALTER TABLE time_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE time_entries FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON time_entries;
CREATE POLICY tenant_isolation_policy ON time_entries
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE time_entries FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE time_entries TO nce_app;
    END IF;
END $$;

-- ============================================================================
-- HR Engine (Module 13, Wave 1 -- hr-schema):
-- Tables backing nce/vertical_modules/hr/**:
--   1. employees (native employee profile card & identity)
--   2. skills (employee-skill relations & assessment levels)
--   3. certifications (cert lifecycle, authority & expiry tracking for Watcher)
--   4. absences (sensitive leave/sick records & Norwegian compliance state)
-- ============================================================================

CREATE TABLE IF NOT EXISTS employees (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name                   TEXT        NOT NULL,
    email                  TEXT,
    role                   TEXT        NOT NULL DEFAULT 'technician',
    department             TEXT        NOT NULL DEFAULT 'operations',
    location_id            TEXT,
    leave_balance          NUMERIC     NOT NULL DEFAULT 25.0,
    active                 BOOLEAN     NOT NULL DEFAULT true,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT employees_id_ns_unique UNIQUE (employee_id, namespace_id)
);

CREATE INDEX IF NOT EXISTS idx_employees_ns_active ON employees (namespace_id, active);
CREATE INDEX IF NOT EXISTS idx_employees_ns_dept ON employees (namespace_id, department);
CREATE INDEX IF NOT EXISTS idx_employees_ns_role ON employees (namespace_id, role);
CREATE INDEX IF NOT EXISTS idx_employees_ns_location ON employees (namespace_id, location_id) WHERE location_id IS NOT NULL;

ALTER TABLE employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE employees FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON employees;
CREATE POLICY tenant_isolation_policy ON employees
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE employees FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE employees TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS skills (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    skill_id               TEXT        NOT NULL,
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    name                   TEXT        NOT NULL,
    category               TEXT        NOT NULL DEFAULT 'general',
    level                  TEXT        NOT NULL DEFAULT 'intermediate',
    assessed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT skills_emp_skill_ns_unique UNIQUE (employee_id, skill_id, namespace_id),
    CONSTRAINT fk_skills_employees FOREIGN KEY (employee_id, namespace_id)
        REFERENCES employees (employee_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_skills_ns_emp ON skills (namespace_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_skills_ns_name ON skills (namespace_id, name);

ALTER TABLE skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON skills;
CREATE POLICY tenant_isolation_policy ON skills
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE skills FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE skills TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS certifications (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    cert_id                TEXT        NOT NULL,
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    authority              TEXT        NOT NULL,
    name                   TEXT        NOT NULL,
    issued                 TIMESTAMPTZ NOT NULL,
    valid_to               TIMESTAMPTZ,
    status                 TEXT        NOT NULL DEFAULT 'active',
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT certs_id_ns_unique UNIQUE (cert_id, namespace_id),
    CONSTRAINT fk_certs_employees FOREIGN KEY (employee_id, namespace_id)
        REFERENCES employees (employee_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_certs_ns_emp ON certifications (namespace_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_certs_ns_valid_to ON certifications (namespace_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_certs_ns_status ON certifications (namespace_id, status);

ALTER TABLE certifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE certifications FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON certifications;
CREATE POLICY tenant_isolation_policy ON certifications
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE certifications FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE certifications TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS absences (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    absence_id             TEXT        NOT NULL,
    employee_id            TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    type                   TEXT        NOT NULL DEFAULT 'sick',
    start_date             TIMESTAMPTZ NOT NULL,
    end_date               TIMESTAMPTZ,
    days                   NUMERIC     NOT NULL DEFAULT 1.0,
    reason                 TEXT,
    status                 TEXT        NOT NULL DEFAULT 'pending',
    compliance_state       TEXT        NOT NULL DEFAULT 'normal',
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hr_source_id           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT absences_id_ns_unique UNIQUE (absence_id, namespace_id),
    CONSTRAINT fk_absences_employees FOREIGN KEY (employee_id, namespace_id)
        REFERENCES employees (employee_id, namespace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_absences_ns_emp ON absences (namespace_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_absences_ns_start ON absences (namespace_id, start_date);
CREATE INDEX IF NOT EXISTS idx_absences_ns_type ON absences (namespace_id, type);

ALTER TABLE absences ENABLE ROW LEVEL SECURITY;
ALTER TABLE absences FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON absences;
CREATE POLICY tenant_isolation_policy ON absences
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE absences FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE absences TO nce_app;
    END IF;
END $$;

-- ============================================================================
-- Marketing Engine (Module 14, Wave 1 -- marketing-schema):
-- Tables backing nce/vertical_modules/marketing/**:
--   1. case_studies (drafted, approved, and published customer success stories)
--   2. testimonials (quotes with high-NPS capture, structured consent tiers & scopes)
--   3. content_assets (marketing assets, AEO/GEO metadata, JSON-LD schemas, MinIO storage)
-- ============================================================================

CREATE TABLE IF NOT EXISTS case_studies (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    project_id             TEXT        NOT NULL,
    title                  TEXT        NOT NULL,
    body                   TEXT        NOT NULL DEFAULT '',
    status                 TEXT        NOT NULL DEFAULT 'draft',
    anonymized             BOOLEAN     NOT NULL DEFAULT TRUE,
    approver               TEXT,
    approved_at            TIMESTAMPTZ,
    marketing_source_id    TEXT,
    raw                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT case_studies_title_not_blank
        CHECK (btrim(title) <> ''),
    CONSTRAINT case_studies_status_check
        CHECK (status IN ('draft', 'in_review', 'approved', 'published', 'retracted'))
);

CREATE INDEX IF NOT EXISTS idx_case_studies_ns_status
    ON case_studies (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_case_studies_ns_project
    ON case_studies (namespace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_case_studies_source_id
    ON case_studies (marketing_source_id) WHERE marketing_source_id IS NOT NULL;

ALTER TABLE case_studies ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_studies FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON case_studies;
CREATE POLICY tenant_isolation_policy ON case_studies
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE case_studies FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE case_studies TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS testimonials (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_id            TEXT        NOT NULL,
    project_id             TEXT,
    quote                  TEXT        NOT NULL DEFAULT '',
    status                 TEXT        NOT NULL DEFAULT 'requested',
    consent                BOOLEAN     NOT NULL DEFAULT FALSE,
    consent_tier           TEXT        NOT NULL DEFAULT 'web_retractable',
    consent_scope          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    consent_recorded_at    TIMESTAMPTZ,
    nps_at_capture         NUMERIC(4, 2),
    marketing_source_id    TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT testimonials_status_check
        CHECK (status IN ('requested', 'received', 'approved', 'declined', 'retracted')),
    CONSTRAINT testimonials_consent_tier_check
        CHECK (consent_tier IN ('none', 'web_retractable', 'ai_citable_irrevocable'))
);

CREATE INDEX IF NOT EXISTS idx_testimonials_ns_status
    ON testimonials (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_testimonials_ns_customer
    ON testimonials (namespace_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_testimonials_source_id
    ON testimonials (marketing_source_id) WHERE marketing_source_id IS NOT NULL;

ALTER TABLE testimonials ENABLE ROW LEVEL SECURITY;
ALTER TABLE testimonials FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON testimonials;
CREATE POLICY tenant_isolation_policy ON testimonials
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE testimonials FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE testimonials TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS content_assets (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    kind                   TEXT        NOT NULL DEFAULT 'case_study',
    ref_id                 TEXT,
    title                  TEXT        NOT NULL DEFAULT '',
    seo                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    storage_uri            TEXT,
    status                 TEXT        NOT NULL DEFAULT 'draft',
    marketing_source_id    TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT content_assets_kind_check
        CHECK (kind IN ('case_study', 'testimonial', 'blog', 'brand', 'drip')),
    CONSTRAINT content_assets_status_check
        CHECK (status IN ('draft', 'approved', 'published', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_content_assets_ns_kind
    ON content_assets (namespace_id, kind);
CREATE INDEX IF NOT EXISTS idx_content_assets_ns_status
    ON content_assets (namespace_id, status);
CREATE INDEX IF NOT EXISTS idx_content_assets_source_id
    ON content_assets (marketing_source_id) WHERE marketing_source_id IS NOT NULL;

ALTER TABLE content_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_assets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON content_assets;
CREATE POLICY tenant_isolation_policy ON content_assets
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE content_assets FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE content_assets TO nce_app;
    END IF;
END $$;

-- Staff & Resources Engine (Module 15, Phase 1 -- resources-registry)
CREATE TABLE IF NOT EXISTS resources (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    kind                   TEXT        NOT NULL,
    ref_id                 TEXT,
    display_name           TEXT        NOT NULL,
    attrs                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT resources_kind_check
        CHECK (kind IN ('employee', 'contractor', 'vehicle', 'tool')),
    CONSTRAINT resources_display_name_not_blank
        CHECK (btrim(display_name) <> '')
);

CREATE INDEX IF NOT EXISTS idx_resources_ns_kind
    ON resources (namespace_id, kind);
CREATE INDEX IF NOT EXISTS idx_resources_ns_ref
    ON resources (namespace_id, ref_id) WHERE ref_id IS NOT NULL;

ALTER TABLE resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE resources FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON resources;
CREATE POLICY tenant_isolation_policy ON resources
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE resources FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE resources TO nce_app;
    END IF;
END $$;
-- ============================================================================
-- Module 15: Staff & Resources Engine - Allocations, Travel Legs, Stays, Per Diems
-- ============================================================================

CREATE TABLE IF NOT EXISTS allocations (
    id                     UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id           UUID             NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    resource_id            UUID             NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    demand_kind            VARCHAR(64)      NOT NULL,
    demand_id              UUID,
    functional_location_id UUID,
    starts_at              TIMESTAMPTZ      NOT NULL,
    ends_at                TIMESTAMPTZ      NOT NULL,
    status                 VARCHAR(32)      NOT NULL DEFAULT 'reserved',
    confidence             DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    attrs                  JSONB            NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT check_allocation_dates CHECK (ends_at > starts_at),
    CONSTRAINT exclude_resource_double_booking EXCLUDE USING gist (
        resource_id WITH =,
        tstzrange(starts_at, ends_at) WITH &&
    ) WHERE (status <> 'released')
);

CREATE INDEX IF NOT EXISTS idx_allocations_tenant_res
    ON allocations (namespace_id, resource_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_allocations_tenant_demand
    ON allocations (namespace_id, demand_kind, demand_id);

ALTER TABLE allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE allocations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON allocations;
CREATE POLICY tenant_isolation_policy ON allocations
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE allocations FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE allocations TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS travel_legs (
    id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    allocation_id  UUID          NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    origin         VARCHAR(255)  NOT NULL,
    destination    VARCHAR(255)  NOT NULL,
    departure_at   TIMESTAMPTZ   NOT NULL,
    arrival_at     TIMESTAMPTZ,
    mode           VARCHAR(64)   NOT NULL DEFAULT 'flight',
    cost_nok       NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    booking_ref    VARCHAR(128),
    status         VARCHAR(32)   NOT NULL DEFAULT 'planned',
    attrs          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_travel_legs_tenant_alloc
    ON travel_legs (namespace_id, allocation_id);

ALTER TABLE travel_legs ENABLE ROW LEVEL SECURITY;
ALTER TABLE travel_legs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON travel_legs;
CREATE POLICY tenant_isolation_policy ON travel_legs
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE travel_legs FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE travel_legs TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS stays (
    id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    allocation_id  UUID          NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    location       VARCHAR(255)  NOT NULL,
    check_in       TIMESTAMPTZ   NOT NULL,
    check_out      TIMESTAMPTZ   NOT NULL,
    cost_nok       NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    booking_ref    VARCHAR(128),
    status         VARCHAR(32)   NOT NULL DEFAULT 'planned',
    attrs          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT check_stay_dates CHECK (check_out > check_in)
);

CREATE INDEX IF NOT EXISTS idx_stays_tenant_alloc
    ON stays (namespace_id, allocation_id);

ALTER TABLE stays ENABLE ROW LEVEL SECURITY;
ALTER TABLE stays FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON stays;
CREATE POLICY tenant_isolation_policy ON stays
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE stays FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE stays TO nce_app;
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS per_diems (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id    UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    allocation_id   UUID          NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    date            DATE          NOT NULL,
    rate_nok        NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    diet_type       VARCHAR(64)   NOT NULL DEFAULT 'statutory_overnight',
    meals_provided  JSONB         NOT NULL DEFAULT '{"breakfast": false, "lunch": false, "dinner": false}'::jsonb,
    attrs           JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_per_diems_tenant_alloc
    ON per_diems (namespace_id, allocation_id);

ALTER TABLE per_diems ENABLE ROW LEVEL SECURITY;
ALTER TABLE per_diems FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON per_diems;
CREATE POLICY tenant_isolation_policy ON per_diems
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE per_diems FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE per_diems TO nce_app;
    END IF;
END $$;

-- ============================================================================
-- Customer Portal Engine (Module 17, Phase 1 -- security spine):
-- Tables backing nce/vertical_modules/customer_portal/**
--   1. portal_users (customer login identities / sessions under customer principal)
--   2. portal_document_shares (scoped, expiring grants to FDV/as-built documents)
--   3. portal_service_requests (customer service intake before Support owns the ticket)
-- ============================================================================

-- 1. portal_users
CREATE TABLE IF NOT EXISTS portal_users (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    portal_user_id     TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_scope_id  UUID        NOT NULL,
    customer_id        TEXT        NOT NULL,
    email              TEXT,
    auth_provider      TEXT        NOT NULL DEFAULT 'magic_link',
    contact            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_login_at      TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT portal_users_id_ns_unique UNIQUE (portal_user_id, namespace_id),
    CONSTRAINT portal_users_auth_provider_check CHECK (auth_provider IN ('magic_link', 'bankid', 'mock'))
);

CREATE INDEX IF NOT EXISTS idx_portal_users_ns_scope ON portal_users (namespace_id, customer_scope_id);
CREATE INDEX IF NOT EXISTS idx_portal_users_ns_customer ON portal_users (namespace_id, customer_id);

ALTER TABLE portal_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_users FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON portal_users;
CREATE POLICY external_isolation_policy ON portal_users
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE portal_users FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE portal_users TO nce_app;
    END IF;
END $$;


-- 2. portal_document_shares
CREATE TABLE IF NOT EXISTS portal_document_shares (
    id                 UUID        NOT NULL DEFAULT gen_random_uuid(),
    share_id           TEXT        NOT NULL,
    namespace_id       UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_scope_id  UUID        NOT NULL,
    document_ref       TEXT        NOT NULL,
    document_kind      TEXT        NOT NULL DEFAULT 'fdv',
    title              TEXT        NOT NULL DEFAULT '',
    granted_by         TEXT        NOT NULL DEFAULT 'system',
    expires_at         TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT portal_document_shares_id_ns_unique UNIQUE (share_id, namespace_id),
    CONSTRAINT portal_document_shares_kind_check CHECK (document_kind IN ('fdv', 'as_built', 'sow', 'manual', 'drawing'))
);

CREATE INDEX IF NOT EXISTS idx_portal_document_shares_ns_scope ON portal_document_shares (namespace_id, customer_scope_id);

ALTER TABLE portal_document_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_document_shares FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON portal_document_shares;
CREATE POLICY external_isolation_policy ON portal_document_shares
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE portal_document_shares FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE portal_document_shares TO nce_app;
    END IF;
END $$;


-- 3. portal_service_requests
CREATE TABLE IF NOT EXISTS portal_service_requests (
    id                     UUID        NOT NULL DEFAULT gen_random_uuid(),
    request_id             TEXT        NOT NULL,
    namespace_id           UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    customer_scope_id      UUID        NOT NULL,
    room_id                TEXT        NOT NULL,
    payload                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status                 TEXT        NOT NULL DEFAULT 'received',
    handed_off_ticket_id   TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT portal_service_requests_id_ns_unique UNIQUE (request_id, namespace_id),
    CONSTRAINT portal_service_requests_status_check CHECK (status IN ('received', 'under_review', 'scheduled', 'resolved', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_portal_service_requests_ns_scope ON portal_service_requests (namespace_id, customer_scope_id);
CREATE INDEX IF NOT EXISTS idx_portal_service_requests_ns_room ON portal_service_requests (namespace_id, room_id);

ALTER TABLE portal_service_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE portal_service_requests FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS external_isolation_policy ON portal_service_requests;
CREATE POLICY external_isolation_policy ON portal_service_requests
    FOR ALL TO nce_app
    USING (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    )
    WITH CHECK (
        namespace_id IS NOT NULL
        AND namespace_id = get_nce_namespace()
        AND customer_scope_id IS NOT NULL
        AND customer_scope_id = get_nce_external_scope()
    );

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE portal_service_requests FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE portal_service_requests TO nce_app;
    END IF;
END $$;
