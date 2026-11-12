"""Unit regression tests for v3.4.3 Pure-Core RLS decoupling & namespace plugin hooks.

Covers Wave 1 Items #1 and #9:
  1. ``EXPECTED_TENANT_RLS_TABLES`` (35) + ``EXPECTED_SPECIAL_RLS_TABLES`` (1) equal
     the exact 36 Pure-Core forced-RLS tables (``schema.sql`` + Core migrations
     ``001``..``036``), ``EXPECTED_GLOBAL_TABLES`` equals the exact 5 Pure-Core
     global tables, and zero ``037+`` vertical tables remain in Core's static catalog
     or ``scripts/apply_integration_schema.py``.
  2. ``PluginCapabilities``, ``get_all_plugin_tenant_rls_tables()`` (explicit
     active-plugin declarations trusted over SQL scanning; fallback SQL scanner
     subtracts ``ALTER TABLE ... DISABLE ROW LEVEL SECURITY``),
     ``get_all_plugin_global_tables()``, ``ResourceSpec`` / ``SecondaryTable``
     plugin global & tenant resolution, and ``verify_rls_catalog_consistency()``.
  3. ``NamespaceOrchestrator._create_namespace()`` succeeds cleanly with zero
     ``node_ownership_registry`` table and invokes ``on_namespace_created`` plugin
     lifecycle hooks when plugins are registered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

import nce.plugins as plugins_mod
from nce.event_log import (
    EXPECTED_GLOBAL_TABLES,
    EXPECTED_SPECIAL_RLS_TABLES,
    EXPECTED_TENANT_RLS_TABLES,
    verify_rls_catalog_consistency,
)
from nce.models import (
    ManageNamespaceCommand,
    ManageNamespaceRequest,
    NamespaceCreate,
    NamespaceMetadata,
)
from nce.plugins import (
    NCEPlugin,
    PluginCapabilities,
    get_all_plugin_global_tables,
    get_all_plugin_tenant_rls_tables,
    get_all_plugin_unmanaged_pg_sites,
)
from nce.resource_surface.spec import ResourceSpec, SecondaryTable

REPO_ROOT = Path(__file__).resolve().parents[2]

PURE_CORE_NAMESPACE_ID_TABLES: frozenset[str] = frozenset(
    {
        "memories",
        "kg_nodes",
        "kg_edges",
        "pii_redactions",
        "memory_salience",
        "contradictions",
        "snapshots",
        "event_log",
        "resource_quotas",
        "outbox_events",
        "saga_execution_log",
        "consolidation_runs",
        "bridge_subscriptions",
        "dead_letter_queue",
        "embedding_migrations",
        "memory_embeddings",
        "embedding_aspects",
        "graph_schema_registry",
        "query_templates",
        "v3_cognitive_ledger",
        "topology_graph",
        "audit_log",
        "active_learning_queue",
        "processed_outbox_events",
        "actor_trust",
        "event_parents",
        "action_approval_queue",
        "action_idempotency",
        "signing_credentials",
        "principal_bindings",
        "outbound_webhooks",
        "mcp_client_tokens",
        "encryption_deks",
        "procedural_heuristics",
    }
)

PURE_CORE_GLOBAL_TABLES: frozenset[str] = frozenset(
    {
        "embedding_models",
        "kg_node_embeddings",
        "reembedding_runs",
        "event_sequences",
        "applied_migrations",
    }
)

SAMPLE_VERTICAL_TABLES: frozenset[str] = frozenset(
    {
        "node_ownership_registry",
        "entity_merge_queue",
        "source_mode_config",
        "divergence_log",
        "source_mode_heartbeat",
        "d365_integrations",
        "product_catalog",
        "geodata_osm_elements",
        "geodata_n50_land_cover",
        "geodata_place_names",
        "pricing_fx_rates",
        "odoo_integrations",
        "salesforce_integrations",
        "entra_id_configs",
        "portal_users",
        "service_tickets",
        "support_ticket_actions",
        "work_orders",
        "employees",
        "notifications",
        "procurement_po_lines",
        "business_insights_kpi_snapshots",
    }
)


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        return None


# ===========================================================================
# 1. Pure-Core RLS Catalog & Decoupling Invariants
# ===========================================================================


def test_pure_core_expected_rls_tables_exact_36_forced_and_5_global() -> None:
    """Core static RLS sets must contain strictly the 36 Pure-Core forced-RLS tables + 5 global."""
    expected_tenant = {tbl: "namespace_id" for tbl in PURE_CORE_NAMESPACE_ID_TABLES}
    expected_tenant["replay_runs"] = "source_namespace_id"

    assert len(PURE_CORE_NAMESPACE_ID_TABLES) == 34
    assert len(EXPECTED_TENANT_RLS_TABLES) == 35
    assert EXPECTED_TENANT_RLS_TABLES == expected_tenant

    assert EXPECTED_SPECIAL_RLS_TABLES == {
        "a2a_grants": ("owner_namespace_id", "target_namespace_id"),
    }
    assert set(EXPECTED_TENANT_RLS_TABLES).isdisjoint(EXPECTED_SPECIAL_RLS_TABLES)

    all_forced_core = set(EXPECTED_TENANT_RLS_TABLES) | set(EXPECTED_SPECIAL_RLS_TABLES)
    assert len(all_forced_core) == 36

    assert EXPECTED_GLOBAL_TABLES == PURE_CORE_GLOBAL_TABLES
    assert len(EXPECTED_GLOBAL_TABLES) == 5
    assert EXPECTED_GLOBAL_TABLES.isdisjoint(all_forced_core)

    # Zero 037+ vertical tables in Core static catalog sets.
    assert SAMPLE_VERTICAL_TABLES.isdisjoint(all_forced_core)
    assert SAMPLE_VERTICAL_TABLES.isdisjoint(EXPECTED_GLOBAL_TABLES)


def test_pure_core_integration_schema_and_namespace_orchestrator_have_zero_vertical_hooks() -> None:
    """scripts/apply_integration_schema.py and nce/orchestrators/namespace.py must be Pure Core."""
    schema_script = (REPO_ROOT / "scripts" / "apply_integration_schema.py").read_text(
        encoding="utf-8"
    )
    assert "_SHARED_CORE_INTEGRATION_TABLES_DDL" not in schema_script
    for vertical_tbl in (
        "node_ownership_registry",
        "entity_merge_queue",
        "source_mode_config",
        "divergence_log",
    ):
        assert vertical_tbl not in schema_script

    ns_orch_src = (REPO_ROOT / "nce" / "orchestrators" / "namespace.py").read_text(encoding="utf-8")
    assert "entity_resolution" not in ns_orch_src
    assert "seed_node_ownership_registry" not in ns_orch_src
    assert "run_plugin_namespace_created_hooks" in ns_orch_src


# ===========================================================================
# 2. PluginCapabilities, Dynamic RLS Discovery & SQL DISABLE Subtraction
# ===========================================================================


def test_plugin_capabilities_defaults_and_minimal_protocol_conformance() -> None:
    """PluginCapabilities defaults are empty/immutable and minimal NCEPlugin stays checkable."""
    caps = PluginCapabilities()
    assert caps.tenant_rls_tables == {}
    assert caps.global_tables == frozenset()
    assert caps.unmanaged_pg_sites == frozenset()

    class _MinimalPlugin:
        plugin_id = "minimal_test"

        def register_tools(self, registry: Any) -> None:
            pass

        def register_routes(self, app: Any) -> None:
            pass

        def register_workers(self, worker_registry: Any) -> None:
            pass

        def register_cognitive_hooks(self, cognitive_engine: Any) -> None:
            pass

        def get_migration_dir(self) -> Path | None:
            return None

    assert isinstance(_MinimalPlugin(), NCEPlugin)


def test_plugin_rls_and_global_discovery_capabilities_and_explicit_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit plugin tenant_rls_tables are trusted over scanning disabled SQL files in mdir."""
    mdir = tmp_path / "explicit_plugin_migrations"
    mdir.mkdir()
    # Disabled engine SQL file exists in the same migration dir, but plugin explicitly
    # declares only its active table set -> disabled_engine_table must NOT be returned.
    (mdir / "001_active.sql").write_text(
        "ALTER TABLE active_engine_items ENABLE ROW LEVEL SECURITY;",
        encoding="utf-8",
    )
    (mdir / "002_disabled.sql").write_text(
        "ALTER TABLE disabled_engine_items ENABLE ROW LEVEL SECURITY;",
        encoding="utf-8",
    )

    class _CapsAndTuplePlugin:
        plugin_id = "caps_plugin"

        def get_migration_dir(self) -> Path | None:
            return mdir

        def get_capabilities(self) -> PluginCapabilities:
            return PluginCapabilities(
                tenant_rls_tables={
                    "active_engine_items": "namespace_id",
                    "custom_scoped_runs": "owner_ns_id",
                },
                global_tables=frozenset({"plugin_global_catalog"}),
                unmanaged_pg_sites=frozenset({"ext_pkg/worker.py:poll_global"}),
            )

    class _CollectionPlugin:
        plugin_id = "tuple_plugin"

        def get_migration_dir(self) -> Path | None:
            return None

        def get_tenant_rls_tables(self) -> tuple[str, ...]:
            return ("tuple_declared_table",)

        def get_global_tables(self) -> set[str]:
            return {"tuple_global_lookup"}

    monkeypatch.setattr(
        plugins_mod,
        "_DISCOVERED_PLUGINS",
        {
            "caps_plugin": _CapsAndTuplePlugin(),  # type: ignore[dict-item]
            "tuple_plugin": _CollectionPlugin(),  # type: ignore[dict-item]
        },
    )

    tenant_tables = get_all_plugin_tenant_rls_tables()
    assert tenant_tables == {
        "active_engine_items": "namespace_id",
        "custom_scoped_runs": "owner_ns_id",
        "tuple_declared_table": "namespace_id",
    }
    assert "disabled_engine_items" not in tenant_tables

    global_tables = get_all_plugin_global_tables()
    assert global_tables == {"plugin_global_catalog", "tuple_global_lookup"}

    unmanaged_sites = get_all_plugin_unmanaged_pg_sites()
    assert unmanaged_sites == frozenset({"ext_pkg/worker.py:poll_global"})


