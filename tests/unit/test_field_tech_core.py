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
    mock_assert_owner = AsyncMock(return_value=None)
    for mod in ("work_orders", "checklist", "photo", "scan", "time_entry"):
        monkeypatch.setattr(
            f"nce.vertical_modules.field_tech.{mod}.assert_owner",
            mock_assert_owner,
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
    monkeypatch.setattr(
        "nce.decision_feedback.scoped_pg_session",
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
        # raw is jsonb; real Postgres hands back the raw JSON string (this
        # pool registers no jsonb codec), never a decoded dict -- a native
        # dict here would mask the bug _decode_raw fixes.
        "raw": '{"customer": "Acme Corp"}',
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
    assert res["raw"] == {"customer": "Acme Corp"}, f"raw was not decoded: {res['raw']!r}"
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
        # raw is jsonb; real Postgres hands back the raw JSON string (this
        # pool registers no jsonb codec), never a decoded dict -- a native
        # dict here would mask the bug _decode_raw fixes.
        "raw": '{"k": "v"}',
        "created_at": None,
        "updated_at": None,
    }
    # Realistic per-call shapes, not one row reused for every table: an
    # empty checklists/time_entries result for a fixture this test doesn't
    # otherwise exercise, then the real work-order row for do_query_work_order's
    # own list query. A single shared conn.fetch.return_value previously
    # handed do_get_work_order's checklists query a work-orders-shaped row
    # with no "items" key at all -- masked until the items decode fix made
    # it a real KeyError instead of a silently-ignored one.
    conn.fetch.side_effect = [
        [],  # checklists (do_get_work_order)
        [],  # time_entries (do_get_work_order)
        [conn.fetchrow.return_value],  # do_query_work_order's own list
    ]
    pool = _make_mock_pool(conn)

    # Single get
    res_get = await do_get_work_order(pool, {"namespace_id": _NS_A, "work_order_id": _WO_ID_1})
    assert res_get["work_order_id"] == _WO_ID_1
    assert res_get["raw"] == {"k": "v"}, f"raw was not decoded: {res_get['raw']!r}"

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
            "raw": "{}",
        },
        # 2. Return from UPDATE
        # raw is jsonb; real Postgres hands back the raw JSON string (this
        # pool registers no jsonb codec), never a decoded dict.
        {
            "id": UUID("12345678-1234-5678-1234-567812345678"),
            "work_order_id": _WO_ID_1,
            "namespace_id": UUID(_NS_A),
            "partner_scope_id": UUID(_PARTNER_SCOPE_1),
            "assignee_id": "cont-bob",
            "assignee_kind": "contractor",
            "status": "assigned",
            "raw": '{"note": "assigned"}',
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
    assert res["raw"] == {"note": "assigned"}, f"raw was not decoded: {res['raw']!r}"


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
            # items/raw are jsonb; real Postgres hands back the raw JSON
            # string (this pool registers no jsonb codec), never a decoded
            # dict/list -- a native [] / {} here would mask the bug the
            # decode fix closes.
            "items": '[{"id": "c1", "label": "Mounting secure", "required": true, "ticked": true}, '
            '{"id": "c2", "label": "Cables labeled", "required": true, "ticked": true}]',
            "completed_at": None,
            "raw": "{}",
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
    assert res["items"] == items, f"items was not decoded: {res['items']!r}"
    assert res["raw"] == {}, f"raw was not decoded: {res['raw']!r}"


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
        # items is jsonb; real Postgres hands back the raw JSON string
        # (this pool registers no jsonb codec), never a decoded list.
        [
            {
                "checklist_id": "CL-01",
                "template_id": "install",
                "items": "[]",
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
    assert partner_wo["checklists"][0]["items"] == [], (
        f"items was not decoded for the partner view: {partner_wo['checklists'][0]['items']!r}"
    )
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


@pytest.mark.asyncio
async def test_dispatch_load_statuses_and_outcome_employee_hr_routing() -> None:
    """do_dispatch includes scheduled/dispatched in load query and do_record_outcome routes employee to hr."""
    from unittest.mock import patch

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {},
    }
    conn.fetch.side_effect = [[], []]
    pool = _make_mock_pool(conn)

    await do_dispatch(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "candidates": [{"id": "emp-1", "name": "Emp 1", "skills": []}],
        },
    )
    load_sql = conn.fetch.call_args_list[0][0][0]
    assert "'scheduled'" in load_sql and "'dispatched'" in load_sql

    conn_out = AsyncMock()
    conn_out.fetchrow.return_value = {
        "id": UUID("12345678-1234-5678-1234-567812345678"),
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "partner_scope_id": None,
        "kind": "service",
        "assignee_id": "emp-1",
        "assignee_kind": "employee",
        "status": "in_progress",
        "raw": "{}",
    }
    conn_out.execute = AsyncMock(return_value="INSERT 1")
    pool_out = _make_mock_pool(conn_out)
    engine_out = MagicMock()
    engine_out.pg_pool = pool_out
    engine_out.modules = {"hr": MagicMock()}

    with patch(
        "nce.vertical_modules.field_tech.outcome.record_decision_feedback",
        new_callable=AsyncMock,
        return_value={"id": "df-1"},
    ):
        res_out = await do_record_outcome(
            engine_out,
            {"namespace_id": _NS_A, "work_order_id": _WO_ID_1, "rating": 5.0},
        )
    assert "hr" in res_out["routed_engines"]


@pytest.mark.asyncio
async def test_dispatch_penalizes_technician_rework_history() -> None:
    """Technician with high rework rate has quality score discounted vs clean history."""
    wo_id = "WO-REWORK-001"

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": wo_id,
        "namespace_id": UUID(_NS_A),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {"required_skills": ["cctv"]},
    }

    conn.fetch.side_effect = [
        [
            {"assignee_id": "tech_clean", "open_count": 0},
            {"assignee_id": "tech_rework", "open_count": 0},
        ],
        [
            {
                "completed_by": "tech_clean",
                "completed_count": 5,
                "avg_rating": 4.8,
                "avg_quality": 0.95,
                "rework_count": 0,
            },
            {
                "completed_by": "tech_rework",
                "completed_count": 5,
                "avg_rating": 4.8,
                "avg_quality": 0.95,
                "rework_count": 3,
            },
        ],
    ]

    pool = _make_mock_pool(conn)

    candidates = [
        {
            "id": "tech_clean",
            "name": "Clean Tech",
            "skills": ["cctv"],
            "location_id": "LOC-OSLO",
        },
        {
            "id": "tech_rework",
            "name": "Rework Tech",
            "skills": ["cctv"],
            "location_id": "LOC-OSLO",
        },
    ]

    res = await do_dispatch(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": wo_id,
            "candidates": candidates,
        },
    )

    ranked = res["ranked_candidates"]
    assert len(ranked) == 2
    assert ranked[0]["id"] == "tech_clean"
    assert ranked[1]["id"] == "tech_rework"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["component_scores"]["history"] > ranked[1]["component_scores"]["history"]
    assert "reworks" in ranked[1]["rationale"]


