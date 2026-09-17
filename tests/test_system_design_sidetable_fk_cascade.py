"""Wave SD-1: Enforce side-table -> kg_nodes FK and cascade (closing D12).

Tests for technical debt D12 closure across the three System Design side tables:
  1. system_design_device_capabilities (migration 039)
  2. system_design_geometry (migration 060)
  3. system_design_node_state (migration 061)

Every side-table row is bound by a foreign key to kg_nodes(label, namespace_id)
with ON DELETE CASCADE. Deleting a node from kg_nodes structurally cascades
deletions to all associated side-table rows, preventing orphan state inheritance
and resurrection.
"""

from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from nce.vertical_modules.system_design import geometry

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_079_PATH = REPO_ROOT / "nce" / "migrations" / "079_system_design_sidetable_fk_cascade.sql"
SCHEMA_PATH = REPO_ROOT / "nce" / "schema.sql"
DOCS_PATH = REPO_ROOT / "docs" / "database_architecture.md"

EXPECTED_FK_CONSTRAINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("system_design_device_capabilities", "fk_sddc_kg_nodes"),
        ("system_design_geometry", "fk_sdg_kg_nodes"),
        ("system_design_node_state", "fk_sdns_kg_nodes"),
    }
)


# ===========================================================================
# 1. Hermetic Contract & Static Integrity Tests (Run without Database)
# ===========================================================================