def test_plugin_sql_fallback_scanner_subtracts_disabled_rls_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fallback SQL migration scan subtracts tables whose later migration disables RLS."""
    mdir = tmp_path / "sql_only_plugin_migrations"
    mdir.mkdir()
    (mdir / "009_initial_catalog.sql").write_text(
        """
        CREATE TABLE product_catalog (id uuid primary key, namespace_id uuid);
        ALTER TABLE product_catalog ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_orders ENABLE ROW LEVEL SECURITY;
        -- ALTER TABLE commented_out_table ENABLE ROW LEVEL SECURITY;
        /* ALTER TABLE block_commented_table ENABLE ROW LEVEL SECURITY; */
        """,
        encoding="utf-8",
    )
    (mdir / "038_catalog_global.sql").write_text(
        """
        ALTER TABLE IF EXISTS public.product_catalog DISABLE ROW LEVEL SECURITY;
        """,
        encoding="utf-8",
    )

    class _SqlOnlyPlugin:
        plugin_id = "sql_only_plugin"

        def get_migration_dir(self) -> Path | None:
            return mdir

    monkeypatch.setattr(
        plugins_mod,
        "_DISCOVERED_PLUGINS",
        {"sql_only_plugin": _SqlOnlyPlugin()},  # type: ignore[dict-item]
    )

    scanned = get_all_plugin_tenant_rls_tables()
    assert scanned == {"tenant_orders": "namespace_id"}
    assert "product_catalog" not in scanned
    assert "commented_out_table" not in scanned
    assert "block_commented_table" not in scanned


def test_resource_spec_and_secondary_table_resolve_plugin_global_and_tenant_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ResourceSpec & SecondaryTable accept plugin global and tenant tables and reject unknown."""

    class _SurfacePlugin:
        plugin_id = "surface_plugin"

        def get_migration_dir(self) -> Path | None:
            return None

        def get_capabilities(self) -> PluginCapabilities:
            return PluginCapabilities(
                tenant_rls_tables={"plugin_tenant_items": "namespace_id"},
                global_tables=frozenset({"product_catalog"}),
            )

    monkeypatch.setattr(
        plugins_mod,
        "_DISCOVERED_PLUGINS",
        {"surface_plugin": _SurfacePlugin()},  # type: ignore[dict-item]
    )

    global_spec = ResourceSpec(
        engine="product",
        entity="v343-catalog-probe",
        node_type="V343_CATALOG_PROBE",
        table_name="product_catalog",
        writable_fields=("part_number",),
    )
    assert global_spec.tenant_scope == "global"

    tenant_spec = ResourceSpec(
        engine="custom",
        entity="v343-tenant-probe",
        node_type="V343_TENANT_PROBE",
        table_name="plugin_tenant_items",
        writable_fields=("label",),
        secondary_tables=(
            SecondaryTable(
                table_name="product_catalog",
                join_field="id",
                fields=("sku",),
            ),
        ),
    )
    assert tenant_spec.tenant_scope == "tenant"

    with pytest.raises(ValueError, match="neither EXPECTED_TENANT_RLS_TABLES"):
        ResourceSpec(
            engine="custom",
            entity="v343-unregistered-probe",
            node_type="V343_UNREGISTERED_PROBE",
            table_name="completely_unregistered_table",
        )