# ============================================================================
# 10. Regulatory Compliance & Dispatch Gates (Wave E-4: FEK, FSE, DSB, NKOM, ISO)
# ============================================================================


def test_regulatory_cert_rules_metadata() -> None:
    """Verify REGULATORY_CERT_RULES codifies Norwegian, Maritime and European standards."""
    from nce.vertical_modules.hr.certs import REGULATORY_CERT_RULES

    assert "FSE_LAVSPENNING" in REGULATORY_CERT_RULES
    fse_lv = REGULATORY_CERT_RULES["FSE_LAVSPENNING"]
    assert fse_lv["max_validity_days"] == 365
    assert "FSE § 7" in fse_lv["standard"]
    assert fse_lv["requires_first_aid"] is True

    assert "FAGBREV_GR_L" in REGULATORY_CERT_RULES
    assert "FEK § 6" in REGULATORY_CERT_RULES["FAGBREV_GR_L"]["standard"]

    assert "FEK_INSTALLATOR" in REGULATORY_CERT_RULES
    assert "FEK § 7" in REGULATORY_CERT_RULES["FEK_INSTALLATOR"]["standard"]

    assert "NKOM_ENA" in REGULATORY_CERT_RULES
    assert "Ekomforskriften" in REGULATORY_CERT_RULES["NKOM_ENA"]["standard"]

    assert "ATEX_IECEX_COMPEX" in REGULATORY_CERT_RULES
    assert REGULATORY_CERT_RULES["ATEX_IECEX_COMPEX"]["max_validity_days"] == 1825

    assert "DNV_MARINE_ELECTRICAL" in REGULATORY_CERT_RULES
    assert "DNV" in REGULATORY_CERT_RULES["DNV_MARINE_ELECTRICAL"]["standard"]


