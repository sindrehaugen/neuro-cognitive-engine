"""Comprehensive test suite for C12 Resource Surface (Wave A-1).

Tests:
  1. Standard REST routes: list, get, create, patch, archive, restore, events, comments, tags, bulk.
  2. Optimistic concurrency control (409 on version mismatch via If-Match and expected_version).
  3. C3/C8 principal tier redaction (employee, contractor, external-customer).
  4. Multi-tenant RLS isolation & derived tenant_scope:
     - Tenant-scoped: cross-namespace read is not found; cross-namespace write fails.
     - Global-scoped: cross-namespace read succeeds; write is guarded.
     - Guard-the-guard: unlisted table raises ValueError at declaration time.
  5. MCP tool twins (list, get, upsert, archive) dispatchable from TOOL_REGISTRY.
  6. Standing positive control (U18): unregister dynamically removes routes and tools.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from nce import admin_state
from nce.resource_surface import (
    ResourceSpec,
    build_all_resource_routes,
    get_resource_spec,
    register_resource,
    unregister_resource,
)
from nce.resource_surface.mcp import build_mcp_tool_definitions, build_mcp_tool_specs
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.inventory.resources import (
    GOODS_RECEIPT_SPEC,
    INVENTORY_ITEM_SPEC,
    INVENTORY_RMA_SPEC,
    STOCK_LOCATION_SPEC,
)

_NS_A = str(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_NS_B = str(uuid.UUID("22222222-2222-2222-2222-222222222222"))


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    admin_state.engine = None
    # Re-register default inventory specs if any test unregistered them
    register_resource(STOCK_LOCATION_SPEC)
    register_resource(INVENTORY_ITEM_SPEC)
    register_resource(GOODS_RECEIPT_SPEC)
    register_resource(INVENTORY_RMA_SPEC)
    yield
    _clear_mem_store()
    register_resource(STOCK_LOCATION_SPEC)
    register_resource(INVENTORY_ITEM_SPEC)
    register_resource(GOODS_RECEIPT_SPEC)
    register_resource(INVENTORY_RMA_SPEC)


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec))
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 1. Standard REST Verbs
# ===========================================================================


def test_rest_create_and_get():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    # Create
    payload = {
        "namespace_id": _NS_A,
        "name": "Central Depot",
        "kind": "warehouse",
        "level": 1,
    }
    create_resp = client.post("/api/inventory/stock-locations", json=payload)
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert "id" in created
    item_id = created["id"]
    assert created["name"] == "Central Depot"

    # Get
    get_resp = client.get(f"/api/inventory/stock-locations/{item_id}?namespace_id={_NS_A}")
    assert get_resp.status_code == 200, get_resp.text
    fetched = get_resp.json()
    assert fetched["id"] == item_id
    assert fetched["name"] == "Central Depot"
    assert fetched["kind"] == "warehouse"


def test_rest_list_with_filters():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    # Insert two items
    client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Warehouse 1", "kind": "warehouse"},
    )
    client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Van 42", "kind": "van"},
    )

    # List all
    all_resp = client.get(f"/api/inventory/stock-locations?namespace_id={_NS_A}")
    assert all_resp.status_code == 200
    all_data = all_resp.json()
    assert all_data["total"] == 2
    assert len(all_data["items"]) == 2

    # List filtered by kind
    filtered_resp = client.get(
        f"/api/inventory/stock-locations?namespace_id={_NS_A}&kind=warehouse"
    )
    assert filtered_resp.status_code == 200
    filtered_data = filtered_resp.json()
    assert len(filtered_data["items"]) == 1
    assert filtered_data["items"][0]["kind"] == "warehouse"


def test_rest_patch_and_concurrency():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    # Create item
    res = client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Initial Name", "kind": "warehouse"},
    ).json()
    item_id = res["id"]
    initial_version = res["version"]

    # Concurrency conflict with bad If-Match
    bad_patch = client.patch(
        f"/api/inventory/stock-locations/{item_id}",
        json={"namespace_id": _NS_A, "name": "Updated Name"},
        headers={"If-Match": "wrong-version-string"},
    )
    assert bad_patch.status_code == 409
    err_body = bad_patch.json()
    assert (
        "concurrency" in err_body.get("error", "").lower()
        or "version" in err_body.get("error", "").lower()
    )
    assert "current_version" in err_body

    # Successful patch with correct version
    good_patch = client.patch(
        f"/api/inventory/stock-locations/{item_id}",
        json={"namespace_id": _NS_A, "name": "Updated Name"},
        headers={"If-Match": str(initial_version)},
    )
    assert good_patch.status_code == 200
    updated = good_patch.json()
    assert updated["name"] == "Updated Name"


def test_rest_archive_and_restore():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    res = client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Temp Bin", "kind": "bin"},
    ).json()
    item_id = res["id"]

    # Archive
    arc_resp = client.post(
        f"/api/inventory/stock-locations/{item_id}/archive",
        json={"namespace_id": _NS_A, "reason": "Decommissioned"},
    )
    assert arc_resp.status_code == 200
    assert arc_resp.json()["archived"] is True

    # List should hide archived items by default
    list_resp = client.get(f"/api/inventory/stock-locations?namespace_id={_NS_A}")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["items"]) == 0

    # Restore
    rest_resp = client.post(
        f"/api/inventory/stock-locations/{item_id}/restore",
        json={"namespace_id": _NS_A},
    )
    assert rest_resp.status_code == 200
    assert rest_resp.json()["archived"] is False

    # List shows it again
    list_again = client.get(f"/api/inventory/stock-locations?namespace_id={_NS_A}")
    assert len(list_again.json()["items"]) == 1


def test_rest_comments_and_tags():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    res = client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Shelf 3", "kind": "shelf"},
    ).json()
    item_id = res["id"]

    # Comments
    add_comm = client.post(
        f"/api/inventory/stock-locations/{item_id}/comments",
        json={"namespace_id": _NS_A, "body": "Inspection passed", "author": "agent-1"},
    )
    assert add_comm.status_code == 201
    list_comm = client.get(
        f"/api/inventory/stock-locations/{item_id}/comments?namespace_id={_NS_A}"
    )
    assert list_comm.status_code == 200
    comments = list_comm.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "Inspection passed"

    # Tags
    add_tag = client.post(
        f"/api/inventory/stock-locations/{item_id}/tags",
        json={"namespace_id": _NS_A, "tag": "hazardous"},
    )
    assert add_tag.status_code == 200
    assert "hazardous" in add_tag.json()["tags"]

    get_tags = client.get(f"/api/inventory/stock-locations/{item_id}/tags?namespace_id={_NS_A}")
    assert get_tags.status_code == 200
    assert "hazardous" in get_tags.json()["tags"]

    del_tag = client.delete(
        f"/api/inventory/stock-locations/{item_id}/tags/hazardous?namespace_id={_NS_A}"
    )
    assert del_tag.status_code == 200
    assert "hazardous" not in del_tag.json()["tags"]


def test_rest_bulk():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    bulk_payload = {
        "namespace_id": _NS_A,
        "items": [
            {"name": "Bulk Location 1", "kind": "bin"},
            {"name": "Bulk Location 2", "kind": "bin"},
            {"name": "Bulk Location 3", "kind": "bin"},
        ],
    }
    resp = client.post("/api/inventory/stock-locations/bulk", json=bulk_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["count"] == 3
    assert len(data["ids"]) == 3


# ===========================================================================
# 2. C3/C8 Principal Tier Redaction
# ===========================================================================


def test_principal_tier_redaction():
    client = _client_for_spec(INVENTORY_ITEM_SPEC)
    # Create item with multiple fields, including sensitive ones
    create_payload = {
        "namespace_id": _NS_A,
        "sku": "SKU-SENSITIVE-01",
        "location_id": "loc-1",
        "qty_on_hand": 100,
        "qty_reserved": 25,
        "qty_blocked": 5,
        "reorder_point": 10,
        "cost": 150.0,
        "margin": 45.0,
    }
    created = client.post("/api/inventory/inventory-items", json=create_payload).json()
    item_id = created["id"]

    # 1. Employee header: sees full data
    emp_resp = client.get(
        f"/api/inventory/inventory-items/{item_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "employee"},
    )
    assert emp_resp.status_code == 200
    emp_data = emp_resp.json()
    assert emp_data["qty_on_hand"] == 100
    assert emp_data["qty_reserved"] == 25
    assert emp_data["qty_blocked"] == 5

    # 2. Contractor header: sees allowlisted fields (includes qty_reserved, not qty_blocked)
    con_resp = client.get(
        f"/api/inventory/inventory-items/{item_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "contractor"},
    )
    assert con_resp.status_code == 200
    con_data = con_resp.json()
    assert con_data["sku"] == "SKU-SENSITIVE-01"
    assert con_data["qty_on_hand"] == 100
    assert con_data["qty_reserved"] == 25
    assert "qty_blocked" not in con_data
    assert "reorder_point" not in con_data

    # 3. External-customer header: stripped of reserved/blocked and sensitive cost/margin words
    cust_resp = client.get(
        f"/api/inventory/inventory-items/{item_id}?namespace_id={_NS_A}",
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert cust_resp.status_code == 200
    cust_data = cust_resp.json()
    assert cust_data["sku"] == "SKU-SENSITIVE-01"
    assert cust_data["qty_on_hand"] == 100
    assert "qty_reserved" not in cust_data
    assert "qty_blocked" not in cust_data
    assert "cost" not in cust_data
    assert "margin" not in cust_data


# ===========================================================================
# 3. Tenancy & Negative RLS Tests
# ===========================================================================


def test_tenant_isolation_negative_rls():
    client = _client_for_spec(STOCK_LOCATION_SPEC)
    # Create in Namespace A
    created = client.post(
        "/api/inventory/stock-locations",
        json={"namespace_id": _NS_A, "name": "Tenant A Loc", "kind": "warehouse"},
    ).json()
    item_id = created["id"]

    # Read from Namespace B returns 404
    cross_get = client.get(f"/api/inventory/stock-locations/{item_id}?namespace_id={_NS_B}")
    assert cross_get.status_code == 404

    # List from Namespace B is empty
    cross_list = client.get(f"/api/inventory/stock-locations?namespace_id={_NS_B}")
    assert cross_list.status_code == 200
    assert len(cross_list.json()["items"]) == 0

    # Write from Namespace B to item owned by A fails
    cross_patch = client.patch(
        f"/api/inventory/stock-locations/{item_id}",
        json={"namespace_id": _NS_B, "name": "Hacked Loc"},
    )
    assert cross_patch.status_code == 404


def test_global_scope_derivation_and_contract():
    # Global table: product_catalog
    global_spec = ResourceSpec(
        engine="product",
        entity="global-parts",
        node_type="PRODUCT_SKU",
        table_name="product_catalog",
        writable_fields=("part_number", "title"),
    )
    assert global_spec.tenant_scope == "global"

    # Tenant table: stock_locations
    tenant_spec = ResourceSpec(
        engine="inventory",
        entity="test-stock",
        node_type="STOCK_LOCATION",
        table_name="stock_locations",
    )
    assert tenant_spec.tenant_scope == "tenant"
    assert tenant_spec.storage_kind == "postgres"

    # Mongo-backed type (e.g. VENDOR / CERT payload_ref)
    mongo_spec = ResourceSpec(
        engine="vendors",
        entity="vendor-profiles",
        node_type="VENDOR",
        storage_kind="mongo",
    )
    assert mongo_spec.tenant_scope == "tenant"
    assert mongo_spec.storage_kind == "mongo"

    # kg_nodes-only stub
    kg_spec = ResourceSpec(
        engine="project",
        entity="projects",
        node_type="PROJECT",
        storage_kind="kg_nodes",
    )
    assert kg_spec.tenant_scope == "graph"
    assert kg_spec.storage_kind == "kg_nodes"

    # Guard-the-guard: unlisted table raises ValueError at declaration
    with pytest.raises(ValueError) as exc_info:
        ResourceSpec(
            engine="inventory",
            entity="rogue-table",
            node_type="ROGUE_NODE",
            table_name="nonexistent_unregistered_table",
        )
    assert "neither EXPECTED_TENANT_RLS_TABLES nor EXPECTED_GLOBAL_TABLES" in str(exc_info.value)


def test_graph_scope_rejects_non_kg_nodes_filterable_field():
    """#311: a graph-primary spec naming a secondary-table column as
    filterable/searchable must fail at declaration, not at a caller's first
    live filter. Guard-the-guard for the kg_nodes-real-columns check,
    mirroring the unlisted-table-name check immediately above it."""
    with pytest.raises(ValueError) as exc_info:
        ResourceSpec(
            engine="system_design",
            entity="rogue-graph-resource",
            node_type="ROGUE_DEVICE",
            table_name=None,
            filterable_fields=("device_category",),
        )
    assert "not real kg_nodes columns" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        ResourceSpec(
            engine="system_design",
            entity="rogue-graph-resource-2",
            node_type="ROGUE_DEVICE_2",
            storage_kind="kg_nodes",
            searchable_fields=("model_number",),
        )
    assert "not real kg_nodes columns" in str(exc_info.value)

    # Positive: a graph-primary spec filtering only on a real kg_nodes
    # column (the CONTACT/#314 pattern) must still construct cleanly --
    # proves the check above is not simply refusing every graph spec.
    ok_spec = ResourceSpec(
        engine="system_design",
        entity="well-behaved-graph-resource",
        node_type="WELL_BEHAVED",
        table_name=None,
        filterable_fields=("change_origin",),
    )
    assert ok_spec.tenant_scope == "graph"


def test_global_scope_read_without_namespace_id():
    """Reads on global-scoped resources do not require namespace_id."""
    global_spec = ResourceSpec(
        engine="product",
        entity="global-read-parts",
        node_type="PRODUCT_SKU",
        table_name="product_catalog",
        writable_fields=("part_number", "title"),
    )
    client = _client_for_spec(global_spec)

    # Create an item
    create_resp = client.post(
        "/api/product/global-read-parts",
        json={"part_number": "PN-100", "title": "Capacitor 10uF"},
    )
    assert create_resp.status_code == 201, create_resp.text
    item = create_resp.json()
    item_id = item["id"]

    # Read without namespace_id query param or header
    get_resp = client.get(f"/api/product/global-read-parts/{item_id}")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == item_id
    assert get_resp.json()["part_number"] == "PN-100"

    # List without namespace_id query param or header
    list_resp = client.get("/api/product/global-read-parts")
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["total"] >= 1
    assert any(it["id"] == item_id for it in list_resp.json()["items"])


def test_global_scope_write_guards_external_customer():
    """Writes to global-scoped resources return 403 Forbidden for external-customer tier."""
    global_spec = ResourceSpec(
        engine="product",
        entity="global-write-parts",
        node_type="PRODUCT_SKU",
        table_name="product_catalog",
        writable_fields=("part_number", "title"),
    )
    client = _client_for_spec(global_spec)

    # 1. Create with employee succeeds
    create_ok = client.post(
        "/api/product/global-write-parts",
        json={"part_number": "PN-200", "title": "Resistor 10k"},
        headers={"X-NCE-Principal-Tier": "employee"},
    )
    assert create_ok.status_code == 201, create_ok.text
    item_id = create_ok.json()["id"]

    # 2. Create with external-customer returns 403
    create_forbidden = client.post(
        "/api/product/global-write-parts",
        json={"part_number": "PN-201", "title": "Forbidden Resistor"},
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert create_forbidden.status_code == 403, create_forbidden.text
    assert "External customers cannot modify global" in create_forbidden.json()["error"]

    # 3. Patch with external-customer returns 403
    patch_forbidden = client.patch(
        f"/api/product/global-write-parts/{item_id}",
        json={"title": "Hacked Title"},
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert patch_forbidden.status_code == 403, patch_forbidden.text

    # 4. Archive with external-customer returns 403
    archive_forbidden = client.post(
        f"/api/product/global-write-parts/{item_id}/archive",
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert archive_forbidden.status_code == 403, archive_forbidden.text

    # 5. Restore with external-customer returns 403
    restore_forbidden = client.post(
        f"/api/product/global-write-parts/{item_id}/restore",
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert restore_forbidden.status_code == 403, restore_forbidden.text

    # 6. Bulk with external-customer returns 403
    bulk_forbidden = client.post(
        "/api/product/global-write-parts/bulk",
        json={"operation": "create", "items": [{"part_number": "PN-202"}]},
        headers={"X-NCE-Principal-Tier": "external-customer"},
    )
    assert bulk_forbidden.status_code == 403, bulk_forbidden.text


def test_non_postgres_and_graph_dispatch_returns_501():
    """Live DB pool dispatch for storage_kind != 'postgres' or graph scope returns 501."""
    mongo_spec = ResourceSpec(
        engine="vendors",
        entity="vendor-profiles-501",
        node_type="VENDOR",
        storage_kind="mongo",
    )
    client = _client_for_spec(mongo_spec)

    # Set mock engine with pg_pool to simulate live DB path
    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()
    admin_state.engine = mock_engine

    try:
        resp = client.get(f"/api/vendors/vendor-profiles-501?namespace_id={_NS_A}")
        assert resp.status_code == 501, resp.text
        assert "mongo backend storage is not supported yet" in resp.json()["error"]
    finally:
        admin_state.engine = None


# ===========================================================================
# 4. MCP Twin Tools
# ===========================================================================


@pytest.mark.asyncio
async def test_mcp_tool_twins_execution():

    mock_engine = MagicMock()
    mock_engine.pg_pool = None  # Use in-memory store for unit execution

    # 1. Upsert tool creates an item
    upsert_fn = TOOL_REGISTRY["inventory_upsert_stock_locations"].handler
    upsert_args = {
        "namespace_id": _NS_A,
        "name": "MCP Warehouse",
        "kind": "warehouse",
        "level": "2",
    }
    upsert_raw = await upsert_fn(mock_engine, upsert_args)
    upsert_res = json.loads(upsert_raw)
    assert "id" in upsert_res
    item_id = upsert_res["id"]
    assert "version" in upsert_res

    # 2. Get tool fetches the item
    get_fn = TOOL_REGISTRY["inventory_get_stock_locations"].handler
    get_args = {"namespace_id": _NS_A, "id": item_id}
    get_raw = await get_fn(mock_engine, get_args)
    get_res = json.loads(get_raw)
    assert get_res["id"] == item_id
    assert get_res["name"] == "MCP Warehouse"

    # 3. List tool lists items
    list_fn = TOOL_REGISTRY["inventory_list_stock_locations"].handler
    list_args = {"namespace_id": _NS_A, "kind": "warehouse"}
    list_raw = await list_fn(mock_engine, list_args)
    list_res = json.loads(list_raw)
    assert list_res["total"] >= 1
    assert any(it["id"] == item_id for it in list_res["items"])

    # 4. Upsert tool with version mismatch fails concurrency check
    conflict_args = {
        "namespace_id": _NS_A,
        "id": item_id,
        "expected_version": "stale-version",
        "name": "Conflict Name",
    }
    conflict_raw = await upsert_fn(mock_engine, conflict_args)
    conflict_res = json.loads(conflict_raw)
    assert "error" in conflict_res
    assert conflict_res["status_code"] == 409

    # 5. Archive tool soft-archives the item
    archive_fn = TOOL_REGISTRY["inventory_archive_stock_locations"].handler
    archive_args = {"namespace_id": _NS_A, "id": item_id, "reason": "Testing archive"}
    archive_raw = await archive_fn(mock_engine, archive_args)
    archive_res = json.loads(archive_raw)
    assert archive_res["archived"] is True


# ===========================================================================
# 5. Standing Positive Control (U18)
# ===========================================================================


def test_positive_control_dynamic_unmount():
    """Prove that unregistering a spec dynamically removes its routes and tools."""
    assert get_resource_spec("inventory", "stock-locations") is not None

    # Unregister
    removed = unregister_resource("inventory", "stock-locations")
    assert removed is True
    assert get_resource_spec("inventory", "stock-locations") is None

    # Generated routes no longer contain stock-locations
    current_routes = build_all_resource_routes()
    paths = [r.path for r in current_routes]
    assert "/api/inventory/stock-locations" not in paths
    assert "/api/inventory/stock-locations/{id}" not in paths

    # Re-register restores capability
    register_resource(STOCK_LOCATION_SPEC)
    assert get_resource_spec("inventory", "stock-locations") is not None
    restored_routes = build_all_resource_routes()
    restored_paths = [r.path for r in restored_routes]
    assert "/api/inventory/stock-locations" in restored_paths


# ---------------------------------------------------------------------------
# excluded_verbs -- built for the RESOURCE (E-6) / FUNCTIONAL_LOCATION (C-1)
# tool-name-collision class, where a spec's entity happens to generate a
# verb's tool/route name that already exists as a hand-written tool. Proven
# by mutation (register/unregister a synthetic spec), not just read as
# correct: a positive-control probe checks the excluded verb is genuinely
# gone, not merely that the OTHER three still exist (which a no-op filter
# would also pass).
# ---------------------------------------------------------------------------


def test_excluded_verbs_rejects_unknown_name():
    with pytest.raises(ValueError, match="excluded_verbs"):
        ResourceSpec(
            engine="k_h_probe",
            entity="k-h-excluded-verbs-typo-probe",
            node_type="K_H_EXCLUDED_VERBS_TYPO_PROBE",
            storage_kind="kg_nodes",
            writable_fields=("label",),
            excluded_verbs=frozenset({"delete"}),  # not a real verb name
        )


def test_excluded_verbs_list_removes_list_route_and_tool_only():
    """Excluding 'list' removes exactly the list route/tool; get/upsert/archive
    and every sub-resource route are unaffected."""
    probe = ResourceSpec(
        engine="k_h_probe",
        entity="k-h-excluded-verbs-list-probe",
        node_type="K_H_EXCLUDED_VERBS_LIST_PROBE",
        storage_kind="kg_nodes",
        writable_fields=("label",),
        excluded_verbs=frozenset({"list"}),
    )

    routes = make_resource_routes(probe)
    paths_and_methods = {(r.path, m) for r in routes for m in r.methods}
    assert (probe.rest_collection_path, "GET") not in paths_and_methods, (
        "list route (GET on the collection path) must be excluded"
    )
    # get/upsert(create+patch+bulk)/archive+restore all still present.
    assert (probe.rest_item_path, "GET") in paths_and_methods
    assert (probe.rest_collection_path, "POST") in paths_and_methods
    assert (f"{probe.rest_collection_path}/bulk", "POST") in paths_and_methods
    assert (probe.rest_item_path, "PATCH") in paths_and_methods
    assert (f"{probe.rest_item_path}/archive", "POST") in paths_and_methods
    assert (f"{probe.rest_item_path}/restore", "POST") in paths_and_methods
    # Sub-resource routes are never affected by excluded_verbs.
    assert (f"{probe.rest_item_path}/events", "GET") in paths_and_methods
    assert (f"{probe.rest_item_path}/comments", "GET") in paths_and_methods

    tool_specs = build_mcp_tool_specs(probe)
    assert f"{probe.engine}_list_{probe.mcp_slug}" not in tool_specs
    assert f"{probe.engine}_get_{probe.mcp_slug}" in tool_specs
    assert f"{probe.engine}_upsert_{probe.mcp_slug}" in tool_specs
    assert f"{probe.engine}_archive_{probe.mcp_slug}" in tool_specs

    tool_defs = build_mcp_tool_definitions(probe)
    tool_names = {t.name for t in tool_defs}
    assert f"{probe.engine}_list_{probe.mcp_slug}" not in tool_names
    assert len(tool_defs) == 3


def test_excluded_verbs_empty_default_generates_all_four():
    """Positive control for the two tests above: the default (excluded_verbs
    empty) still generates all four MCP tools and the list route -- proves the
    exclusion logic actually branches on the field rather than always
    dropping 'list', which would make the test above pass for the wrong
    reason."""
    probe = ResourceSpec(
        engine="k_h_probe",
        entity="k-h-excluded-verbs-default-probe",
        node_type="K_H_EXCLUDED_VERBS_DEFAULT_PROBE",
        storage_kind="kg_nodes",
        writable_fields=("label",),
    )
    routes = make_resource_routes(probe)
    paths_and_methods = {(r.path, m) for r in routes for m in r.methods}
    assert (probe.rest_collection_path, "GET") in paths_and_methods

    tool_specs = build_mcp_tool_specs(probe)
    assert len(tool_specs) == 4
    assert f"{probe.engine}_list_{probe.mcp_slug}" in tool_specs