# ===========================================================================
# 3. verify_rls_catalog_consistency() Pure-Core & Plugin Catalog Verification
# ===========================================================================


def _build_pure_core_catalog_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "table_name": "namespaces",
            "rls_enabled": False,
            "force_rls_enabled": False,
            "columns": ["id", "slug", "parent_id", "metadata", "created_at"],
            "policy_count": 0,
        }
    ]
    for tbl, col in EXPECTED_TENANT_RLS_TABLES.items():
        rows.append(
            {
                "table_name": tbl,
                "rls_enabled": True,
                "force_rls_enabled": True,
                "columns": ["id", col],
                "policy_count": 1,
            }
        )
    for tbl, cols in EXPECTED_SPECIAL_RLS_TABLES.items():
        rows.append(
            {
                "table_name": tbl,
                "rls_enabled": True,
                "force_rls_enabled": True,
                "columns": ["id", *cols],
                "policy_count": 1,
            }
        )
    for tbl in EXPECTED_GLOBAL_TABLES:
        rows.append(
            {
                "table_name": tbl,
                "rls_enabled": False,
                "force_rls_enabled": False,
                "columns": ["id"],
                "policy_count": 0,
            }
        )
    return rows


def _make_catalog_mock_conn(
    catalog_rows: list[dict[str, Any]],
    policy_map: dict[str, list[dict[str, str]]] | None = None,
) -> AsyncMock:
    conn = AsyncMock()

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM pg_class c" in sql:
            return catalog_rows
        if "FROM   pg_policies" in sql or "FROM pg_policies" in sql:
            tbl = args[0]
            if policy_map and tbl in policy_map:
                return policy_map[tbl]
            if tbl in EXPECTED_SPECIAL_RLS_TABLES:
                return [
                    {
                        "policyname": "a2a_isolation",
                        "qual": "(owner_namespace_id = get_nce_namespace() OR target_namespace_id = get_nce_namespace())",
                        "with_check": "(owner_namespace_id = get_nce_namespace())",
                    }
                ]
            col = EXPECTED_TENANT_RLS_TABLES.get(tbl, "namespace_id")
            return [
                {
                    "policyname": "tenant_isolation_policy",
                    "qual": f"({col} = get_nce_namespace())",
                    "with_check": f"({col} = get_nce_namespace())",
                }
            ]
        return []

    conn.fetch.side_effect = _fetch
    return conn


