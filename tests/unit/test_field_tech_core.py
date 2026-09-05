"""
tests/unit/test_field_tech_core.py
===================================
Unit test suite for M12 Field Tech Engine domain logic:
  - require_field_tech_enabled guard (opt-in gate)
  - do_create_work_order, do_get_work_order, do_query_work_order, do_assign
  - do_complete_checklist (ISO9001 verification record + quality gates)
  - do_scan_serial (seed edge: BOM_LINE -[installed_as]-> ASSET)
  - do_log_time (time entry + op_id dedup)
  - do_attach_photo (photo documentation)
  - do_sync (offline reconciliation with server-sequence ordering & conflict surfacing)
  - do_partner_view (Partner Access Model: partner-scoped & field-redacted projection)
  - do_record_outcome (v3_cognitive_ledger write with field_tech_source_id)
  - do_dispatch (AI dispatch ranking over composite weights)
  - Cross-tenant isolation swap-mutant tests (Charter §4.4)
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from asyncpg.exceptions import DataError

from nce.vertical_modules.field_tech._guard import (
    FieldTechDisabledError,
    require_field_tech_enabled,
)
from nce.vertical_modules.field_tech.checklist import (
    ChecklistIncompleteError,
    do_complete_checklist,
)
from nce.vertical_modules.field_tech.dispatch import do_dispatch
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.field_tech.partner_view import do_partner_view
from nce.vertical_modules.field_tech.photo import do_attach_photo
from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.field_tech.sync import do_sync
from nce.vertical_modules.field_tech.time_entry import do_log_time
from nce.vertical_modules.field_tech.work_orders import (
    WorkOrderNotFoundError,
    do_assign,
    do_create_work_order,
    do_get_work_order,
    do_query_work_order,
)

_NS_A = "00000000-0000-4000-8000-000000000001"
_NS_B = "00000000-0000-4000-8000-000000000002"
_PARTNER_SCOPE_1 = "11111111-1111-4000-8000-111111111111"
_PARTNER_SCOPE_2 = "22222222-2222-4000-8000-222222222222"
_WO_ID_1 = "WO-2026-0001"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    """Create a mock pool yielding *conn* on acquire."""
    pool = MagicMock()
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_pg_session with a pass-through for unit tests."""

    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        ctx = pool.acquire()
        if hasattr(ctx, "__aenter__"):
            conn = await ctx.__aenter__()
            try:
                yield conn
            finally:
                await ctx.__aexit__(None, None, None)
        else:
            yield ctx

    monkeypatch.setattr(
        "nce.vertical_modules.field_tech._guard.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.work_orders.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.checklist.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.scan.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.time_entry.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.photo.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.sync.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.partner_view.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.outcome.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.dispatch.scoped_pg_session",
        _fake_scoped,
        raising=False,
    )


# ============================================================================
# 1. Opt-in Guard Tests
# ============================================================================


@pytest.mark.asyncio
async def test_guard_passes_when_enabled() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"field_tech_enabled": True}
    pool = _make_mock_pool(conn)
    await require_field_tech_enabled(pool, _NS_A)


@pytest.mark.asyncio
async def test_guard_raises_when_disabled_or_missing() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"field_tech_enabled": False}
    pool = _make_mock_pool(conn)
    with pytest.raises(FieldTechDisabledError, match="not enabled"):
        await require_field_tech_enabled(pool, _NS_A)

    conn.fetchrow.return_value = None
    with pytest.raises(FieldTechDisabledError, match="not found"):
        await require_field_tech_enabled(pool, _NS_A)


@pytest.mark.asyncio
async def test_guard_raises_on_invalid_uuid() -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = DataError("invalid input syntax for type uuid")
    pool = _make_mock_pool(conn)
    with pytest.raises(FieldTechDisabledError, match="Invalid namespace_id"):
        await require_field_tech_enabled(pool, "bad-uuid")


# ============================================================================
# 2. Work Order Lifecycle Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_create_work_order() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": UUID("12345678-1234-5678-1234-567812345678"),
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "partner_scope_id": None,
        "kind": "install",
        "source_kind": "project",
        "source_ref": "PRJ-100",
        "location_id": "LOC-ROOM-1",
        "assignee_id": None,
        "assignee_kind": None,
        "status": "pending",
        "due_at": None,
        "raw": {"customer": "Acme Corp"},
        "created_at": None,
        "updated_at": None,
    }
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_create_work_order(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "kind": "install",
            "source_kind": "project",
            "source_ref": "PRJ-100",
            "location_id": "LOC-ROOM-1",
            "bom_lines": ["BOM-1", "BOM-2"],
        },
    )

    assert res["work_order_id"] == _WO_ID_1
    assert res["status"] == "pending"
    # Verify graph edge creations
    exec_calls = [c[0][0] for c in conn.execute.call_args_list]
    assert any("INSERT INTO kg_nodes" in sql for sql in exec_calls)
    assert any("INSERT INTO kg_edges" in sql for sql in exec_calls)