def test_technician_compliance_electrical_lv_valid() -> None:
    """Technician holding valid FSE (<365 days) and Fagbrev Gr. L is compliant."""
    from datetime import date, timedelta

    from nce.vertical_modules.hr.certs import validate_technician_regulatory_compliance

    ref_date = date(2026, 10, 16)
    certs = [
        {
            "name": "FSE_LAVSPENNING",
            "issued": ref_date - timedelta(days=90),
            "valid_to": ref_date + timedelta(days=275),
        },
        {"name": "FAGBREV_GR_L", "issued": "2018-06-15"},
    ]
    res = validate_technician_regulatory_compliance(certs, "electrical_lv", ref_date=ref_date)
    assert res["compliant"] is True
    assert len(res["reasons"]) == 0
    assert len(res["verified_certs"]) == 2


def test_technician_compliance_fse_365_days_expired_under_fse_section_7() -> None:
    """FSE completed >365 days ago expires under FSE § 7 / EN 50110-1 even with longer nominal validity."""
    from datetime import date, timedelta

    from nce.vertical_modules.hr.certs import validate_technician_regulatory_compliance

    ref_date = date(2026, 10, 16)
    certs = [
        {
            "name": "FSE_LAVSPENNING",
            "issued": ref_date - timedelta(days=400),
            "valid_to": ref_date + timedelta(days=365),  # Nominal future expiry entered in error
        },
        {"name": "FAGBREV_GR_L"},
    ]
    res = validate_technician_regulatory_compliance(certs, "electrical_lv", ref_date=ref_date)
    assert res["compliant"] is False
    assert "FSE_LAVSPENNING" in res["expired_certs"]
    assert any("FSE § 7" in r and "365 days" in r for r in res["reasons"])


def test_technician_compliance_missing_trade_qualification() -> None:
    """Technician with active FSE but lacking FEK § 6 trade cert is non-compliant."""
    from datetime import date, timedelta

    from nce.vertical_modules.hr.certs import validate_technician_regulatory_compliance

    ref_date = date(2026, 10, 16)
    certs = [
        {"name": "FSE_LAVSPENNING", "issued": ref_date - timedelta(days=30)},
    ]
    res = validate_technician_regulatory_compliance(certs, "electrical_lv", ref_date=ref_date)
    assert res["compliant"] is False
    assert "FAGBREV_GR_L" in res["missing_certs"]
    assert any("Missing required certificate" in r for r in res["reasons"])


def test_technician_compliance_electrical_hv() -> None:
    """Scope electrical_hv requires FSE Høyspenning and Fagbrev Gr. H."""
    from datetime import date, timedelta

    from nce.vertical_modules.hr.certs import validate_technician_regulatory_compliance

    ref_date = date(2026, 10, 16)
    # Lacks Gr. H (only Gr. L)
    certs_lv_only = [
        {"name": "FSE_HOYSPENNING", "issued": ref_date - timedelta(days=30)},
        {"name": "FAGBREV_GR_L"},
    ]
    res_lv = validate_technician_regulatory_compliance(
        certs_lv_only, "electrical_hv", ref_date=ref_date
    )
    assert res_lv["compliant"] is False

    # Proper HV qualifications
    certs_hv = [
        {"name": "FSE_HOYSPENNING", "issued": ref_date - timedelta(days=30)},
        {"name": "FAGBREV_GR_H"},
    ]
    res_hv = validate_technician_regulatory_compliance(certs_hv, "electrical_hv", ref_date=ref_date)
    assert res_hv["compliant"] is True


