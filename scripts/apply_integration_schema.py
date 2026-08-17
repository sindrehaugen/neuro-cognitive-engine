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
    for path in sorted(migrations_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
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


async def _main() -> None:
    nce_dir = Path(__file__).resolve().parents[1] / "nce"
    schema_path = nce_dir / "schema.sql"
    migrations_dir = nce_dir / "migrations"

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(schema_path.read_text(encoding="utf-8"))
        print(f"Applied schema from {schema_path}")

        if migrations_dir.is_dir():
            await _apply_migrations(conn, migrations_dir)
            print("Applied all migrations.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
