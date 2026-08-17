import asyncio
import os
from pathlib import Path

import asyncpg


async def print_policies(conn, step_name):
    print(f"\n--- Policies after: {step_name} ---")
    rows = await conn.fetch("""
        SELECT tablename, policyname, roles, qual
        FROM pg_policies
        WHERE schemaname = 'public'
        ORDER BY tablename, policyname
    """)
    for row in rows:
        print(
            f"  Table: {row['tablename']} | Policy: {row['policyname']} | Roles: {row['roles']} | Qual: {row['qual']}"
        )


async def _main() -> None:
    dsn = os.getenv("PG_DSN") or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta"
    conn = await asyncpg.connect(dsn)
    try:
        # Set session-level lock timeout to 10 seconds to prevent application starvation during lock contention
        print("Setting lock timeout to 10 seconds...")
        await conn.execute("SET lock_timeout = '10s';")

        async with conn.transaction():
            # Acquire transaction-level advisory lock to serialize concurrent migrations
            print("Acquiring transaction advisory lock (123456)...")
            await conn.execute("SELECT pg_advisory_xact_lock(123456);")

            # 1. Drop all policies
            policies = await conn.fetch(
                "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'"
            )
            for p in policies:
                await conn.execute(
                    f'DROP POLICY IF EXISTS "{p["policyname"]}" ON "{p["tablename"]}";'
                )
            print("Dropped all existing policies.")

            # 2. Drop legacy roles
            for role in ['trimcp_app', 'trimcp_gc']:
                role_exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)", role
                )
                if role_exists:
                    await conn.execute(f"REASSIGN OWNED BY {role} TO mcp_user;")
                    await conn.execute(f"DROP OWNED BY {role};")
                    await conn.execute(f"DROP ROLE IF EXISTS {role};")
            print("Dropped legacy roles.")

            await print_policies(conn, "Initial Drop")

            # 3. Apply schema.sql
            print("Applying schema.sql...")
            schema_path = Path("nce/schema.sql")
            await conn.execute(schema_path.read_text(encoding="utf-8"))
            await print_policies(conn, "schema.sql")

            # 4. Apply migrations in order
            migrations_dir = Path("nce/migrations")
            migration_files = sorted(migrations_dir.glob("*.sql"))
            for migration_file in migration_files:
                print(f"Applying migration: {migration_file.name}...")
                sql = migration_file.read_text(encoding="utf-8")
                try:
                    await conn.execute(sql)
                except Exception as e:
                    err_str = str(e).lower()
                    if "citus" in migration_file.name and (
                        "extension \"citus\" is not available" in err_str
                        or "extension" in err_str
                        and "citus" in err_str
                    ):
                        print("  (Citus missing - applying fallback topology SQL...)")
                        # Fallback topology SQL
                        await conn.execute("""
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
                        """)
                    else:
                        raise e
                await print_policies(conn, migration_file.name)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