def test_technician_compliance_nkom_atex_and_dnv() -> None:
    """Validate Ekom, ATEX (1825 day validity), and DNV Maritime scopes."""
    from datetime import date, timedelta

    from nce.vertical_modules.hr.certs import validate_technician_regulatory_compliance

    ref_date = date(2026, 10, 16)

    # NKOM ENA
    res_nkom = validate_technician_regulatory_compliance(
        [{"name": "NKOM_ENA"}], "ekom", ref_date=ref_date
    )
    assert res_nkom["compliant"] is True

    # ATEX / CompEx valid within 5 years (1825 days)
    res_atex_ok = validate_technician_regulatory_compliance(
        [{"name": "ATEX_IECEX_COMPEX", "issued": ref_date - timedelta(days=1000)}],
        "atex_ex",
        ref_date=ref_date,
    )
    assert res_atex_ok["compliant"] is True

    # ATEX / CompEx expired beyond 5 years
    res_atex_exp = validate_technician_regulatory_compliance(
        [{"name": "ATEX_IECEX_COMPEX", "issued": ref_date - timedelta(days=1900)}],
        "atex_ex",
        ref_date=ref_date,
    )
    assert res_atex_exp["compliant"] is False
    assert "ATEX_IECEX_COMPEX" in res_atex_exp["expired_certs"]

    # DNV Marine Electrical
    res_dnv = validate_technician_regulatory_compliance(
        [{"name": "DNV_MARINE_ELECTRICAL"}],
        "dnv_marine",
        ref_date=ref_date,
    )
    assert res_dnv["compliant"] is True


def test_contractor_compliance_electrical_dsb_and_fa() -> None:
    """Contractor must be registered in DSB Elvirksomhetsregisteret and have Faglig Ansvarlig."""
    from nce.vertical_modules.vendors.certs import validate_contractor_regulatory_compliance

    # Compliant contractor
    ok_meta = {"dsb_elvirksomhet_registered": True, "faglig_ansvarlig": "Ola Nordmann"}
    res_ok = validate_contractor_regulatory_compliance(ok_meta, [], "electrical_lv")
    assert res_ok["compliant"] is True

    # Missing DSB registration
    no_dsb = {"dsb_elvirksomhet_registered": False, "faglig_ansvarlig": "Ola Nordmann"}
    res_no_dsb = validate_contractor_regulatory_compliance(no_dsb, [], "electrical_lv")
    assert res_no_dsb["compliant"] is False
    assert any("DSB Elvirksomhetsregisteret" in r for r in res_no_dsb["reasons"])

    # Missing Faglig Ansvarlig
    no_fa = {"dsb_elvirksomhet_registered": True, "faglig_ansvarlig": ""}
    res_no_fa = validate_contractor_regulatory_compliance(no_fa, [], "electrical_lv")
    assert res_no_fa["compliant"] is False
    assert any("Faglig Ansvarlig" in r for r in res_no_fa["reasons"])