class TestSideTableFkStaticContracts:
    """Verifies DDL structure, naming, line endings, and doc registrations."""

    def test_migration_079_exists_and_pure_crlf(self) -> None:
        assert MIGRATION_079_PATH.exists(), f"Missing {MIGRATION_079_PATH}"
        raw = MIGRATION_079_PATH.read_bytes()
        assert b"\r\n" in raw, "Migration 079 must have CRLF"
        lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
        assert lone_lf == 0, f"Migration 079 contains {lone_lf} lone LF line endings"

    def test_migration_079_declares_all_three_named_fk_cascades(self) -> None:
        sql = MIGRATION_079_PATH.read_text(encoding="utf-8")
        for table, constraint in EXPECTED_FK_CONSTRAINTS:
            assert f"ADD CONSTRAINT {constraint}" in sql, (
                f"Missing ADD CONSTRAINT {constraint} in {MIGRATION_079_PATH.name}"
            )
            assert f"ALTER TABLE {table}" in sql, (
                f"Missing ALTER TABLE {table} in {MIGRATION_079_PATH.name}"
            )

        # Must enforce composite key to match kg_nodes(label, namespace_id)
        assert (
            sql.count(
                "FOREIGN KEY (node_label, namespace_id)\n"
                "            REFERENCES kg_nodes (label, namespace_id)\n"
                "            ON DELETE CASCADE"
            )
            == 3
            or sql.count("REFERENCES kg_nodes (label, namespace_id)\n            ON DELETE CASCADE")
            == 3
        )

    def test_migration_079_contains_pre_constraint_orphan_cleanup(self) -> None:
        sql = MIGRATION_079_PATH.read_text(encoding="utf-8")
        for table in (
            "system_design_device_capabilities",
            "system_design_geometry",
            "system_design_node_state",
        ):
            pattern = rf"DELETE FROM\s+{table}\s+\w+\s+WHERE NOT EXISTS"
            assert re.search(pattern, sql, re.IGNORECASE), (
                f"Missing pre-constraint orphan cleanup for {table}"
            )

    def test_schema_sql_contains_all_three_fk_constraints(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        for table, constraint in EXPECTED_FK_CONSTRAINTS:
            assert f"ADD CONSTRAINT {constraint}" in schema, (
                f"Missing constraint {constraint} in nce/schema.sql"
            )
            assert f"ALTER TABLE {table}" in schema, (
                f"Missing ALTER TABLE {table} in nce/schema.sql"
            )

    def test_docs_database_architecture_registers_migration_079(self) -> None:
        doc = DOCS_PATH.read_text(encoding="utf-8")
        assert "079_system_design_sidetable_fk_cascade.sql" in doc, (
            "docs/database_architecture.md does not register migration 079"
        )
        assert "fk_sddc_kg_nodes" in doc
        assert "fk_sdg_kg_nodes" in doc
        assert "fk_sdns_kg_nodes" in doc

    def test_geometry_bump_design_version_ensures_kg_nodes_entry(self) -> None:
        src = inspect.getsource(geometry.bump_design_version)
        assert "INSERT INTO kg_nodes" in src, (
            "bump_design_version must ensure DESIGN node is in kg_nodes"
        )
        assert "'DESIGN'" in src
        assert "ON CONFLICT (label, namespace_id) DO NOTHING" in src


# ===========================================================================
# 2. Database Integration Tests: Constraint Catalog & Cascade Verification
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestSideTableFkDatabaseCascade:
    """Exercises foreign key enforcement and ON DELETE CASCADE on a live database."""

    async def test_all_three_constraints_exist_in_pg_catalog_with_cascade(
        self, pg_pool: Any
    ) -> None:
        """Assert each constraint is a foreign key ('f') with confdeltype = 'c' (CASCADE)."""
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    t.relname AS table_name,
                    c.conname AS constraint_name,
                    c.contype AS constraint_type,
                    c.confdeltype::text AS delete_rule,
                    ft.relname AS foreign_table
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_class ft ON ft.oid = c.confrelid
                WHERE c.conname IN ('fk_sddc_kg_nodes', 'fk_sdg_kg_nodes', 'fk_sdns_kg_nodes')
                """
            )

        found = {r["constraint_name"]: dict(r) for r in rows}
        for table, constraint in EXPECTED_FK_CONSTRAINTS:
            assert constraint in found, f"Constraint {constraint} not found in pg_constraint"
            entry = found[constraint]
            assert entry["table_name"] == table
            assert entry["constraint_type"] == "f", "Must be foreign key ('f')"
            deltype = entry["delete_rule"]
            deltype = deltype.decode() if isinstance(deltype, (bytes, bytearray)) else str(deltype)
            assert deltype == "c", f"Constraint {constraint} delete rule must be 'c' (CASCADE)"
            assert entry["foreign_table"] == "kg_nodes"

    async def test_node_delete_cascades_to_all_three_side_tables(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Deleting a node from kg_nodes cascades deletions to all three side tables."""
        ns_id: uuid.UUID = await make_namespace()
        design_id = f"TEST-CASCADE-{uuid.uuid4().hex[:8].upper()}"
        node_lbl = f"DEVICE:{design_id}:MAIN-AMP"

        async with pg_pool.acquire() as conn:
            # 1. Insert parent node in kg_nodes
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, 'DEVICE', $2::uuid, 'sync')
                """,
                node_lbl,
                str(ns_id),
            )

            # 2. Insert row in system_design_device_capabilities
            await conn.execute(
                """
                INSERT INTO system_design_device_capabilities (
                    namespace_id, node_label, power_draw_watts, heat_btu_hr
                ) VALUES ($1::uuid, $2, 350.0, 1194.0)
                """,
                str(ns_id),
                node_lbl,
            )

            # 3. Insert row in system_design_geometry
            await conn.execute(
                """
                INSERT INTO system_design_geometry (
                    namespace_id, node_label, x, y, rack_position
                ) VALUES ($1::uuid, $2, 10.0, 20.0, 4.0)
                """,
                str(ns_id),
                node_lbl,
            )

            # 4. Insert row in system_design_node_state
            await conn.execute(
                """
                INSERT INTO system_design_node_state (
                    namespace_id, node_label, node_type, status
                ) VALUES ($1::uuid, $2, 'DEVICE', 'planned')
                """,
                str(ns_id),
                node_lbl,
            )

            # Verify all 3 side-table rows exist
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM system_design_device_capabilities WHERE namespace_id = $1::uuid AND node_label = $2",
                    str(ns_id),
                    node_lbl,
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM system_design_geometry WHERE namespace_id = $1::uuid AND node_label = $2",
                    str(ns_id),
                    node_lbl,
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM system_design_node_state WHERE namespace_id = $1::uuid AND node_label = $2",
                    str(ns_id),
                    node_lbl,
                )
                == 1
            )

            # 5. Delete the parent node from kg_nodes
            await conn.execute(
                "DELETE FROM kg_nodes WHERE namespace_id = $1::uuid AND label = $2",
                str(ns_id),
                node_lbl,
            )

            # 6. Structurally assert all 3 side-table rows were CASCADE-DELETED
            cap_count = await conn.fetchval(
                "SELECT count(*) FROM system_design_device_capabilities WHERE namespace_id = $1::uuid AND node_label = $2",
                str(ns_id),
                node_lbl,
            )
            geom_count = await conn.fetchval(
                "SELECT count(*) FROM system_design_geometry WHERE namespace_id = $1::uuid AND node_label = $2",
                str(ns_id),
                node_lbl,
            )
            state_count = await conn.fetchval(
                "SELECT count(*) FROM system_design_node_state WHERE namespace_id = $1::uuid AND node_label = $2",
                str(ns_id),
                node_lbl,
            )

            assert cap_count == 0, (
                "system_design_device_capabilities row survived parent node deletion (D12)"
            )
            assert geom_count == 0, "system_design_geometry row survived parent node deletion (D12)"
            assert state_count == 0, (
                "system_design_node_state row survived parent node deletion (D12)"
            )

    async def test_design_node_delete_cascades_to_design_version_row(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Deleting the DESIGN node from kg_nodes cascades and deletes its version row in geometry."""
        ns_id: uuid.UUID = await make_namespace()
        design_id = f"TEST-DSGN-{uuid.uuid4().hex[:8].upper()}"
        design_lbl = f"DESIGN:{design_id}"

        async with pg_pool.acquire() as conn:
            # 1. bump_design_version seeds DESIGN node in kg_nodes and version row in geometry
            ver = await geometry.bump_design_version(conn, ns_id, design_id, None)
            assert ver == 1

            # Assert both exist
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1::uuid AND label = $2",
                    str(ns_id),
                    design_lbl,
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM system_design_geometry WHERE namespace_id = $1::uuid AND node_label = $2 AND version IS NOT NULL",
                    str(ns_id),
                    design_lbl,
                )
                == 1
            )

            # 2. Delete the DESIGN node from kg_nodes
            await conn.execute(
                "DELETE FROM kg_nodes WHERE namespace_id = $1::uuid AND label = $2",
                str(ns_id),
                design_lbl,
            )

            # 3. Assert version row in system_design_geometry cascaded
            remaining = await conn.fetchval(
                "SELECT count(*) FROM system_design_geometry WHERE namespace_id = $1::uuid AND node_label = $2",
                str(ns_id),
                design_lbl,
            )
            assert remaining == 0, "Design version row survived DESIGN node deletion from kg_nodes"

    async def test_insert_side_table_row_without_kg_node_is_rejected(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Inserting into any side-table with a node_label absent from kg_nodes raises ForeignKeyViolationError."""
        ns_id: uuid.UUID = await make_namespace()
        non_existent_label = f"DEVICE:NONEXISTENT:{uuid.uuid4().hex[:8].upper()}"

        async with pg_pool.acquire() as conn:
            # Capabilities
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO system_design_device_capabilities (
                        namespace_id, node_label, power_draw_watts
                    ) VALUES ($1::uuid, $2, 100.0)
                    """,
                    str(ns_id),
                    non_existent_label,
                )

            # Geometry
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO system_design_geometry (
                        namespace_id, node_label, x, y
                    ) VALUES ($1::uuid, $2, 1.0, 2.0)
                    """,
                    str(ns_id),
                    non_existent_label,
                )

            # State
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO system_design_node_state (
                        namespace_id, node_label, node_type, status
                    ) VALUES ($1::uuid, $2, 'DEVICE', 'planned')
                    """,
                    str(ns_id),
                    non_existent_label,
                )

    async def test_composite_foreign_key_prevents_cross_tenant_references(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """Composite key (node_label, namespace_id) prevents tenant B from referencing tenant A's node."""
        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        node_lbl = f"DEVICE:CROSS-TENANT:{uuid.uuid4().hex[:8].upper()}"

        async with pg_pool.acquire() as conn:
            # Node exists only in tenant A
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, 'DEVICE', $2::uuid, 'sync')
                """,
                node_lbl,
                str(ns_a),
            )

            # Tenant B attempts to insert side-table row pointing to node_lbl under ns_b
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO system_design_device_capabilities (
                        namespace_id, node_label, power_draw_watts
                    ) VALUES ($1::uuid, $2, 100.0)
                    """,
                    str(ns_b),
                    node_lbl,
                )
