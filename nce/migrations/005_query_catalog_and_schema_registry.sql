-- Migration 005: Query template catalog (Phase 1) + graph schema registry (Phase 3)
--
-- vector(768) matches cfg.EMBEDDING.VECTOR_DIM default (config.py:164 → EMBEDDING_VECTOR_DIM=768).
-- If EMBEDDING_VECTOR_DIM is overridden before running this migration, update the
-- dimension literal below to match before applying.
--
-- Tables use public schema — no nce. prefix exists in this database.

-- ─── Phase 1: Intent-Based Query Template Catalog ────────────────────────────

CREATE TABLE IF NOT EXISTS query_templates (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                 TEXT NOT NULL,
    description          TEXT NOT NULL,
    description_embedding vector(768),
    tags                 TEXT[]   NOT NULL DEFAULT '{}',
    tools                TEXT[]   NOT NULL DEFAULT '{}',
    param_schema         JSONB    NOT NULL DEFAULT '{}',
    pipeline             JSONB    NOT NULL DEFAULT '[]',
    raw_template         TEXT,
    target_engine        VARCHAR(20) NOT NULL DEFAULT 'pipeline'
                             CHECK (target_engine IN ('postgres', 'mongodb', 'graph', 'pipeline')),
    namespace_id         UUID REFERENCES namespaces(id) ON DELETE CASCADE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (namespace_id, slug)
);

CREATE INDEX IF NOT EXISTS query_templates_tags_gin
    ON query_templates USING GIN(tags);

CREATE INDEX IF NOT EXISTS query_templates_embedding_hnsw
    ON query_templates USING hnsw (description_embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS query_templates_namespace
    ON query_templates(namespace_id)
    WHERE namespace_id IS NOT NULL;

-- RLS:
--   SELECT — global seeds (namespace_id IS NULL) visible to every tenant;
--             custom templates visible only within their own namespace.
--   INSERT/UPDATE/DELETE — nce_app may only touch rows in its own namespace.
--             WITH CHECK prevents nce_app from creating global (NULL) seeds;
--             seeding must go through a superuser / migration runner connection.
ALTER TABLE query_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_templates FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON query_templates;
CREATE POLICY tenant_isolation_policy ON query_templates
    FOR ALL TO nce_app
    USING  (namespace_id IS NULL OR namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE query_templates TO nce_app;
    END IF;
END $$;

-- ─── Phase 3: Write-Time Graph Schema Registry ───────────────────────────────
--
-- Stores vocabulary-level types only — never instance labels.
--   NODE entries: type_key = kg_nodes.entity_type  (e.g. "Person", "CONCEPT")
--   EDGE entries: type_key = kg_edges.predicate    (e.g. "AUTHORED", "ATTENDED")
--
-- Updated via ON CONFLICT upsert inside the Saga transaction that writes
-- kg_nodes / kg_edges — zero additional round trips.
-- describe_schema() reads are O(1) PK index lookups, not SELECT DISTINCT scans.

CREATE TABLE IF NOT EXISTS graph_schema_registry (
    namespace_id  UUID    NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    element_type  VARCHAR(10) NOT NULL CHECK (element_type IN ('NODE', 'EDGE')),
    type_key      TEXT    NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_id, element_type, type_key)
);

ALTER TABLE graph_schema_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE graph_schema_registry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON graph_schema_registry;
CREATE POLICY tenant_isolation_policy ON graph_schema_registry
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE graph_schema_registry TO nce_app;
    END IF;
END $$;