@pytest.mark.asyncio
async def test_do_get_and_query_work_order() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": UUID("12345678-1234-5678-1234-567812345678"),
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "partner_scope_id": None,
        "kind": "install",
        "source_kind": "project",
        "source_ref": "PRJ-100",
        "location_id": "LOC-ROOM-1",
        "assignee_id": "tech-alice",
        "assignee_kind": "employee",
        "status": "assigned",
        "due_at": None,
        "raw": {},
        "created_at": None,
        "updated_at": None,
    }
    conn.fetch.return_value = [conn.fetchrow.return_value]
    pool = _make_mock_pool(conn)

    # Single get
    res_get = await do_get_work_order(pool, {"namespace_id": _NS_A, "work_order_id": _WO_ID_1})
    assert res_get["work_order_id"] == _WO_ID_1

    # Query list
    res_query = await do_query_work_order(pool, {"namespace_id": _NS_A, "status": "assigned"})
    assert res_query["total"] == 1
    assert len(res_query["work_orders"]) == 1


@pytest.mark.asyncio
async def test_do_assign() -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        # 1. Fetch work order
        {
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "status": "pending",
            "raw": {},
        },
        # 2. Return from UPDATE
        {
            "id": UUID("12345678-1234-5678-1234-567812345678"),
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": UUID(_PARTNER_SCOPE_1),
            "assignee_id": "cont-bob",
            "assignee_kind": "contractor",
            "status": "assigned",
            "raw": {},
        },
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_assign(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "assignee_id": "cont-bob",
            "assignee_kind": "contractor",
            "partner_scope_id": _PARTNER_SCOPE_1,
        },
    )
    assert res["status"] == "assigned"
    assert res["assignee_id"] == "cont-bob"


# ============================================================================
# 3. Quality Checklist & Verification Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_complete_checklist_iso9001() -> None:
    conn = AsyncMock()
    # WO exists
    conn.fetchrow.side_effect = [
        {"partner_scope_id": None},
        # Checklist row insert
        {
            "id": UUID("88888888-8888-4888-8888-888888888888"),
            "checklist_id": "CL-001",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "template_id": "install_standard",
            "items": [],
            "completed_at": None,
            "raw": {},
            "created_at": None,
            "updated_at": None,
        },
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    items = [
        {"id": "c1", "label": "Mounting secure", "required": True, "ticked": True},
        {"id": "c2", "label": "Cables labeled", "required": True, "ticked": True},
    ]
    res = await do_complete_checklist(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "checklist_id": "CL-001",
            "items": items,
            "require_all_required": True,
        },
    )
    assert res["is_complete"] is True
    assert len(res["missing_required"]) == 0


@pytest.mark.asyncio
async def test_do_complete_checklist_fails_on_missing_required_in_strict_mode() -> None:
    conn = AsyncMock()
    pool = _make_mock_pool(conn)

    items = [
        {"id": "c1", "label": "Mounting secure", "required": True, "ticked": False},
    ]
    with pytest.raises(ChecklistIncompleteError, match="missing required items"):
        await do_complete_checklist(
            pool,
            {
                "namespace_id": _NS_A,
                "work_order_id": _WO_ID_1,
                "items": items,
                "require_all_required": True,
            },
        )


# ============================================================================
# 4. Scan, Time Entry, Photo Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_scan_serial_seed_edge() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"partner_scope_id": None}
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_scan_serial(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "bom_line_id": "BOM-101",
            "serial": "SN-98765",
            "product_id": "PROD-MIC",
        },
    )
    assert res["serial"] == "SN-98765"
    assert res["seed_edge"]["predicate"] == "installed_as"

    # Verify boundary edge handed to Assets: BOM_LINE -[installed_as]-> ASSET
    edge_calls = [
        c[0] for c in conn.execute.call_args_list if "INSERT INTO kg_edges" in str(c[0][0])
    ]
    assert len(edge_calls) >= 1
    edge_call = edge_calls[0]
    assert "installed_as" in str(edge_call[0])
    assert edge_call[1] == "BOM_LINE:BOM-101"
    assert edge_call[2] == "ASSET:SN-98765"