@pytest.mark.asyncio
async def test_verify_rls_catalog_consistency_pure_core_passes_and_enforces_replay_and_a2a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure-Core catalog passes with zero vertical tables and enforces replay_runs & a2a_grants."""
    monkeypatch.setattr(plugins_mod, "_DISCOVERED_PLUGINS", {})

    rows = _build_pure_core_catalog_rows()
    conn = _make_catalog_mock_conn(rows)
    await verify_rls_catalog_consistency(conn, require_plugin_tables=True)

    # Break replay_runs column -> fails closed with source_namespace_id missing.
    broken_replay = [
        {**r, "columns": ["id", "namespace_id"]} if r["table_name"] == "replay_runs" else r
        for r in rows
    ]
    with pytest.raises(RuntimeError, match="replay_runs: missing source_namespace_id column"):
        await verify_rls_catalog_consistency(_make_catalog_mock_conn(broken_replay))

    # Break a2a_grants force_rls_enabled -> fails closed.
    broken_a2a = [
        {**r, "force_rls_enabled": False} if r["table_name"] == "a2a_grants" else r for r in rows
    ]
    with pytest.raises(RuntimeError, match="a2a_grants: FORCE ROW LEVEL SECURITY is not enabled"):
        await verify_rls_catalog_consistency(_make_catalog_mock_conn(broken_a2a))


@pytest.mark.asyncio
async def test_verify_rls_catalog_consistency_merges_plugin_tenant_and_global_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin tenant and global tables are enforced when present and required when requested."""

    class _MockVerticalPlugin:
        plugin_id = "mock_vertical"

        def get_migration_dir(self) -> Path | None:
            return None

        def get_capabilities(self) -> PluginCapabilities:
            return PluginCapabilities(
                tenant_rls_tables={"ext_orders": "namespace_id"},
                global_tables=frozenset({"product_catalog"}),
            )

    monkeypatch.setattr(
        plugins_mod,
        "_DISCOVERED_PLUGINS",
        {"mock_vertical": _MockVerticalPlugin()},  # type: ignore[dict-item]
    )

    pure_rows = _build_pure_core_catalog_rows()
    # Optional plugin tables absent in Pure-Core DB: allowed when require_plugin_tables=False,
    # rejected when require_plugin_tables=True.
    await verify_rls_catalog_consistency(
        _make_catalog_mock_conn(pure_rows), require_plugin_tables=False
    )
    with pytest.raises(
        RuntimeError, match="ext_orders: expected tenant table does not exist in schema"
    ):
        await verify_rls_catalog_consistency(
            _make_catalog_mock_conn(pure_rows), require_plugin_tables=True
        )

    # Plugin global table accidentally having RLS enabled -> rejected.
    bad_global_rows = [
        *pure_rows,
        {
            "table_name": "ext_orders",
            "rls_enabled": True,
            "force_rls_enabled": True,
            "columns": ["id", "namespace_id"],
            "policy_count": 1,
        },
        {
            "table_name": "product_catalog",
            "rls_enabled": True,
            "force_rls_enabled": False,
            "columns": ["id", "namespace_id"],
            "policy_count": 1,
        },
    ]
    with pytest.raises(RuntimeError, match="product_catalog: declared global"):
        await verify_rls_catalog_consistency(
            _make_catalog_mock_conn(bad_global_rows), require_plugin_tables=True
        )


