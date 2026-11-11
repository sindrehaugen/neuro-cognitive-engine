#!/usr/bin/env python3
"""Apply nce/schema.sql then nce/migrations/*.sql to the DB named by PG_DSN or DATABASE_URL.

Migration selection mirrors nce/orchestrator.py _apply_pg_migrations:
  - top-level *.sql files only (nce/migrations/optional/ is excluded)
  - applied in ascending lexical (numeric) order
  - citus migrations are skipped with a fallback topology_graph DDL when the
    citus extension is not available
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

_NCE_DIR = Path(__file__).resolve().parents[1] / "nce"
_SCHEMA_PATH = _NCE_DIR / "schema.sql"
_MIGRATIONS_DIR = _NCE_DIR / "migrations"


def _dsn() -> str:
    raw = (os.getenv("PG_DSN") or os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        print("PG_DSN or DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)
    return raw


_TOPOLOGY_GRAPH_FALLBACK_DDL = """
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
ALTER TABLE topology_graph FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS topology_graph_tenant_isolation ON topology_graph;
CREATE POLICY topology_graph_tenant_isolation ON topology_graph
    FOR ALL
    USING (namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON topology_graph TO nce_gc;
"""


async def _apply_migrations(conn: asyncpg.Connection, migrations_dir: Path) -> None:
    """Apply top-level *.sql migrations in lexical order, excluding optional/ subdir."""
    paths = await asyncio.to_thread(lambda: sorted(migrations_dir.glob("*.sql")))
    for path in paths:
        sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
        if "citus" in path.name:
            citus_available = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'citus')"
            )
            if not citus_available:
                print(
                    f"  [warn] citus unavailable — applying topology_graph fallback for {path.name}",
                    file=sys.stderr,
                )
                await conn.execute(_TOPOLOGY_GRAPH_FALLBACK_DDL)
                continue
        await conn.execute(sql)
        print(f"  Applied migration: {path.name}")


_SHARED_CORE_INTEGRATION_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS node_ownership_registry (
    id                    UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_type             TEXT        NOT NULL,
    transition            TEXT,
    owner_engine          TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT ck_node_ownership_owner_engine_nonempty CHECK (owner_engine <> '')
);
CREATE INDEX IF NOT EXISTS idx_node_ownership_registry_namespace_type_transition
    ON node_ownership_registry(namespace_id, node_type, transition);
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_ownership_registry_ns_type_transition
    ON node_ownership_registry (namespace_id, node_type, COALESCE(transition, ''));
ALTER TABLE node_ownership_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE node_ownership_registry FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_policy ON node_ownership_registry;
CREATE POLICY tenant_isolation_policy ON node_ownership_registry
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE node_ownership_registry TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE node_ownership_registry TO nce_gc;

CREATE TABLE IF NOT EXISTS entity_merge_queue (
    id                    UUID             NOT NULL DEFAULT gen_random_uuid(),
    namespace_id          UUID             NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    node_type             TEXT             NOT NULL,
    candidate_payload     JSONB            NOT NULL,
    target_node_id        UUID,
    score                 DOUBLE PRECISION NOT NULL,
    status                TEXT             NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at            TIMESTAMPTZ      NOT NULL DEFAULT now(),
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
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE entity_merge_queue TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE entity_merge_queue TO nce_gc;

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
ALTER TABLE source_mode_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_mode_config FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_policy ON source_mode_config;
CREATE POLICY tenant_isolation_policy ON source_mode_config
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_mode_config TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_mode_config TO nce_gc;

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
ALTER TABLE divergence_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE divergence_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_policy ON divergence_log;
CREATE POLICY tenant_isolation_policy ON divergence_log
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
GRANT SELECT, INSERT ON TABLE divergence_log TO nce_app;
GRANT SELECT, INSERT, DELETE ON TABLE divergence_log TO nce_gc;

CREATE TABLE IF NOT EXISTS decision_feedback (
    id            UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id  UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine        TEXT        NOT NULL,
    context_id    TEXT,
    proposal      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    decision      TEXT        NOT NULL,
    delta         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    actor         TEXT        NOT NULL DEFAULT 'human',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_decision_feedback_ns_engine
    ON decision_feedback (namespace_id, engine, created_at DESC);
ALTER TABLE decision_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_feedback FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_policy ON decision_feedback;
CREATE POLICY tenant_isolation_policy ON decision_feedback
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE decision_feedback TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE decision_feedback TO nce_gc;

CREATE TABLE IF NOT EXISTS source_mode_heartbeat (
    namespace_id     UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    engine           TEXT        NOT NULL,
    last_checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_count      BIGINT      NOT NULL DEFAULT 1,
    PRIMARY KEY (namespace_id, engine)
);
CREATE INDEX IF NOT EXISTS idx_source_mode_heartbeat_last_checked
    ON source_mode_heartbeat (namespace_id, engine, last_checked_at);
ALTER TABLE source_mode_heartbeat ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_mode_heartbeat FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_policy ON source_mode_heartbeat;
CREATE POLICY tenant_isolation_policy ON source_mode_heartbeat
    FOR ALL TO nce_app
    USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_mode_heartbeat TO nce_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_mode_heartbeat TO nce_gc;
"""


async def _main() -> None:
    schema_path = _SCHEMA_PATH
    migrations_dir = _MIGRATIONS_DIR

    conn = await asyncpg.connect(_dsn())
    try:
        schema_sql = await asyncio.to_thread(schema_path.read_text, encoding="utf-8")
        await conn.execute(schema_sql)
        print(f"Applied schema from {schema_path}")

        if await asyncio.to_thread(os.path.isdir, str(migrations_dir)):
            await _apply_migrations(conn, migrations_dir)
            print("Applied all migrations.")

        await conn.execute(_SHARED_CORE_INTEGRATION_TABLES_DDL)
        print("Applied Shared-Core C1/C5/C10 integration tables.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