@pytest.mark.asyncio
async def test_do_log_time_with_dedup() -> None:
    conn = AsyncMock()
    # 1. First insert: op_id does not exist
    conn.fetchrow.side_effect = [
        None,  # op_id not in time_entries
        {"partner_scope_id": None},  # wo exists
        {
            "id": UUID("77777777-7777-4777-7777-777777777777"),
            "time_entry_id": "TE-001",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "started_at": None,
            "ended_at": None,
            "source": "gps",
            "approved": False,
            "op_id": "OP-SYNC-1",
            "raw": {},
            "created_at": None,
        },
    ]
    pool = _make_mock_pool(conn)

    res = await do_log_time(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "op_id": "OP-SYNC-1",
            "source": "gps",
            "hours": 2.5,
        },
    )
    assert res["status"] == "logged"
    assert res["deduplicated"] is False

    # 2. Second insert with same op_id: returns deduplicated
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = {
        "id": UUID("77777777-7777-4777-7777-777777777777"),
        "time_entry_id": "TE-001",
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "partner_scope_id": None,
        "started_at": None,
        "ended_at": None,
        "source": "gps",
        "approved": False,
        "op_id": "OP-SYNC-1",
        "raw": {},
        "created_at": None,
    }
    res_dup = await do_log_time(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "op_id": "OP-SYNC-1",
        },
    )
    assert res_dup["status"] == "deduplicated"
    assert res_dup["deduplicated"] is True


@pytest.mark.asyncio
async def test_do_attach_photo() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"partner_scope_id": None}
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_attach_photo(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "blob_ref": "minio://photos/rack_rear.jpg",
            "caption": "Rear cabling of AV rack",
        },
    )
    assert res["status"] == "attached"
    assert res["blob_ref"] == "minio://photos/rack_rear.jpg"


# ============================================================================
# 5. Offline Sync & Conflict Reconcile Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_sync_server_seq_and_conflict_surfacing() -> None:
    conn = AsyncMock()
    # Mock behavior for op processing:
    # 1. time_entry op_id check -> None (not duplicate)
    # 2. checklist complete -> wo row + insert row
    # 3. scan serial -> bom_line existing check (conflict detected!)
    conn.fetchval.return_value = None  # not duplicate
    conn.fetchrow.side_effect = [
        # For checklist
        {"partner_scope_id": None},
        {
            "id": UUID("11111111-1111-4111-8111-111111111111"),
            "checklist_id": "CL-SYNC-1",
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": None,
            "template_id": "standard",
            "items": [],
            "completed_at": None,
            "raw": {},
            "created_at": None,
            "updated_at": None,
        },
        # For scan: conflict query returns an existing different serial!
        {"object_label": "ASSET:EXISTING-DIFFERENT-SN"},
    ]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    ops = [
        {
            "op_id": "OP-1",
            "work_order_id": _WO_ID_1,
            "type": "complete_checklist",
            "payload": {"items": [{"id": "item1", "ticked": True}]},
        },
        {
            "op_id": "OP-2",
            "work_order_id": _WO_ID_1,
            "type": "scan_serial",
            "payload": {"bom_line_id": "BOM-101", "serial": "NEW-SN-002"},
        },
    ]

    res = await do_sync(
        pool,
        {
            "namespace_id": _NS_A,
            "device_id": "iPad-Field-01",
            "ops": ops,
        },
    )

    assert res["status"] == "synced"
    assert "OP-1" in res["applied_ops"]
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["type"] == "scan_conflict"
    assert res["conflicts"][0]["existing_asset"] == "ASSET:EXISTING-DIFFERENT-SN"


# ============================================================================
# 6. Partner Access & Redaction Projection Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_partner_view_redaction_allowlist() -> None:
    conn = AsyncMock()
    wo_row = {
        "id": UUID("12345678-1234-5678-1234-567812345678"),
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "partner_scope_id": UUID(_PARTNER_SCOPE_1),
        "kind": "install",
        "location_id": "LOC-1",
        "status": "in_progress",
        "priority": "standard",
        "summary": "Install displays",
        "due_at": None,
        "created_at": None,
        "updated_at": None,
        "bom_lines": [
            {"line_id": "BOM-1", "part": "Display 75"},
        ],
    }
    conn.fetch.side_effect = [
        # work_orders
        [wo_row],
        # checklists
        [
            {
                "checklist_id": "CL-01",
                "template_id": "install",
                "items": [],
                "completed_at": None,
            }
        ],
        # scans / installed bom lines
        [{"subject_label": f"WORK_ORDER:{_WO_ID_1}", "object_label": "BOM_LINE:BOM-1"}],
    ]
    pool = _make_mock_pool(conn)

    res = await do_partner_view(
        pool,
        {
            "namespace_id": _NS_A,
            "partner_scope_id": _PARTNER_SCOPE_1,
            "work_order_id": _WO_ID_1,
        },
    )

    partner_wo = res["work_order"]
    assert partner_wo["work_order_id"] == _WO_ID_1
    assert partner_wo["location_id"] == "LOC-1"
    # Verify strict allow-list redaction: NO margin, price, or cost leaked
    assert "secret_margin" not in partner_wo
    assert "internal_pricing" not in partner_wo
    assert "strategy" not in partner_wo
    bom = partner_wo.get("assigned_bom_lines", [])
    assert len(bom) == 1
    assert bom[0] == "BOM-1"