def test_contractor_compliance_ekom_dnv_and_iso() -> None:
    """Contractor Ekom, DNV Marine, and ISO 9001/14001/45001 verification."""
    from datetime import date, timedelta

    from nce.vertical_modules.vendors.certs import validate_contractor_regulatory_compliance

    ref_date = date(2026, 10, 16)

    # Ekom scope with active cert
    res_ekom = validate_contractor_regulatory_compliance(
        {},
        [{"name": "NKOM_ENA", "valid_to": ref_date + timedelta(days=100)}],
        "ekom",
        ref_date=ref_date,
    )
    assert res_ekom["compliant"] is True

    # DNV scope with DNV Approved Supplier
    res_dnv = validate_contractor_regulatory_compliance(
        {"dnv_approved_supplier": True},
        [],
        "dnv_marine",
        ref_date=ref_date,
    )
    assert res_dnv["compliant"] is True

    # ISO requirements: all active
    iso_certs = [
        {"name": "ISO_9001", "valid_to": ref_date + timedelta(days=200)},
        {"name": "ISO_14001", "valid_to": ref_date + timedelta(days=200)},
        {"name": "ISO_45001", "valid_to": ref_date + timedelta(days=200)},
    ]
    res_iso_ok = validate_contractor_regulatory_compliance(
        {"dsb_elvirksomhet_registered": True, "faglig_ansvarlig": "Kari Nordmann"},
        iso_certs,
        "electrical_lv",
        require_iso=["ISO_9001", "ISO_14001", "ISO_45001"],
        ref_date=ref_date,
    )
    assert res_iso_ok["compliant"] is True

    # ISO requirements: missing ISO_45001
    res_iso_missing = validate_contractor_regulatory_compliance(
        {"dsb_elvirksomhet_registered": True, "faglig_ansvarlig": "Kari Nordmann"},
        iso_certs[:2],
        "electrical_lv",
        require_iso=["ISO_9001", "ISO_14001", "ISO_45001"],
        ref_date=ref_date,
    )
    assert res_iso_missing["compliant"] is False
    assert any("ISO_45001" in r for r in res_iso_missing["reasons"])

    # ISO requirements: expired ISO_14001
    iso_certs_expired = [
        {"name": "ISO_9001", "valid_to": ref_date + timedelta(days=200)},
        {"name": "ISO_14001", "valid_to": ref_date - timedelta(days=5)},
        {"name": "ISO_45001", "valid_to": ref_date + timedelta(days=200)},
    ]
    res_iso_exp = validate_contractor_regulatory_compliance(
        {"dsb_elvirksomhet_registered": True, "faglig_ansvarlig": "Kari Nordmann"},
        iso_certs_expired,
        "electrical_lv",
        require_iso=["ISO_9001", "ISO_14001", "ISO_45001"],
        ref_date=ref_date,
    )
    assert res_iso_exp["compliant"] is False
    assert any("ISO_14001" in r for r in res_iso_exp["reasons"])


