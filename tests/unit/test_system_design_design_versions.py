"""tests/unit/test_system_design_design_versions.py

Unit test suite for System Design Wave C-3:
- Solution design versions per functional location
- Atomic active-design version toggling (set-active)
- ROOM_SPEC structured metadata validation (dimensions, acoustics, display, seating)
- Strict multi-tenancy boundary isolation
- Strict MCP error hierarchy (DesignNotFoundError, InvalidDesignError, InvalidRoomSpecError)
- MCP tool handlers (8 tools)
- REST admin routes (9 endpoints)
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from starlette.requests import Request

from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.system_design import (
    api_system_design_create_design,
    api_system_design_get_design,
    api_system_design_get_fl_active_design,
    api_system_design_get_room_spec,
    api_system_design_list_designs,
    api_system_design_list_fl_designs,
    api_system_design_set_active_design,
    api_system_design_set_room_spec,
    api_system_design_update_design,
)
from nce.orchestrator import NCEEngine
from nce.vertical_modules.system_design.design_versions import (
    DesignNotFoundError,
    DesignVersionError,
    InvalidDesignError,
    InvalidRoomSpecError,
    _clear_mem_store,
    canonical_design_label,
    create_design,
    get_active_design_for_fl,
    get_design,
    get_room_spec,
    list_designs,
    set_active_design,
    set_room_spec,
    update_design,
    validate_room_spec,
)
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_create_design,
    handle_system_design_get_active_design,
    handle_system_design_get_design,
    handle_system_design_get_room_spec,
    handle_system_design_list_designs,
    handle_system_design_set_active_design,
    handle_system_design_set_room_spec,
    handle_system_design_update_design,
)


@pytest.fixture(autouse=True)
def clean_mock_store():
    """Clear in-memory store before and after each test."""
    _clear_mem_store()
    yield
    _clear_mem_store()


# ===========================================================================
# 1. Domain Service Unit Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_and_get_design():
    ns_id = uuid4()
    fl_id = "FL-HQ-B1-R101"
    room_spec = {
        "width_m": 6.5,
        "depth_m": 8.0,
        "height_m": 2.8,
        "seating_capacity": 12,
        "display_type": "Dual 75in 4K LCD",
        "target_rt60_s": 0.5,
    }

    created = await create_design(
        None,
        namespace_id=ns_id,
        design_id="DSGN-001",
        name="Boardroom AV Rev 1",
        functional_location_id=fl_id,
        version=1,
        revision="revA",
        room_spec=room_spec,
        is_active=False,
        metadata={"drawings": ["DWG-001.pdf"]},
    )

    assert created["id"] == "DSGN-001"
    assert created["name"] == "Boardroom AV Rev 1"
    assert created["functional_location_id"] == "FL:FL-HQ-B1-R101"
    assert created["is_active"] is False
    assert created["version"] == 1
    assert created["revision"] == "revA"
    assert created["room_spec"]["width_m"] == 6.5
    assert created["room_spec"]["seating_capacity"] == 12

    # Fetch
    fetched = await get_design(None, namespace_id=ns_id, design_id="DSGN-001")
    assert fetched["id"] == "DSGN-001"
    assert fetched["label"] == canonical_design_label("DSGN-001")
    assert fetched["metadata"]["drawings"] == ["DWG-001.pdf"]


@pytest.mark.asyncio
async def test_get_nonexistent_design_raises_design_not_found():
    ns_id = uuid4()
    with pytest.raises(DesignNotFoundError) as exc_info:
        await get_design(None, namespace_id=ns_id, design_id="NONEXISTENT")
    # Verify exception inherits from KeyError and DesignVersionError
    assert isinstance(exc_info.value, KeyError)
    assert isinstance(exc_info.value, DesignVersionError)


@pytest.mark.asyncio
async def test_create_design_validation():
    ns_id = uuid4()
    with pytest.raises(InvalidDesignError):
        await create_design(None, ns_id, design_id="", name="Valid", functional_location_id="FL-1")

    with pytest.raises(InvalidDesignError):
        await create_design(None, ns_id, design_id="D1", name="", functional_location_id="FL-1")

    with pytest.raises(InvalidDesignError):
        await create_design(None, ns_id, design_id="D1", name="Valid", functional_location_id="")


def test_validate_room_spec():
    # Valid spec
    valid = {
        "width_m": "5.0",
        "depth_m": 4.2,
        "height_m": 3.0,
        "ceiling_height_m": 3.2,
        "seating_capacity": "8",
        "target_rt60_s": 0.45,
        "target_spl_dba": 75.0,
        "category": "CONFERENCE_MEDIUM",
        "display_type": "OLED",
        "display_size_in": 65,
        "camera_type": "PTZ 4K",
        "mic_type": "Ceiling Array",
        "speaker_type": "Pendant",
        "typical_features": ["Teams Rooms", "BYOD"],
        "custom_key": "custom_value",
    }
    cleaned = validate_room_spec(valid)
    assert cleaned["width_m"] == 5.0
    assert cleaned["seating_capacity"] == 8
    assert cleaned["display_size_in"] == 65.0
    assert cleaned["typical_features"] == ["Teams Rooms", "BYOD"]
    assert cleaned["custom_key"] == "custom_value"

    # Non-dict
    with pytest.raises(InvalidRoomSpecError):
        validate_room_spec("not a dict")

    # Negative float
    with pytest.raises(InvalidRoomSpecError):
        validate_room_spec({"width_m": -2.0})

    # Invalid numeric
    with pytest.raises(InvalidRoomSpecError):
        validate_room_spec({"depth_m": "abc"})

    # Negative seating capacity
    with pytest.raises(InvalidRoomSpecError):
        validate_room_spec({"seating_capacity": -5})

    # Non-list typical_features
    with pytest.raises(InvalidRoomSpecError):
        validate_room_spec({"typical_features": "just a string"})


@pytest.mark.asyncio
async def test_atomic_set_active_design():
    ns_id = uuid4()
    fl_1 = "FL-ROOM-A"
    fl_2 = "FL-ROOM-B"

    # Create 3 designs for FL-1
    await create_design(None, ns_id, "D1", "Design 1", fl_1, is_active=True)
    await create_design(None, ns_id, "D2", "Design 2", fl_1, is_active=False)
    await create_design(None, ns_id, "D3", "Design 3", fl_1, is_active=False)

    # Create 1 design for FL-2 (should remain unaffected)
    await create_design(None, ns_id, "D_OTHER", "Design Other", fl_2, is_active=True)

    assert (await get_design(None, ns_id, "D1"))["is_active"] is True
    assert (await get_active_design_for_fl(None, ns_id, fl_1))["id"] == "D1"

    # Activate D2 atomically
    activated = await set_active_design(None, ns_id, "D2")
    assert activated["id"] == "D2"
    assert activated["is_active"] is True

    # Check D1 is no longer active, D2 is active, D3 is still not active
    assert (await get_design(None, ns_id, "D1"))["is_active"] is False
    assert (await get_design(None, ns_id, "D2"))["is_active"] is True
    assert (await get_design(None, ns_id, "D3"))["is_active"] is False

    # Check active for FL-1
    active_fl1 = await get_active_design_for_fl(None, ns_id, fl_1)
    assert active_fl1["id"] == "D2"

    # Check FL-2 active status is unchanged
    assert (await get_design(None, ns_id, "D_OTHER"))["is_active"] is True
    assert (await get_active_design_for_fl(None, ns_id, fl_2))["id"] == "D_OTHER"


@pytest.mark.asyncio
async def test_multi_tenancy_isolation():
    ns_a = uuid4()
    ns_b = uuid4()
    fl_id = "FL-CONF-01"

    await create_design(None, ns_a, "DSGN-TENANT-A", "Design A", fl_id)
    await create_design(None, ns_b, "DSGN-TENANT-B", "Design B", fl_id)

    # Tenant A sees only its own design
    list_a = await list_designs(None, ns_a)
    assert len(list_a) == 1
    assert list_a[0]["id"] == "DSGN-TENANT-A"

    # Tenant B sees only its own design
    list_b = await list_designs(None, ns_b)
    assert len(list_b) == 1
    assert list_b[0]["id"] == "DSGN-TENANT-B"

    # Cross-tenant get raises DesignNotFoundError
    with pytest.raises(DesignNotFoundError):
        await get_design(None, ns_a, "DSGN-TENANT-B")


@pytest.mark.asyncio
async def test_list_designs_filtering_and_pagination():
    ns_id = uuid4()
    fl_1 = "FL-ROOM-1"
    fl_2 = "FL-ROOM-2"

    await create_design(None, ns_id, "D1", "Audio System V1", fl_1, is_active=True)
    await create_design(None, ns_id, "D2", "Video Wall V2", fl_1, is_active=False)
    await create_design(None, ns_id, "D3", "Lighting Control", fl_2, is_active=True)

    # Filter by FL
    fl1_designs = await list_designs(None, ns_id, functional_location_id=fl_1)
    assert len(fl1_designs) == 2
    assert {d["id"] for d in fl1_designs} == {"D1", "D2"}

    # Filter by is_active
    active_designs = await list_designs(None, ns_id, is_active=True)
    assert len(active_designs) == 2
    assert {d["id"] for d in active_designs} == {"D1", "D3"}

    # Search query
    audio_designs = await list_designs(None, ns_id, query="Audio")
    assert len(audio_designs) == 1
    assert audio_designs[0]["id"] == "D1"

    # Pagination
    paged = await list_designs(None, ns_id, limit=2, offset=1)
    assert len(paged) == 2


@pytest.mark.asyncio
async def test_update_design_and_room_spec():
    ns_id = uuid4()
    await create_design(
        None,
        ns_id,
        "D1",
        "Initial Name",
        "FL-1",
        room_spec={"width_m": 4.0},
        revision="revA",
    )

    # Update design name and revision
    updated = await update_design(None, ns_id, "D1", name="Updated Name", revision="revB")
    assert updated["name"] == "Updated Name"
    assert updated["revision"] == "revB"
    assert updated["room_spec"]["width_m"] == 4.0

    # Update room spec via set_room_spec
    new_spec = await set_room_spec(None, ns_id, "D1", {"width_m": 6.0, "seating_capacity": 10})
    assert new_spec["width_m"] == 6.0
    assert new_spec["seating_capacity"] == 10

    # Get room spec directly
    got_spec = await get_room_spec(None, ns_id, "D1")
    assert got_spec == new_spec


# ===========================================================================
# 2. MCP Tool Handlers Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_mcp_handlers_lifecycle():
    engine = NCEEngine()
    ns_id = str(uuid4())
    fl_id = "FL-BOARDROOM"

    # 1. create_design
    create_args = {
        "namespace_id": ns_id,
        "design_id": "MCP-DSGN-01",
        "name": "MCP Design Test",
        "functional_location_id": fl_id,
        "room_spec": {"width_m": 8.0, "depth_m": 10.0, "seating_capacity": 20},
        "is_active": False,
    }
    raw_res = await handle_system_design_create_design(engine, create_args)
    created = json.loads(raw_res)
    assert created["id"] == "MCP-DSGN-01"
    assert created["name"] == "MCP Design Test"

    # 2. get_design
    raw_res = await handle_system_design_get_design(
        engine, {"namespace_id": ns_id, "design_id": "MCP-DSGN-01"}
    )
    fetched = json.loads(raw_res)
    assert fetched["id"] == "MCP-DSGN-01"

    # 3. list_designs
    raw_res = await handle_system_design_list_designs(
        engine, {"namespace_id": ns_id, "functional_location_id": fl_id}
    )
    listed = json.loads(raw_res)
    assert len(listed) == 1
    assert listed[0]["id"] == "MCP-DSGN-01"

    # 4. update_design
    raw_res = await handle_system_design_update_design(
        engine,
        {
            "namespace_id": ns_id,
            "design_id": "MCP-DSGN-01",
            "name": "MCP Design Updated",
        },
    )
    updated = json.loads(raw_res)
    assert updated["name"] == "MCP Design Updated"

    # 5. set_active_design
    raw_res = await handle_system_design_set_active_design(
        engine, {"namespace_id": ns_id, "design_id": "MCP-DSGN-01"}
    )
    active = json.loads(raw_res)
    assert active["is_active"] is True

    # 6. get_active_design
    raw_res = await handle_system_design_get_active_design(
        engine, {"namespace_id": ns_id, "functional_location_id": fl_id}
    )
    fl_active = json.loads(raw_res)
    assert fl_active["id"] == "MCP-DSGN-01"

    # 7. get_room_spec
    raw_res = await handle_system_design_get_room_spec(
        engine, {"namespace_id": ns_id, "design_id": "MCP-DSGN-01"}
    )
    spec = json.loads(raw_res)
    assert spec["width_m"] == 8.0
    assert spec["seating_capacity"] == 20

    # 8. set_room_spec
    raw_res = await handle_system_design_set_room_spec(
        engine,
        {
            "namespace_id": ns_id,
            "design_id": "MCP-DSGN-01",
            "room_spec": {"width_m": 9.5, "seating_capacity": 24},
        },
    )
    updated_spec = json.loads(raw_res)
    assert updated_spec["width_m"] == 9.5
    assert updated_spec["seating_capacity"] == 24


# ===========================================================================
# 3. REST Admin Handlers Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_rest_endpoints_lifecycle():
    engine = NCEEngine()
    admin_state.engine = engine
    ns_id = str(uuid4())
    fl_id = "FL-REST-01"

    # 1. POST /api/system-design/designs
    body_create = json.dumps(
        {
            "namespace_id": ns_id,
            "design_id": "REST-DSGN-01",
            "name": "REST Design",
            "functional_location_id": fl_id,
            "room_spec": {"width_m": 5.0, "seating_capacity": 6},
        }
    ).encode("utf-8")
    req_create = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/designs",
        }
    )
    req_create._body = body_create
    resp_create = await api_system_design_create_design(req_create)
    assert resp_create.status_code == 201
    data_create = json.loads(resp_create.body)
    assert data_create["status"] == "ok"
    assert data_create["design"]["id"] == "REST-DSGN-01"

    # 2. GET /api/system-design/designs/{id}
    req_get = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/designs/REST-DSGN-01",
            "path_params": {"id": "REST-DSGN-01"},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_get = await api_system_design_get_design(req_get)
    assert resp_get.status_code == 200
    assert json.loads(resp_get.body)["design"]["name"] == "REST Design"

    # 3. GET /api/system-design/designs
    req_list = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/designs",
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_list = await api_system_design_list_designs(req_list)
    assert resp_list.status_code == 200
    assert json.loads(resp_list.body)["count"] == 1

    # 4. PATCH /api/system-design/designs/{id}
    body_patch = json.dumps(
        {
            "namespace_id": ns_id,
            "name": "REST Design Updated",
        }
    ).encode("utf-8")
    req_patch = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/system-design/designs/REST-DSGN-01",
            "path_params": {"id": "REST-DSGN-01"},
        }
    )
    req_patch._body = body_patch
    resp_patch = await api_system_design_update_design(req_patch)
    assert resp_patch.status_code == 200
    assert json.loads(resp_patch.body)["design"]["name"] == "REST Design Updated"

    # 5. POST /api/system-design/designs/{id}/set-active
    req_active = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/designs/REST-DSGN-01/set-active",
            "path_params": {"id": "REST-DSGN-01"},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_active = await api_system_design_set_active_design(req_active)
    assert resp_active.status_code == 200
    assert json.loads(resp_active.body)["design"]["is_active"] is True

    # 6. GET /api/system-design/designs/{id}/room-spec
    req_rs_get = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/designs/REST-DSGN-01/room-spec",
            "path_params": {"id": "REST-DSGN-01"},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_rs_get = await api_system_design_get_room_spec(req_rs_get)
    assert resp_rs_get.status_code == 200
    assert json.loads(resp_rs_get.body)["room_spec"]["width_m"] == 5.0

    # 7. PUT /api/system-design/designs/{id}/room-spec
    body_rs_put = json.dumps(
        {
            "namespace_id": ns_id,
            "room_spec": {"width_m": 7.5, "seating_capacity": 14},
        }
    ).encode("utf-8")
    req_rs_put = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/system-design/designs/REST-DSGN-01/room-spec",
            "path_params": {"id": "REST-DSGN-01"},
        }
    )
    req_rs_put._body = body_rs_put
    resp_rs_put = await api_system_design_set_room_spec(req_rs_put)
    assert resp_rs_put.status_code == 200
    assert json.loads(resp_rs_put.body)["room_spec"]["width_m"] == 7.5

    # 8. GET /api/system-design/functional-locations/{id}/designs
    req_fl_designs = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/system-design/functional-locations/{fl_id}/designs",
            "path_params": {"id": fl_id},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_fl_designs = await api_system_design_list_fl_designs(req_fl_designs)
    assert resp_fl_designs.status_code == 200
    assert json.loads(resp_fl_designs.body)["count"] == 1

    # 9. GET /api/system-design/functional-locations/{id}/active-design
    req_fl_act = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/system-design/functional-locations/{fl_id}/active-design",
            "path_params": {"id": fl_id},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_fl_act = await api_system_design_get_fl_active_design(req_fl_act)
    assert resp_fl_act.status_code == 200
    assert json.loads(resp_fl_act.body)["active_design"]["id"] == "REST-DSGN-01"


@pytest.mark.asyncio
async def test_rest_endpoints_error_handling():
    engine = NCEEngine()
    admin_state.engine = engine
    ns_id = str(uuid4())

    # 404 on get non-existent design
    req_404 = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/designs/DOESNOTEXIST",
            "path_params": {"id": "DOESNOTEXIST"},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_404 = await api_system_design_get_design(req_404)
    assert resp_404.status_code == 404

    # 422 on create with missing fields
    body_bad = json.dumps({"namespace_id": ns_id, "design_id": "BAD"}).encode("utf-8")
    req_bad = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/designs",
        }
    )
    req_bad._body = body_bad
    resp_bad = await api_system_design_create_design(req_bad)
    assert resp_bad.status_code == 422
