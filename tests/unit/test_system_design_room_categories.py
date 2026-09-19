"""tests/unit/test_system_design_room_categories.py

Unit test suite for System Design Wave C-2:
- Curated AV/engineering room categories as config-as-IP
- FUNCTIONAL_LOCATION room category attachment via kg_edges (has_category)
- Responsible personnel assignments via kg_edges (responsible_for)
- C16 Principal Mapping integration for my-responsible queries
- Public upsert_fl_path and upsert_fl_edge primitives
- Strict MCP error hierarchy and REST endpoint parity
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from starlette.requests import Request

from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.system_design import (
    api_system_design_assign_fl_responsible,
    api_system_design_get_fl_room_category,
    api_system_design_get_room_category,
    api_system_design_list_fl_responsible,
    api_system_design_list_room_categories,
    api_system_design_set_fl_room_category,
    api_system_design_unassign_fl_responsible,
)
from nce.orchestrator import NCEEngine
from nce.principal_bindings import PrincipalContext
from nce.vertical_modules.system_design.fl_tree import (
    _MEM_EDGES,
    _MEM_NODES,
    FLNodeNotFoundError,
    _clear_mem_store,
)
from nce.vertical_modules.system_design.graph import (
    fl_label,
    upsert_fl_edge,
    upsert_fl_path,
)
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_assign_fl_responsible,
    handle_system_design_get_fl_room_category,
    handle_system_design_get_room_category,
    handle_system_design_list_fl_responsible,
    handle_system_design_list_my_responsible_fls,
    handle_system_design_list_room_categories,
    handle_system_design_set_fl_room_category,
    handle_system_design_unassign_fl_responsible,
)
from nce.vertical_modules.system_design.room_categories import (
    ROOM_CATEGORIES,
    InvalidResponsibleRoleError,
    RoomCategoryNotFoundError,
    assign_fl_responsible,
    get_fl_room_category,
    get_room_category,
    list_fl_responsible,
    list_my_responsible_fls,
    list_room_categories,
    set_fl_room_category,
    unassign_fl_responsible,
)

# Removed global pytestmark to allow sync tests without PytestWarning


@pytest.fixture(autouse=True)
def cleanup_mem():
    """Reset in-memory storage before and after each test."""
    _clear_mem_store()
    yield
    _clear_mem_store()


@pytest.fixture
def mock_engine():
    """Create a mock NCEEngine with no live DB pool."""
    eng = MagicMock(spec=NCEEngine)
    eng.pg_pool = None
    admin_state.engine = eng
    return eng


# ---------------------------------------------------------------------------
# 1. Config-as-IP: Room Categories Definition & Pure Lookups
# ---------------------------------------------------------------------------


def test_room_categories_definitions():
    """Verify all 11 required room categories exist with required metadata."""
    expected = {
        "BOARDROOM",
        "CONFERENCE_LARGE",
        "CONFERENCE_MEDIUM",
        "MEETING_SMALL",
        "HUDDLE",
        "TRAINING_ROOM",
        "AUDITORIUM",
        "ALL_HANDS",
        "FLEX_SPACE",
        "WAR_ROOM",
        "OPERATIONS_CENTER",
    }
    assert set(ROOM_CATEGORIES.keys()) == expected

    for cat_id, cat in ROOM_CATEGORIES.items():
        assert cat["id"] == cat_id
        assert cat["name"]
        assert cat["description"]
        assert cat["capacity_min"] > 0
        assert cat["capacity_max"] >= cat["capacity_min"]

        # Acoustics metadata
        acoustics = cat["acoustics"]
        assert 0.0 < acoustics["target_rt60_seconds"] <= 2.0
        assert 15 <= acoustics["noise_criterion_nc"] <= 45
        assert acoustics["sound_isolation_stc"] >= 25

        # Video metadata
        video = cat["video"]
        assert video["display_type"]
        assert video["camera_type"]
        assert video["fov_degrees"] > 0

        # Audio metadata
        audio = cat["audio"]
        assert audio["mic_type"]
        assert isinstance(audio["aec_required"], bool)
        assert audio["speaker_type"]

        # Features
        assert isinstance(cat["typical_features"], list)
        assert len(cat["typical_features"]) > 0


def test_list_and_get_room_categories():
    """Verify filtering and retrieving room categories."""
    all_cats = list_room_categories()
    assert len(all_cats) == 11
    assert [c["id"] for c in all_cats] == sorted(ROOM_CATEGORIES.keys())

    # Filter by search string
    boardroom = list_room_categories(q="boardroom")
    assert len(boardroom) == 1
    assert boardroom[0]["id"] == "BOARDROOM"

    # Filter by capacity
    large_spaces = list_room_categories(min_capacity=50)
    assert any(c["id"] == "AUDITORIUM" for c in large_spaces)
    assert any(c["id"] == "ALL_HANDS" for c in large_spaces)
    assert not any(c["id"] == "HUDDLE" for c in large_spaces)

    small_spaces = list_room_categories(max_capacity=4)
    assert any(c["id"] == "HUDDLE" for c in small_spaces)
    assert not any(c["id"] == "AUDITORIUM" for c in small_spaces)

    # Direct lookup
    huddle = get_room_category("HUDDLE")
    assert huddle["name"] == "Huddle Space"

    # Case insensitivity
    huddle_lower = get_room_category("huddle")
    assert huddle_lower["id"] == "HUDDLE"

    # Not found raises RoomCategoryNotFoundError
    with pytest.raises(RoomCategoryNotFoundError):
        get_room_category("NON_EXISTENT_CATEGORY")


# ---------------------------------------------------------------------------
# 2. Graph Primitives: upsert_fl_path & upsert_fl_edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_fl_path_and_edge():
    """Verify public graph authoring primitives upsert_fl_path and upsert_fl_edge."""
    ns_id = uuid4()
    ns_str = str(ns_id)

    # Author a path
    label = await upsert_fl_path(
        None,
        ns_id,
        namespace_slug="corp",
        path_parts=["SiteA", "Bld1", "Fl2", "Conf301"],
        source_id="ext-import-123",
    )
    expected_label = "FL:CORP:SITEA:BLD1:FL2:CONF301"
    assert label == expected_label
    assert fl_label("corp", "SiteA", "Bld1", "Fl2", "Conf301") == expected_label

    # Verify node exists in memory store
    node = _MEM_NODES[ns_str][label]
    assert node["label"] == label
    assert node["entity_type"] == "FUNCTIONAL_LOCATION"
    assert node["system_design_source_id"] == "ext-import-123"

    # Author an edge
    parent_label = "FL:corp:SiteA:Bld1:Fl2"
    await upsert_fl_edge(
        None,
        ns_id,
        subject=parent_label,
        predicate="parent_of",
        obj=label,
        confidence=1.0,
        source_id="ext-import-edge-1",
    )

    edges = _MEM_EDGES[ns_str]
    assert len(edges) == 1
    assert edges[0]["subject_label"] == parent_label
    assert edges[0]["predicate"] == "parent_of"
    assert edges[0]["object_label"] == label


# ---------------------------------------------------------------------------
# 3. Room Category Attachment to Functional Location
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fl_room_category_attachment():
    """Test associating and retrieving room category on a functional location."""
    ns_id = uuid4()
    fl_lbl = await upsert_fl_path(
        None, ns_id, namespace_slug="test", path_parts=["HQ", "Floor1", "Room101"]
    )

    # Initial state: unassigned
    res = await get_fl_room_category(None, ns_id, fl_id_or_label=fl_lbl)
    assert res["category_id"] is None
    assert res["category"] is None

    # Set category
    set_res = await set_fl_room_category(
        None, ns_id, fl_id_or_label=fl_lbl, category_id="BOARDROOM", actor="architect@example.com"
    )
    assert set_res["status"] == "ok"
    assert set_res["category_id"] == "BOARDROOM"
    assert set_res["actor"] == "architect@example.com"

    # Retrieve category
    get_res = await get_fl_room_category(None, ns_id, fl_id_or_label=fl_lbl)
    assert get_res["category_id"] == "BOARDROOM"
    assert get_res["category"]["name"] == "Executive Boardroom"
    assert get_res["category"]["acoustics"]["target_rt60_seconds"] == 0.5

    # Re-assigning to a different category replaces the edge
    await set_fl_room_category(None, ns_id, fl_id_or_label=fl_lbl, category_id="HUDDLE")
    get_res2 = await get_fl_room_category(None, ns_id, fl_id_or_label=fl_lbl)
    assert get_res2["category_id"] == "HUDDLE"
    assert get_res2["category"]["name"] == "Huddle Space"

    # Category edge list should only contain one has_category edge
    cat_edges = [
        e
        for e in _MEM_EDGES.get(str(ns_id), [])
        if e.get("subject_label") == fl_lbl and e.get("predicate") == "has_category"
    ]
    assert len(cat_edges) == 1
    assert cat_edges[0]["object_label"] == "ROOM_CATEGORY:HUDDLE"

    # Invalid category raises RoomCategoryNotFoundError
    with pytest.raises(RoomCategoryNotFoundError):
        await set_fl_room_category(None, ns_id, fl_id_or_label=fl_lbl, category_id="INVALID_ROOM")

    # Missing node raises FLNodeNotFoundError
    with pytest.raises(FLNodeNotFoundError):
        await set_fl_room_category(
            None, ns_id, fl_id_or_label="FL:missing:node", category_id="BOARDROOM"
        )


# ---------------------------------------------------------------------------
# 4. Responsible Personnel Operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fl_responsible_assignment_lifecycle():
    """Test assigning, listing, and unassigning responsible employees on FL."""
    ns_id = uuid4()
    fl_lbl = await upsert_fl_path(
        None, ns_id, namespace_slug="test", path_parts=["HQ", "Auditorium"]
    )

    # Assign primary
    r1 = await assign_fl_responsible(
        None,
        ns_id,
        fl_id_or_label=fl_lbl,
        employee_id="EMP-101",
        role="primary",
        actor="admin",
    )
    assert r1["status"] == "ok"
    assert r1["employee_id"] == "EMP-101"
    assert r1["role"] == "primary"

    # Assign backup and lead technician
    await assign_fl_responsible(
        None, ns_id, fl_id_or_label=fl_lbl, employee_id="EMP-102", role="backup"
    )
    await assign_fl_responsible(
        None, ns_id, fl_id_or_label=fl_lbl, employee_id="EMP-103", role="lead_technician"
    )

    # List responsible
    resp_list = await list_fl_responsible(None, ns_id, fl_id_or_label=fl_lbl)
    assert len(resp_list) == 3
    emp_ids = {r["employee_id"]: r["role"] for r in resp_list}
    assert emp_ids == {
        "EMP-101": "primary",
        "EMP-102": "backup",
        "EMP-103": "lead_technician",
    }

    # Invalid role raises InvalidResponsibleRoleError
    with pytest.raises(InvalidResponsibleRoleError):
        await assign_fl_responsible(
            None, ns_id, fl_id_or_label=fl_lbl, employee_id="EMP-104", role="superhero"
        )

    # Empty employee ID raises ValueError
    with pytest.raises(ValueError):
        await assign_fl_responsible(
            None, ns_id, fl_id_or_label=fl_lbl, employee_id="   ", role="primary"
        )

    # Unassign employee
    un_res = await unassign_fl_responsible(
        None, ns_id, fl_id_or_label=fl_lbl, employee_id="EMP-101", actor="admin"
    )
    assert un_res["status"] == "ok"
    assert un_res["unassigned"] is True

    # Unassign non-existent employee returns unassigned=False
    un_res2 = await unassign_fl_responsible(
        None, ns_id, fl_id_or_label=fl_lbl, employee_id="EMP-999"
    )
    assert un_res2["unassigned"] is False

    # List after unassign
    resp_list_after = await list_fl_responsible(None, ns_id, fl_id_or_label=fl_lbl)
    assert len(resp_list_after) == 2
    assert {r["employee_id"] for r in resp_list_after} == {"EMP-102", "EMP-103"}


# ---------------------------------------------------------------------------
# 5. C16 Principal Mapping & my-responsible Queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c16_principal_mapping_responsible():
    """Verify list_my_responsible_fls contract under C16 Principal Mapping."""
    ns_id = uuid4()
    fl_1 = await upsert_fl_path(None, ns_id, namespace_slug="c16", path_parts=["Site", "RoomA"])
    fl_2 = await upsert_fl_path(None, ns_id, namespace_slug="c16", path_parts=["Site", "RoomB"])

    await assign_fl_responsible(
        None, ns_id, fl_id_or_label=fl_1, employee_id="EMP-CORP-42", role="primary"
    )
    await assign_fl_responsible(
        None, ns_id, fl_id_or_label=fl_2, employee_id="EMP-CORP-42", role="backup"
    )

    # 1. Bound employee caller
    emp_principal = PrincipalContext(
        principal_id="sub-1001",
        tier="employee",
        employee_id="EMP-CORP-42",
        roles={"technician"},
    )
    my_fls = await list_my_responsible_fls(None, ns_id, principal=emp_principal)
    assert len(my_fls) == 2
    fl_roles = {f["fl_label"]: f["role"] for f in my_fls}
    assert fl_roles == {fl_1: "primary", fl_2: "backup"}

    # 2. Customer tier caller (C16 security contract: MUST return [] without error)
    cust_principal = PrincipalContext(
        principal_id="sub-2002",
        tier="customer",
        customer_id="CUST-99",
        employee_id=None,
    )
    assert await list_my_responsible_fls(None, ns_id, principal=cust_principal) == []

    # 3. Anonymous / Unbound caller (MUST return [])
    anon_principal = PrincipalContext(
        principal_id="sub-3003",
        tier="anonymous",
        employee_id=None,
    )
    assert await list_my_responsible_fls(None, ns_id, principal=anon_principal) == []

    # 4. Explicit employee_id override
    direct_fls = await list_my_responsible_fls(None, ns_id, employee_id="EMP-CORP-42")
    assert len(direct_fls) == 2


@pytest.mark.asyncio
async def test_negative_rls_namespace_isolation():
    """Verify tenant isolation across responsible and room category edges."""
    ns_1 = uuid4()
    ns_2 = uuid4()

    fl_ns1 = await upsert_fl_path(None, ns_1, namespace_slug="ns1", path_parts=["Site", "Room1"])
    fl_ns2 = await upsert_fl_path(None, ns_2, namespace_slug="ns2", path_parts=["Site", "Room1"])

    await set_fl_room_category(None, ns_1, fl_id_or_label=fl_ns1, category_id="BOARDROOM")
    await set_fl_room_category(None, ns_2, fl_id_or_label=fl_ns2, category_id="HUDDLE")

    await assign_fl_responsible(
        None, ns_1, fl_id_or_label=fl_ns1, employee_id="EMP-1", role="primary"
    )

    # In ns_1: category is BOARDROOM, EMP-1 is assigned
    res1 = await get_fl_room_category(None, ns_1, fl_id_or_label=fl_ns1)
    assert res1["category_id"] == "BOARDROOM"
    resp1 = await list_fl_responsible(None, ns_1, fl_id_or_label=fl_ns1)
    assert len(resp1) == 1

    # In ns_2: category is HUDDLE, EMP-1 is NOT assigned
    res2 = await get_fl_room_category(None, ns_2, fl_id_or_label=fl_ns2)
    assert res2["category_id"] == "HUDDLE"
    resp2 = await list_fl_responsible(None, ns_2, fl_id_or_label=fl_ns2)
    assert len(resp2) == 0

    # Cross-tenant my_responsible lookup
    emp_ctx = PrincipalContext(principal_id="user-1", tier="employee", employee_id="EMP-1")
    assert len(await list_my_responsible_fls(None, ns_1, principal=emp_ctx)) == 1
    assert len(await list_my_responsible_fls(None, ns_2, principal=emp_ctx)) == 0


# ---------------------------------------------------------------------------
# 6. REST Handlers Parity & Status Codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_room_categories_endpoints(mock_engine):
    """Test REST handlers for room categories."""
    # GET /api/system-design/room-categories
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/room-categories",
            "query_string": b"",
        }
    )
    resp = await api_system_design_list_room_categories(req)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["count"] == 11

    # GET /api/system-design/room-categories/{id}
    req_cat = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/room-categories/BOARDROOM",
            "path_params": {"id": "BOARDROOM"},
        }
    )
    resp_cat = await api_system_design_get_room_category(req_cat)
    assert resp_cat.status_code == 200
    assert json.loads(resp_cat.body)["category"]["id"] == "BOARDROOM"

    # GET /api/system-design/room-categories/INVALID -> 404
    req_bad = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/room-categories/INVALID",
            "path_params": {"id": "INVALID"},
        }
    )
    resp_bad = await api_system_design_get_room_category(req_bad)
    assert resp_bad.status_code == 404


@pytest.mark.asyncio
async def test_rest_fl_metadata_endpoints(mock_engine):
    """Test REST handlers for setting/getting category and managing personnel."""
    ns_id = uuid4()
    fl_lbl = await upsert_fl_path(None, ns_id, namespace_slug="rest", path_parts=["HQ", "Room202"])

    # POST /api/system-design/functional-locations/{id}/room-category
    body_set_cat = json.dumps(
        {
            "namespace_id": str(ns_id),
            "category_id": "CONFERENCE_LARGE",
            "actor": "eng@test.com",
        }
    ).encode("utf-8")
    req_set = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/system-design/functional-locations/{fl_lbl}/room-category",
            "path_params": {"id": fl_lbl},
        }
    )
    req_set._body = body_set_cat
    resp_set = await api_system_design_set_fl_room_category(req_set)
    assert resp_set.status_code == 200
    assert json.loads(resp_set.body)["category_id"] == "CONFERENCE_LARGE"

    # GET /api/system-design/functional-locations/{id}/room-category
    req_get = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/system-design/functional-locations/{fl_lbl}/room-category",
            "path_params": {"id": fl_lbl},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_get = await api_system_design_get_fl_room_category(req_get)
    assert resp_get.status_code == 200
    assert json.loads(resp_get.body)["category_id"] == "CONFERENCE_LARGE"

    # POST /api/system-design/functional-locations/{id}/responsible
    body_assign = json.dumps(
        {
            "namespace_id": str(ns_id),
            "employee_id": "TECH-77",
            "role": "lead_technician",
        }
    ).encode("utf-8")
    req_assign = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/system-design/functional-locations/{fl_lbl}/responsible",
            "path_params": {"id": fl_lbl},
        }
    )
    req_assign._body = body_assign
    resp_assign = await api_system_design_assign_fl_responsible(req_assign)
    assert resp_assign.status_code == 200
    assert json.loads(resp_assign.body)["employee_id"] == "TECH-77"

    # GET /api/system-design/functional-locations/{id}/responsible
    req_list_resp = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/system-design/functional-locations/{fl_lbl}/responsible",
            "path_params": {"id": fl_lbl},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_list_resp = await api_system_design_list_fl_responsible(req_list_resp)
    assert resp_list_resp.status_code == 200
    assert json.loads(resp_list_resp.body)["count"] == 1

    # DELETE /api/system-design/functional-locations/{id}/responsible/{employee_id}
    req_del = Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": f"/api/system-design/functional-locations/{fl_lbl}/responsible/TECH-77",
            "path_params": {"id": fl_lbl, "employee_id": "TECH-77"},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_del = await api_system_design_unassign_fl_responsible(req_del)
    assert resp_del.status_code == 200
    assert json.loads(resp_del.body)["unassigned"] is True


# ---------------------------------------------------------------------------
# 7. MCP Handlers Parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handlers(mock_engine):
    """Test MCP tool handlers directly."""
    ns_id = uuid4()
    fl_lbl = await upsert_fl_path(None, ns_id, namespace_slug="mcp", path_parts=["Site", "Room5"])

    # 1. list room categories
    mcp_cats = json.loads(await handle_system_design_list_room_categories(mock_engine, {}))
    assert len(mcp_cats) == 11

    # 2. get room category
    mcp_cat = json.loads(
        await handle_system_design_get_room_category(mock_engine, {"category_id": "BOARDROOM"})
    )
    assert mcp_cat["id"] == "BOARDROOM"

    # 3. set fl room category
    set_ret = json.loads(
        await handle_system_design_set_fl_room_category(
            mock_engine,
            {"namespace_id": str(ns_id), "fl_id": fl_lbl, "category_id": "BOARDROOM"},
        )
    )
    assert set_ret["category_id"] == "BOARDROOM"

    # 4. get fl room category
    get_ret = json.loads(
        await handle_system_design_get_fl_room_category(
            mock_engine, {"namespace_id": str(ns_id), "fl_id": fl_lbl}
        )
    )
    assert get_ret["category_id"] == "BOARDROOM"

    # 5. assign responsible
    assign_ret = json.loads(
        await handle_system_design_assign_fl_responsible(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "fl_id": fl_lbl,
                "employee_id": "EMP-MCP-1",
                "role": "primary",
            },
        )
    )
    assert assign_ret["employee_id"] == "EMP-MCP-1"

    # 6. list responsible
    list_ret = json.loads(
        await handle_system_design_list_fl_responsible(
            mock_engine, {"namespace_id": str(ns_id), "fl_id": fl_lbl}
        )
    )
    assert len(list_ret) == 1

    # 7. list my responsible
    my_ret = json.loads(
        await handle_system_design_list_my_responsible_fls(
            mock_engine, {"namespace_id": str(ns_id), "employee_id": "EMP-MCP-1"}
        )
    )
    assert len(my_ret) == 1
    assert my_ret[0]["fl_label"] == fl_lbl

    # 8. unassign responsible
    unassign_ret = json.loads(
        await handle_system_design_unassign_fl_responsible(
            mock_engine,
            {"namespace_id": str(ns_id), "fl_id": fl_lbl, "employee_id": "EMP-MCP-1"},
        )
    )
    assert unassign_ret["unassigned"] is True