# ===========================================================================
# 4. NamespaceOrchestrator._create_namespace() & Lifecycle Hooks
# ===========================================================================


@pytest.mark.asyncio
async def test_create_namespace_succeeds_without_node_ownership_table_and_invokes_plugin_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_create_namespace succeeds with zero vertical tables and calls on_namespace_created."""
    ns_id = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    now = datetime.now(timezone.utc)
    executed_queries: list[str] = []

    conn = AsyncMock()

    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any]:
        executed_queries.append(sql)
        if "node_ownership_registry" in sql:
            raise AssertionError("Pure Core must never touch node_ownership_registry directly")
        return {
            "id": ns_id,
            "slug": args[0],
            "parent_id": args[1],
            "created_at": now,
            "metadata": {"temporal_retention_days": 90},
        }

    async def _execute(sql: str, *args: Any) -> str:
        executed_queries.append(sql)
        if "node_ownership_registry" in sql:
            raise AssertionError("Pure Core must never touch node_ownership_registry directly")
        return "OK"

    conn.fetchrow.side_effect = _fetchrow
    conn.execute.side_effect = _execute
    conn.transaction = MagicMock(return_value=_AsyncCtx(conn))

    import nce.orchestrators.namespace as ns_mod
    import nce.plugins as live_plugins_mod

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))

    orch = ns_mod.NamespaceOrchestrator(pool)
    payload = ManageNamespaceRequest(
        command=ManageNamespaceCommand.create,
        create=NamespaceCreate(
            slug="pure-core-ns",
            metadata=NamespaceMetadata(temporal_retention_days=90),
        ),
    )

    # Case 1: Zero plugins loaded -> succeeds cleanly with zero vertical SQL.
    monkeypatch.setattr(live_plugins_mod, "_DISCOVERED_PLUGINS", {})
    with (
        patch.object(ns_mod, "set_namespace_context", AsyncMock()) as mock_ctx,
        patch("nce.event_log.append_event", AsyncMock()) as mock_append,
    ):
        created = await orch.manage_namespace(payload, admin_identity="unit-admin")

    assert created["id"] == ns_id
    assert created["slug"] == "pure-core-ns"
    mock_ctx.assert_awaited_once_with(conn, ns_id)
    mock_append.assert_awaited_once()
    assert not any("node_ownership_registry" in q for q in executed_queries)

    # Case 2: Async and sync plugin hooks registered -> both invoked with (conn, ns_id).
    hook_calls: list[tuple[str, Any, UUID]] = []

    class _AsyncHookPlugin:
        plugin_id = "async_hook"

        async def on_namespace_created(self, hook_conn: Any, hook_ns_id: UUID) -> None:
            hook_calls.append(("async", hook_conn, hook_ns_id))

    class _SyncHookPlugin:
        plugin_id = "sync_hook"

        def on_namespace_created(self, hook_conn: Any, hook_ns_id: UUID) -> None:
            hook_calls.append(("sync", hook_conn, hook_ns_id))

    monkeypatch.setattr(
        live_plugins_mod,
        "_DISCOVERED_PLUGINS",
        {
            "async_hook": _AsyncHookPlugin(),  # type: ignore[dict-item]
            "sync_hook": _SyncHookPlugin(),  # type: ignore[dict-item]
        },
    )

    with (
        patch.object(ns_mod, "set_namespace_context", AsyncMock()),
        patch("nce.event_log.append_event", AsyncMock()),
    ):
        await orch.manage_namespace(payload, admin_identity="unit-admin")

    assert hook_calls == [
        ("async", conn, ns_id),
        ("sync", conn, ns_id),
    ]

    # Direct helper call with no hook method on plugin is a clean no-op.
    class _NoHookPlugin:
        plugin_id = "no_hook"

    monkeypatch.setattr(
        live_plugins_mod,
        "_DISCOVERED_PLUGINS",
        {"no_hook": _NoHookPlugin()},  # type: ignore[dict-item]
    )
    await live_plugins_mod.run_plugin_namespace_created_hooks(conn, uuid4())
