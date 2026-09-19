"""tests/unit/test_system_design_design_requests.py

Unit test suite for System Design Wave C-4:
- Solution design intake queue (losningsdesign-ko / DESIGN_REQUEST)
- Intake queue lifecycle: pending -> assigned -> in_progress -> completed / cancelled / rejected
- Owner assignment and priority filtering (low, normal, high, urgent)
- Fulfillment from quote via do_design_from_quote / fulfill_design_request_from_quote
- Strict multi-tenancy boundary isolation
- Domain exceptions (DesignRequestNotFoundError, InvalidDesignRequestStatusError, InvalidDesignRequestPayloadError)
- MCP tool handlers (7 tools)
- REST admin routes (8 endpoints)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from starlette.requests import Request

from nce.admin_handlers._shared import admin_state
from nce.admin_handlers.system_design import (
    api_system_design_assign_request,
    api_system_design_cancel_request,
    api_system_design_complete_request,
    api_system_design_create_request,
    api_system_design_fulfill_request,
    api_system_design_get_request,
    api_system_design_list_requests,
    api_system_design_update_request,
)
from nce.orchestrator import NCEEngine
from nce.vertical_modules.system_design.design_requests import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_URGENT,
    STATUS_ASSIGNED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    DesignRequestNotFoundError,
    InvalidDesignRequestPayloadError,
    InvalidDesignRequestStatusError,
    _clear_mem_store,
    assign_design_request,
    cancel_design_request,
    complete_design_request,
    create_design_request,
    find_active_request_for_quote,
    fulfill_design_request_from_quote,
    get_design_request,
    list_design_requests,
    update_design_request,
)
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_assign_design_request,
    handle_system_design_complete_design_request,
    handle_system_design_create_design_request,
    handle_system_design_fulfill_request_from_quote,
    handle_system_design_get_design_request,
    handle_system_design_list_design_requests,
    handle_system_design_update_design_request,
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
async def test_create_and_get_design_request():
    ns_id = uuid4()
    fl_id = "FL-HQ-B1-R101"
    quote_id = "QUOTE-987"
    room_spec = {
        "width_m": 6.5,
        "depth_m": 8.0,
        "height_m": 2.8,
        "seating_capacity": 12,
    }

    # 1. Create with owner -> auto assigned status
    req = await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Main Boardroom AV Upgrade",
        quote_id=quote_id,
        functional_location_id=fl_id,
        description="Upgrade VC system and acoustic panels",
        priority=PRIORITY_HIGH,
        owner_id="EMP-101",
        room_spec=room_spec,
        metadata={"client_vip": True},
        request_id="REQ-001",
    )

    assert req["id"] == "REQ-001"
    assert req["title"] == "Main Boardroom AV Upgrade"
    assert req["status"] == STATUS_ASSIGNED
    assert req["priority"] == PRIORITY_HIGH
    assert req["owner_id"] == "EMP-101"
    assert req["quote_id"] == quote_id
    assert req["room_spec"]["seating_capacity"] == 12
    assert req["metadata"]["client_vip"] is True

    # 2. Fetch created request
    fetched = await get_design_request(conn=None, namespace_id=ns_id, request_id="REQ-001")
    assert fetched["id"] == "REQ-001"
    assert fetched["title"] == req["title"]

    # 3. Create without owner -> pending status
    req2 = await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Huddle Room A Refresh",
        priority=PRIORITY_NORMAL,
    )
    assert req2["status"] == STATUS_PENDING
    assert req2["owner_id"] is None


@pytest.mark.asyncio
async def test_create_validation_errors():
    ns_id = uuid4()

    # Empty title
    with pytest.raises(InvalidDesignRequestPayloadError, match="title"):
        await create_design_request(conn=None, namespace_id=ns_id, title="")

    # Invalid priority
    with pytest.raises(InvalidDesignRequestPayloadError, match="priority"):
        await create_design_request(
            conn=None, namespace_id=ns_id, title="Test", priority="super_urgent"
        )


@pytest.mark.asyncio
async def test_get_not_found_raises():
    ns_id = uuid4()
    with pytest.raises(DesignRequestNotFoundError):
        await get_design_request(conn=None, namespace_id=ns_id, request_id="NON-EXISTENT")


@pytest.mark.asyncio
async def test_list_design_requests_filters():
    ns_id = uuid4()

    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Audio Room 1",
        description="First audio project",
        priority=PRIORITY_LOW,
        owner_id="EMP-1",
        quote_id="Q-1",
        functional_location_id="FL-1",
        request_id="R-1",
    )
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Video Wall B",
        description="Big screen",
        priority=PRIORITY_URGENT,
        owner_id="EMP-2",
        quote_id="Q-2",
        functional_location_id="FL-2",
        request_id="R-2",
    )
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Town Hall Audio",
        description="Conference hall acoustics",
        priority=PRIORITY_HIGH,
        quote_id="Q-1",
        functional_location_id="FL-1",
        request_id="R-3",
    )

    # Filter by status (R-1, R-2 are assigned; R-3 is pending)
    pending = await list_design_requests(conn=None, namespace_id=ns_id, status=STATUS_PENDING)
    assert len(pending) == 1
    assert pending[0]["id"] == "R-3"

    # Filter by owner_id
    emp1_items = await list_design_requests(conn=None, namespace_id=ns_id, owner_id="EMP-1")
    assert len(emp1_items) == 1
    assert emp1_items[0]["id"] == "R-1"

    # Filter by quote_id
    q1_items = await list_design_requests(conn=None, namespace_id=ns_id, quote_id="Q-1")
    assert len(q1_items) == 2

    # Filter by priority
    urgent_items = await list_design_requests(
        conn=None, namespace_id=ns_id, priority=PRIORITY_URGENT
    )
    assert len(urgent_items) == 1
    assert urgent_items[0]["id"] == "R-2"

    # Search query
    audio_items = await list_design_requests(conn=None, namespace_id=ns_id, query="audio")
    assert len(audio_items) == 2

    # Pagination
    paged = await list_design_requests(conn=None, namespace_id=ns_id, limit=2, offset=1)
    assert len(paged) == 2


@pytest.mark.asyncio
async def test_update_and_assign_lifecycle():
    ns_id = uuid4()
    req = await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Initial Title",
        priority=PRIORITY_LOW,
        request_id="R-LIFE",
    )
    assert req["status"] == STATUS_PENDING

    # 1. Update title and priority
    updated = await update_design_request(
        conn=None,
        namespace_id=ns_id,
        request_id="R-LIFE",
        title="Updated Title",
        priority=PRIORITY_HIGH,
        description="Added description",
    )
    assert updated["title"] == "Updated Title"
    assert updated["priority"] == PRIORITY_HIGH
    assert updated["description"] == "Added description"

    # 2. Assign owner -> auto status=assigned
    assigned = await assign_design_request(
        conn=None,
        namespace_id=ns_id,
        request_id="R-LIFE",
        owner_id="ENG-42",
    )
    assert assigned["owner_id"] == "ENG-42"
    assert assigned["status"] == STATUS_ASSIGNED

    # 3. Transition to in_progress
    in_prog = await update_design_request(
        conn=None,
        namespace_id=ns_id,
        request_id="R-LIFE",
        status=STATUS_IN_PROGRESS,
    )
    assert in_prog["status"] == STATUS_IN_PROGRESS

    # 4. Complete request
    completed = await complete_design_request(
        conn=None,
        namespace_id=ns_id,
        request_id="R-LIFE",
        design_id="DSGN-V1-42",
    )
    assert completed["status"] == STATUS_COMPLETED
    assert completed["design_id"] == "DSGN-V1-42"
    assert completed["completed_at"] is not None

    # 5. Invalid status transition error
    with pytest.raises(InvalidDesignRequestStatusError):
        await update_design_request(
            conn=None, namespace_id=ns_id, request_id="R-LIFE", status="invalid_status"
        )


@pytest.mark.asyncio
async def test_cancel_design_request():
    ns_id = uuid4()
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="To Cancel",
        request_id="R-CANCEL",
    )
    cancelled = await cancel_design_request(
        conn=None,
        namespace_id=ns_id,
        request_id="R-CANCEL",
        reason="Client changed scope",
    )
    assert cancelled["status"] == STATUS_CANCELLED
    assert cancelled["metadata"]["cancel_reason"] == "Client changed scope"


@pytest.mark.asyncio
async def test_find_active_request_for_quote():
    ns_id = uuid4()
    # Terminal completed request
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Completed earlier",
        quote_id="Q-SEARCH",
        request_id="R-COMP",
    )
    await complete_design_request(
        conn=None, namespace_id=ns_id, request_id="R-COMP", design_id="D-OLD"
    )

    # Active pending request
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Active one",
        quote_id="Q-SEARCH",
        request_id="R-ACTIVE",
    )

    active = await find_active_request_for_quote(conn=None, namespace_id=ns_id, quote_id="Q-SEARCH")
    assert active is not None
    assert active["id"] == "R-ACTIVE"


@pytest.mark.asyncio
async def test_fulfill_design_request_validation():
    ns_id = uuid4()
    engine = NCEEngine()

    # Request without quote_id fails
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="No Quote",
        request_id="R-NOQ",
    )
    with pytest.raises(InvalidDesignRequestPayloadError, match="quote_id"):
        await fulfill_design_request_from_quote(engine, ns_id, "R-NOQ")

    # Cancelled request fails
    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Cancelled",
        quote_id="Q-100",
        request_id="R-CANC",
    )
    await cancel_design_request(conn=None, namespace_id=ns_id, request_id="R-CANC")
    with pytest.raises(InvalidDesignRequestStatusError, match="terminal"):
        await fulfill_design_request_from_quote(engine, ns_id, "R-CANC")


@pytest.mark.asyncio
async def test_fulfill_design_request_delegation():
    ns_id = uuid4()
    engine = NCEEngine()

    await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Ready to fulfill",
        quote_id="Q-200",
        request_id="R-READY",
    )

    mock_result = {
        "design_id": "DESIGN-Q-200",
        "quote_label": "QUOTE:Q-200",
        "design_label": "DESIGN:DESIGN-Q-200",
        "authored": {"nodes": 5, "edges": 4},
        "design_request_id": "R-READY",
        "design_request_status": STATUS_COMPLETED,
    }

    with patch(
        "nce.vertical_modules.system_design.from_quote.do_design_from_quote",
        new=AsyncMock(return_value=mock_result),
    ) as mock_fq:
        res = await fulfill_design_request_from_quote(engine, ns_id, "R-READY")
        assert res["design_id"] == "DESIGN-Q-200"
        assert res["design_request_status"] == STATUS_COMPLETED
        mock_fq.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_tenancy_boundary():
    ns_1 = uuid4()
    ns_2 = uuid4()

    await create_design_request(
        conn=None,
        namespace_id=ns_1,
        title="Tenant 1 Request",
        request_id="SHARED-ID",
    )

    # Must exist in tenant 1
    t1_req = await get_design_request(conn=None, namespace_id=ns_1, request_id="SHARED-ID")
    assert t1_req["id"] == "SHARED-ID"

    # Must NOT exist in tenant 2
    with pytest.raises(DesignRequestNotFoundError):
        await get_design_request(conn=None, namespace_id=ns_2, request_id="SHARED-ID")

    # Tenant 2 list is empty
    t2_list = await list_design_requests(conn=None, namespace_id=ns_2)
    assert len(t2_list) == 0


# ===========================================================================
# 2. MCP Handlers Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_mcp_handlers_lifecycle():
    engine = NCEEngine()
    ns_id = str(uuid4())

    # 1. create_design_request
    raw_res = await handle_system_design_create_design_request(
        engine,
        {
            "namespace_id": ns_id,
            "title": "MCP Boardroom Request",
            "priority": "high",
            "quote_id": "Q-MCP-1",
            "request_id": "REQ-MCP-1",
        },
    )
    created = json.loads(raw_res)
    assert created["id"] == "REQ-MCP-1"
    assert created["priority"] == "high"

    # 2. get_design_request
    raw_res = await handle_system_design_get_design_request(
        engine,
        {"namespace_id": ns_id, "request_id": "REQ-MCP-1"},
    )
    fetched = json.loads(raw_res)
    assert fetched["id"] == "REQ-MCP-1"

    # 3. list_design_requests
    raw_res = await handle_system_design_list_design_requests(
        engine,
        {"namespace_id": ns_id, "priority": "high"},
    )
    items = json.loads(raw_res)
    assert len(items) == 1

    # 4. update_design_request
    raw_res = await handle_system_design_update_design_request(
        engine,
        {
            "namespace_id": ns_id,
            "request_id": "REQ-MCP-1",
            "description": "Added via MCP update",
        },
    )
    updated = json.loads(raw_res)
    assert updated["description"] == "Added via MCP update"

    # 5. assign_design_request
    raw_res = await handle_system_design_assign_design_request(
        engine,
        {
            "namespace_id": ns_id,
            "request_id": "REQ-MCP-1",
            "owner_id": "ENG-99",
        },
    )
    assigned = json.loads(raw_res)
    assert assigned["owner_id"] == "ENG-99"
    assert assigned["status"] == STATUS_ASSIGNED

    # 6. complete_design_request
    raw_res = await handle_system_design_complete_design_request(
        engine,
        {
            "namespace_id": ns_id,
            "request_id": "REQ-MCP-1",
            "design_id": "DSGN-MCP-DONE",
        },
    )
    completed = json.loads(raw_res)
    assert completed["status"] == STATUS_COMPLETED
    assert completed["design_id"] == "DSGN-MCP-DONE"

    # 7. fulfill_request_from_quote
    await handle_system_design_create_design_request(
        engine,
        {
            "namespace_id": ns_id,
            "title": "MCP To Fulfill",
            "quote_id": "Q-FULFILL",
            "request_id": "REQ-MCP-2",
        },
    )
    mock_fq = {
        "design_id": "DESIGN-Q-FULFILL",
        "quote_label": "QUOTE:Q-FULFILL",
        "design_label": "DESIGN:DESIGN-Q-FULFILL",
        "authored": {"nodes": 2, "edges": 1},
        "design_request_id": "REQ-MCP-2",
    }
    with patch(
        "nce.vertical_modules.system_design.from_quote.do_design_from_quote",
        new=AsyncMock(return_value=mock_fq),
    ):
        raw_res = await handle_system_design_fulfill_request_from_quote(
            engine,
            {
                "namespace_id": ns_id,
                "request_id": "REQ-MCP-2",
            },
        )
        res = json.loads(raw_res)
        assert res["design_id"] == "DESIGN-Q-FULFILL"


# ===========================================================================
# 3. REST Admin Handlers Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_rest_endpoints_lifecycle():
    engine = NCEEngine()
    admin_state.engine = engine
    ns_id = str(uuid4())

    # 1. POST /api/system-design/requests
    body_create = json.dumps(
        {
            "namespace_id": ns_id,
            "request_id": "REST-REQ-1",
            "title": "REST Boardroom Request",
            "quote_id": "Q-REST-1",
            "priority": "urgent",
        }
    ).encode("utf-8")
    req_create = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/requests",
        }
    )
    req_create._body = body_create
    resp_create = await api_system_design_create_request(req_create)
    assert resp_create.status_code == 201
    data_create = json.loads(resp_create.body)
    assert data_create["status"] == "ok"
    assert data_create["request"]["id"] == "REST-REQ-1"
    assert data_create["request"]["priority"] == "urgent"

    # 2. GET /api/system-design/requests/{id}
    req_get = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/requests/REST-REQ-1",
            "path_params": {"id": "REST-REQ-1"},
            "query_string": f"namespace_id={ns_id}".encode(),
        }
    )
    resp_get = await api_system_design_get_request(req_get)
    assert resp_get.status_code == 200
    assert json.loads(resp_get.body)["request"]["title"] == "REST Boardroom Request"

    # 3. GET /api/system-design/requests
    req_list = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system-design/requests",
            "query_string": f"namespace_id={ns_id}&priority=urgent".encode(),
        }
    )
    resp_list = await api_system_design_list_requests(req_list)
    assert resp_list.status_code == 200
    data_list = json.loads(resp_list.body)
    assert data_list["count"] == 1

    # 4. PATCH /api/system-design/requests/{id}
    body_update = json.dumps(
        {
            "namespace_id": ns_id,
            "description": "Updated via REST PATCH",
        }
    ).encode("utf-8")
    req_update = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/system-design/requests/REST-REQ-1",
            "path_params": {"id": "REST-REQ-1"},
        }
    )
    req_update._body = body_update
    resp_update = await api_system_design_update_request(req_update)
    assert resp_update.status_code == 200
    assert json.loads(resp_update.body)["request"]["description"] == "Updated via REST PATCH"

    # 5. POST /api/system-design/requests/{id}/assign
    body_assign = json.dumps(
        {
            "namespace_id": ns_id,
            "owner_id": "REST-ENG-1",
        }
    ).encode("utf-8")
    req_assign = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/requests/REST-REQ-1/assign",
            "path_params": {"id": "REST-REQ-1"},
        }
    )
    req_assign._body = body_assign
    resp_assign = await api_system_design_assign_request(req_assign)
    assert resp_assign.status_code == 200
    assert json.loads(resp_assign.body)["request"]["owner_id"] == "REST-ENG-1"

    # 6. POST /api/system-design/requests/{id}/complete
    body_comp = json.dumps(
        {
            "namespace_id": ns_id,
            "design_id": "REST-DSGN-DONE",
        }
    ).encode("utf-8")
    req_comp = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/requests/REST-REQ-1/complete",
            "path_params": {"id": "REST-REQ-1"},
        }
    )
    req_comp._body = body_comp
    resp_comp = await api_system_design_complete_request(req_comp)
    assert resp_comp.status_code == 200
    assert json.loads(resp_comp.body)["request"]["status"] == STATUS_COMPLETED

    # 7. POST /api/system-design/requests/{id}/cancel
    body_canc_create = json.dumps(
        {
            "namespace_id": ns_id,
            "request_id": "REST-REQ-CANC",
            "title": "To be cancelled",
        }
    ).encode("utf-8")
    req_c1 = Request({"type": "http", "method": "POST", "path": "/api/system-design/requests"})
    req_c1._body = body_canc_create
    await api_system_design_create_request(req_c1)

    body_cancel = json.dumps(
        {
            "namespace_id": ns_id,
            "reason": "Cancelled by client",
        }
    ).encode("utf-8")
    req_canc = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/system-design/requests/REST-REQ-CANC/cancel",
            "path_params": {"id": "REST-REQ-CANC"},
        }
    )
    req_canc._body = body_cancel
    resp_canc = await api_system_design_cancel_request(req_canc)
    assert resp_canc.status_code == 200
    assert json.loads(resp_canc.body)["request"]["status"] == STATUS_CANCELLED

    # 8. POST /api/system-design/requests/{id}/fulfill
    body_ful_create = json.dumps(
        {
            "namespace_id": ns_id,
            "request_id": "REST-REQ-FUL",
            "title": "To be fulfilled",
            "quote_id": "Q-FUL-REST",
        }
    ).encode("utf-8")
    req_f1 = Request({"type": "http", "method": "POST", "path": "/api/system-design/requests"})
    req_f1._body = body_ful_create
    await api_system_design_create_request(req_f1)

    mock_fq = {
        "design_id": "DESIGN-Q-FUL-REST",
        "quote_label": "QUOTE:Q-FUL-REST",
        "design_label": "DESIGN:DESIGN-Q-FUL-REST",
        "authored": {"nodes": 3, "edges": 2},
    }
    with patch(
        "nce.vertical_modules.system_design.from_quote.do_design_from_quote",
        new=AsyncMock(return_value=mock_fq),
    ):
        body_fulfill = json.dumps({"namespace_id": ns_id}).encode("utf-8")
        req_ful = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/system-design/requests/REST-REQ-FUL/fulfill",
                "path_params": {"id": "REST-REQ-FUL"},
            }
        )
        req_ful._body = body_fulfill
        resp_ful = await api_system_design_fulfill_request(req_ful)
        assert resp_ful.status_code == 200
        assert json.loads(resp_ful.body)["result"]["design_id"] == "DESIGN-Q-FUL-REST"


@pytest.mark.asyncio
async def test_do_design_from_quote_completes_design_request():
    from nce.vertical_modules.system_design.from_quote import do_design_from_quote

    ns_id = uuid4()
    quote_id = "Q-TEST-INT"
    req = await create_design_request(
        conn=None,
        namespace_id=ns_id,
        title="Integration Request",
        quote_id=quote_id,
        request_id="REQ-INT-1",
    )
    assert req["status"] == STATUS_PENDING

    class DummyConn:
        async def execute(self, *args, **kwargs):
            pass

        async def fetch(self, *args, **kwargs):
            return []

        async def fetchrow(self, *args, **kwargs):
            return None

    class MockScopedSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return DummyConn()

        async def __aexit__(self, *args):
            pass

    engine = NCEEngine()
    engine.pg_pool = object()

    with (
        patch(
            "nce.vertical_modules.system_design.from_quote.scoped_pg_session",
            new=MockScopedSession,
        ),
        patch(
            "nce.vertical_modules.system_design.from_quote._read_quote_lines",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "nce.vertical_modules.system_design.from_quote.do_author_functional_location",
            new=AsyncMock(return_value={"nodes": 1, "edges": 0}),
        ),
        patch(
            "nce.vertical_modules.system_design.from_quote._upsert_edge",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.system_design.from_quote.emit_graph_write",
            new=AsyncMock(),
        ),
        patch(
            "nce.vertical_modules.system_design.from_quote.find_active_request_for_quote",
            new=AsyncMock(return_value=req),
        ),
        patch(
            "nce.vertical_modules.system_design.from_quote.complete_design_request",
            new=AsyncMock(return_value={"id": "REQ-INT-1", "status": STATUS_COMPLETED}),
        ),
    ):
        res = await do_design_from_quote(engine, {"namespace_id": str(ns_id), "quote_id": quote_id})
        assert res["design_request_id"] == "REQ-INT-1"
        assert res["design_request_status"] == STATUS_COMPLETED