@pytest.mark.asyncio
async def test_dispatch_regulatory_scope_disqualification_and_ranking() -> None:
    """do_dispatch disqualifies non-compliant candidates and records them in disqualified_candidates."""
    from datetime import date, timedelta

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {"regulatory_scope": "electrical_lv"},
    }
    # Load and history queries return empty
    conn.fetch.side_effect = [[], []]
    pool = _make_mock_pool(conn)

    today = date.today()

    candidates = [
        # 1. Compliant Employee
        {
            "id": "emp_compliant",
            "kind": "employee",
            "name": "Compliant Employee",
            "certs": [
                {"name": "FSE_LAVSPENNING", "issued": today - timedelta(days=60)},
                {"name": "FAGBREV_GR_L"},
            ],
            "skills": ["electrical"],
            "location_id": "LOC-OSLO",
        },
        # 2. Expired FSE Employee (>365 days under FSE § 7)
        {
            "id": "emp_expired_fse",
            "kind": "employee",
            "name": "Expired FSE Employee",
            "certs": [
                {"name": "FSE_LAVSPENNING", "issued": today - timedelta(days=400)},
                {"name": "FAGBREV_GR_L"},
            ],
            "skills": ["electrical"],
            "location_id": "LOC-OSLO",
        },
        # 3. Contractor with missing DSB Elvirksomhetsregisteret
        {
            "id": "cont_unregistered",
            "kind": "contractor",
            "name": "Unregistered Contractor",
            "certs": [
                {"name": "FSE_LAVSPENNING", "issued": today - timedelta(days=30)},
                {"name": "FAGBREV_GR_L"},
            ],
            "contractor_meta": {
                "dsb_elvirksomhet_registered": False,
                "faglig_ansvarlig": "Ola",
            },
            "skills": ["electrical"],
            "location_id": "LOC-OSLO",
        },
        # 4. Compliant Contractor
        {
            "id": "cont_compliant",
            "kind": "contractor",
            "name": "Compliant Contractor",
            "certs": [
                {"name": "FSE_LAVSPENNING", "issued": today - timedelta(days=30)},
                {"name": "FAGBREV_GR_L"},
            ],
            "contractor_meta": {
                "dsb_elvirksomhet_registered": True,
                "faglig_ansvarlig": "Per Hansen",
            },
            "skills": ["electrical"],
            "location_id": "LOC-OSLO",
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

    ranked_ids = [c["id"] for c in res["ranked_candidates"]]
    assert "emp_compliant" in ranked_ids
    assert "cont_compliant" in ranked_ids
    assert "emp_expired_fse" not in ranked_ids
    assert "cont_unregistered" not in ranked_ids

    assert "disqualified_candidates" in res
    disq_map = {d["id"]: d["reasons"] for d in res["disqualified_candidates"]}
    assert "emp_expired_fse" in disq_map
    assert any("FSE § 7" in r or "365 days" in r for r in disq_map["emp_expired_fse"])

    assert "cont_unregistered" in disq_map
    assert any("DSB Elvirksomhetsregisteret" in r for r in disq_map["cont_unregistered"])


@pytest.mark.asyncio
async def test_dispatch_all_disqualified_returns_empty_ranked() -> None:
    """When no candidates satisfy regulatory scope, ranked_candidates is empty and top_recommendation is None."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {},
    }
    pool = _make_mock_pool(conn)

    candidates = [
        {
            "id": "tech_no_certs",
            "kind": "employee",
            "name": "No Certs Tech",
            "certs": [],
        },
    ]

    res = await do_dispatch(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "regulatory_scope": "electrical_lv",
            "candidates": candidates,
        },
    )

    assert len(res["ranked_candidates"]) == 0
    assert res["top_recommendation"] == "None"
    assert len(res["disqualified_candidates"]) == 1
    assert res["disqualified_candidates"][0]["id"] == "tech_no_certs"


@pytest.mark.asyncio
async def test_dispatch_without_regulatory_scope_preserves_legacy_ranking() -> None:
    """When regulatory_scope is omitted, no disqualification occurs and backward-compatible dict is returned."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": _WO_ID_1,
        "namespace_id": UUID(_NS_A),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {},
    }
    conn.fetch.side_effect = [[], []]
    pool = _make_mock_pool(conn)

    candidates = [
        {"id": "tech_plain", "name": "Plain Tech", "skills": []},
    ]

    res = await do_dispatch(
        pool,
        {
            "namespace_id": _NS_A,
            "work_order_id": _WO_ID_1,
            "candidates": candidates,
        },
    )

    assert len(res["ranked_candidates"]) == 1
    assert "disqualified_candidates" not in res
    assert res["top_recommendation"] == "Plain Tech"


@pytest.mark.asyncio
async def test_resources_planner_do_recommend_resources_regulatory_filtering() -> None:
    """do_recommend_resources filters out non-compliant resources when regulatory_scope is provided."""
    from unittest.mock import patch
    from uuid import uuid4

    from nce.vertical_modules.resources.planner import do_recommend_resources

    res_compliant_id = uuid4()
    res_non_compliant_id = uuid4()
    ns_id = uuid4()

    mock_rows = [
        {
            "id": res_compliant_id,
            "kind": "employee",
            "ref_id": "TECH-01",
            "display_name": "Compliant Electrical Tech",
            "attrs": {
                "skills": ["electrical"],
                "certs": [
                    {"name": "FSE_LAVSPENNING", "issued": "2026-05-01"},
                    {"name": "FAGBREV_GR_L"},
                ],
                "base_location": "Oslo",
            },
        },
        {
            "id": res_non_compliant_id,
            "kind": "employee",
            "ref_id": "TECH-02",
            "display_name": "Non-compliant Tech",
            "attrs": {
                "skills": ["electrical"],
                "certs": [],
                "base_location": "Oslo",
            },
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = mock_rows
    mock_conn.fetchval.return_value = None
    mock_conn.fetchrow.return_value = {"total_jobs": 0, "avg_rating": None, "avg_quality": None}

    class MockContextManager:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.resources.planner.scoped_pg_session",
            return_value=MockContextManager(),
        ),
        patch(
            "nce.vertical_modules.resources.planner.require_resources_enabled",
            return_value=None,
        ),
    ):
        result = await do_recommend_resources(
            engine,
            {
                "namespace_id": str(ns_id),
                "demand_kind": "service",
                "required_kinds": ["employee"],
                "regulatory_scope": "electrical_lv",
                "starts_at": "2026-10-25T08:00:00Z",
                "ends_at": "2026-10-25T16:00:00Z",
                "auto_reserve": False,
            },
        )

        assert result["status"] == "suggested"
        assert len(result["plan"]) == 1
        winner = result["plan"][0]
        assert winner["resource_id"] == str(res_compliant_id)