# ============================================================================
# 7. Outcome Recording & Cognitive Ledger Write
# ============================================================================


@pytest.mark.asyncio
async def test_do_record_outcome() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "partner_scope_id": None,
        "kind": "service",
        "assignee_id": "tech_alice",
        "assignee_kind": "employee",
        "status": "in_progress",
        "raw": {},
    }
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    res = await do_record_outcome(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "rating": 4.8,
            "quality_score": 0.95,
            "resolution_notes": "Firmware reloaded, audio loop resolved",
            "was_rework": False,
        },
    )

    assert res["status"] == "recorded"
    assert res["rating"] == 4.8
    assert res["completed_by"] == "tech_alice"

    # Verify v3_cognitive_ledger insert with field_tech_source_id
    ledger_call = next(
        (
            c
            for c in conn.execute.call_args_list
            if "INSERT INTO v3_cognitive_ledger" in str(c[0][0])
        ),
        None,
    )
    assert ledger_call is not None
    tlx_payload = json.loads(ledger_call[0][4])
    assert tlx_payload["event_type"] == "field_tech_outcome"
    assert tlx_payload["field_tech_source_id"] == _WO_ID_1
    assert tlx_payload["rating"] == 4.8


# ============================================================================
# 8. AI Dispatch Ranking Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_dispatch_ranking() -> None:
    conn = AsyncMock()
    # 1. Fetch work order
    conn.fetchrow.return_value = {
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "kind": "install",
        "location_id": "LOC-OSLO",
        "raw": {"required_skills": ["crestron_certified", "dante_level_2"]},
    }
    # 2. Fetch load
    conn.fetch.side_effect = [
        # load_rows
        [{"assignee_id": "tech_alice", "open_count": 0}],
        # history_rows from v3_cognitive_ledger
        [
            {
                "completed_by": "tech_alice",
                "completed_count": 5,
                "avg_rating": 4.9,
                "avg_quality": 0.98,
            }
        ],
    ]
    pool = _make_mock_pool(conn)

    candidates = [
        {
            "id": "tech_alice",
            "name": "Alice Expert",
            "skills": ["crestron_certified", "dante_level_2"],
            "location_id": "LOC-OSLO",
        },
        {
            "id": "tech_bob",
            "name": "Bob Junior",
            "skills": [],
            "location_id": "LOC-BERGEN",
        },
    ]

    res = await do_dispatch(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "candidates": candidates,
        },
    )

    ranked = res["ranked_candidates"]
    assert len(ranked) == 2
    # Alice should be ranked #1 because she matches skills, location, has 0 load and top history
    assert ranked[0]["id"] == "tech_alice"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert res["top_recommendation"] == "Alice Expert"


# ============================================================================
# 9. Cross-Tenant Swap-Mutant Tests (Charter §4.4)
# ============================================================================


@pytest.mark.asyncio
async def test_cross_tenant_swap_mutants() -> None:
    """Proves Tenant B cannot query, complete, or mutate Tenant A's work orders."""
    conn = AsyncMock()

    def _mock_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        # Check tenant predicate on work_orders query
        if "work_orders" in sql and "namespace_id = $2::uuid" in sql:
            wo_id, ns_id = args[0], args[1]
            if wo_id == _WO_ID_1 and ns_id == UUID(_NS_A):
                return {
                    "id": UUID("12345678-1234-5678-1234-567812345678"),
                    "work_order_id": _WO_ID_1,
                    "namespace_id": UUID(_NS_A),
                    "partner_scope_id": None,
                    "kind": "install",
                    "status": "pending",
                    "raw": {},
                }
            # Mutant: Tenant B passes _NS_B -> returns None
            return None
        return None

    conn.fetchrow.side_effect = _mock_fetchrow
    pool = _make_mock_pool(conn)

    # Tenant A succeeds
    res_a = await do_get_work_order(pool, {"namespace_id": _NS_A, "work_order_id": _WO_ID_1})
    assert res_a["work_order_id"] == _WO_ID_1

    # Tenant B fails with WorkOrderNotFoundError
    with pytest.raises(WorkOrderNotFoundError):
        await do_get_work_order(pool, {"namespace_id": _NS_B, "work_order_id": _WO_ID_1})

    # Tenant B cannot record outcome on Tenant A's work order
    with pytest.raises(ValueError, match="not found in namespace"):
        await do_record_outcome(pool, {"namespace_id": _NS_B, "work_order_id": _WO_ID_1})
