"""
tests/unit/test_procurement_po_line_model.py
============================================
Unit test suite for Wave PR-2:
  - POLineStatus enum and transition rules
  - Canonical po_line_label formatting
  - Contract-A ownership registration for PO_LINE under procurement
  - upsert_po_line_node graph and relational write path
  - update_po_line_status transition validation and C4 graph event emission
  - Event payload contract compatibility with project/automation.py
  - Migration 074, schema.sql, and EXPECTED_TENANT_RLS_TABLES RLS registration
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.vertical_modules.procurement import (
    ALLOWED_TRANSITIONS,
    POLineStatus,
    po_line_label,
    update_po_line_status,
    upsert_po_line_node,
    validate_status_transition,
)
from nce.vertical_modules.project.automation import _handle_po_status_changed

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. POLineStatus enum and transition state machine
# ---------------------------------------------------------------------------
def test_po_line_status_enum_values():
    """Verify POLineStatus enum values."""
    assert POLineStatus.DRAFT.value == "DRAFT"
    assert POLineStatus.ORDERED.value == "ORDERED"
    assert POLineStatus.RECEIVED.value == "RECEIVED"
    assert POLineStatus.CANCELLED.value == "CANCELLED"


def test_po_line_allowed_transitions_matrix():
    """Verify the allowed transition mapping."""
    assert ALLOWED_TRANSITIONS[POLineStatus.DRAFT] == frozenset(
        {POLineStatus.ORDERED, POLineStatus.CANCELLED}
    )
    assert ALLOWED_TRANSITIONS[POLineStatus.ORDERED] == frozenset(
        {POLineStatus.RECEIVED, POLineStatus.CANCELLED}
    )
    assert ALLOWED_TRANSITIONS[POLineStatus.RECEIVED] == frozenset()
    assert ALLOWED_TRANSITIONS[POLineStatus.CANCELLED] == frozenset()


@pytest.mark.parametrize(
    "current,nxt",
    [
        (POLineStatus.DRAFT, POLineStatus.ORDERED),
        (POLineStatus.DRAFT, POLineStatus.CANCELLED),
        (POLineStatus.ORDERED, POLineStatus.RECEIVED),
        (POLineStatus.ORDERED, POLineStatus.CANCELLED),
        ("DRAFT", "ORDERED"),
        ("draft", "ordered"),
        ("ORDERED", "RECEIVED"),
        ("ordered", "cancelled"),
        (POLineStatus.DRAFT, POLineStatus.DRAFT),
        (POLineStatus.ORDERED, POLineStatus.ORDERED),
        (POLineStatus.RECEIVED, POLineStatus.RECEIVED),
        (POLineStatus.CANCELLED, POLineStatus.CANCELLED),
    ],
)
def test_validate_status_transition_valid(current, nxt):
    """Valid transitions (including idempotent no-ops) must succeed without error."""
    validate_status_transition(current, nxt)


@pytest.mark.parametrize(
    "current,nxt",
    [
        (POLineStatus.DRAFT, POLineStatus.RECEIVED),
        (POLineStatus.ORDERED, POLineStatus.DRAFT),
        (POLineStatus.RECEIVED, POLineStatus.DRAFT),
        (POLineStatus.RECEIVED, POLineStatus.ORDERED),
        (POLineStatus.RECEIVED, POLineStatus.CANCELLED),
        (POLineStatus.CANCELLED, POLineStatus.DRAFT),
        (POLineStatus.CANCELLED, POLineStatus.ORDERED),
        (POLineStatus.CANCELLED, POLineStatus.RECEIVED),
    ],
)
def test_validate_status_transition_invalid(current, nxt):
    """Disallowed transitions must raise ValueError."""
    with pytest.raises(ValueError, match="Invalid PO_LINE status transition"):
        validate_status_transition(current, nxt)


def test_validate_status_transition_unknown_status():
    """Unknown statuses must raise ValueError."""
    with pytest.raises(ValueError, match="Unknown current PO_LINE status"):
        validate_status_transition("NON_EXISTENT", "ORDERED")

    with pytest.raises(ValueError, match="Unknown target PO_LINE status"):
        validate_status_transition("DRAFT", "INVALID_TARGET")


# ---------------------------------------------------------------------------
# 2. Canonical PO_LINE label formatting
# ---------------------------------------------------------------------------
def test_po_line_label_format():
    """Verify canonical label format PO_LINE:<PO_NUMBER>:<LINE_REF>."""
    lbl = po_line_label("PO-2026-001", "LINE-01")
    assert lbl == "PO_LINE:PO-2026-001:LINE-01"

    # Normalizes to uppercase
    lbl_lower = po_line_label("po-100", "line-2")
    assert lbl_lower == "PO_LINE:PO-100:LINE-2"


# ---------------------------------------------------------------------------
# 3. Contract-A ownership registration
# ---------------------------------------------------------------------------
def test_node_ownership_json_declares_procurement_po_line():
    """node-ownership.json must declare procurement as owner of PO_LINE with transitions."""
    config_path = REPO_ROOT / "nce" / "config_data" / "node-ownership.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))

    ownership_list = data["ownership"]
    po_line_entries = [e for e in ownership_list if e.get("node_type") == "PO_LINE"]
    assert len(po_line_entries) == 5, (
        f"Expected 5 PO_LINE ownership entries, found {len(po_line_entries)}"
    )

    for entry in po_line_entries:
        assert entry["owner_engine"] == "procurement"

    transitions = {e.get("transition") for e in po_line_entries}
    expected_transitions = {
        None,
        "status:draft",
        "status:ordered",
        "status:received",
        "status:cancelled",
    }
    assert transitions == expected_transitions


@pytest.mark.asyncio
async def test_assert_owner_for_po_line():
    """assert_owner must succeed for procurement and fail for other engines."""
    ns = uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"owner_engine": "procurement"}

    # When procurement asserts ownership
    await assert_owner(mock_conn, ns, "PO_LINE", "procurement", "status:draft")
    await assert_owner(mock_conn, ns, "PO_LINE", "procurement", "status:ordered")

    # When an unauthorized engine asserts ownership, it must raise OwnershipError
    with pytest.raises(OwnershipError):
        await assert_owner(mock_conn, ns, "PO_LINE", "sales", "status:draft")

    with pytest.raises(OwnershipError):
        await assert_owner(mock_conn, ns, "PO_LINE", "inventory", "status:ordered")

    # Deny by default when no row registered
    mock_conn.fetchrow.return_value = None
    with pytest.raises(OwnershipError):
        await assert_owner(mock_conn, ns, "PO_LINE", "procurement", "status:draft")


# ---------------------------------------------------------------------------
# 4. upsert_po_line_node graph and relational operations
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upsert_po_line_node_executes_writes():
    """upsert_po_line_node writes kg_nodes, kg_edges, and procurement_po_lines."""
    ns = uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"owner_engine": "procurement"}

    res = await upsert_po_line_node(
        mock_conn,
        ns,
        po_number="PO-100",
        line_ref="LINE-1",
        bom_line_label="BOM_LINE:SYS-1:BL-01",
        project_id="PROJ-001",
        artnr="ART-42",
        description="Ceiling Speaker",
        quantity=4.0,
        unit_price=1250.0,
        currency="NOK",
        status=POLineStatus.DRAFT,
    )

    assert res["po_number"] == "PO-100"
    assert res["line_ref"] == "LINE-1"
    assert res["po_line_label"] == "PO_LINE:PO-100:LINE-1"
    assert res["status"] == "DRAFT"
    assert res["bom_line_label"] == "BOM_LINE:SYS-1:BL-01"
    assert res["project_id"] == "PROJ-001"

    # Verify calls to mock_conn.execute: kg_nodes, PO edge, BOM_LINE edge, relational table
    assert mock_conn.execute.call_count >= 4


# ---------------------------------------------------------------------------
# 5. update_po_line_status transition validation and event emission
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_po_line_status_valid_transition():
    """update_po_line_status validates transition, updates DB, and emits C4 event."""
    ns = uuid4()
    mock_conn = AsyncMock()

    async def fetchrow_side_effect(query, *args):
        if "procurement_po_lines" in query:
            return {
                "status": "DRAFT",
                "project_id": "PROJ-777",
                "bom_line_label": "BOM_LINE:DES-1:BL-99",
            }
        if "node_ownership_registry" in query:
            return {"owner_engine": "procurement"}
        return None

    mock_conn.fetchrow.side_effect = fetchrow_side_effect

    with patch(
        "nce.vertical_modules.procurement.po_line.emit_graph_write", new_callable=AsyncMock
    ) as mock_emit:
        res = await update_po_line_status(
            mock_conn,
            ns,
            po_number="PO-999",
            line_ref="L-1",
            new_status=POLineStatus.ORDERED,
            project_value=50000.0,
        )

        assert res["status"] == "ORDERED"
        assert res["project_id"] == "PROJ-777"
        assert res["bom_line_label"] == "BOM_LINE:DES-1:BL-99"
        assert res["project_value"] == 50000.0

        # Verify emit_graph_write was called with correct parameters
        mock_emit.assert_awaited_once()
        args, kwargs = mock_emit.call_args
        # emit_graph_write(conn, ns, node_type, op, payload=...)
        assert args[1] == ns
        assert args[2] == "PO_LINE"
        assert args[3] == "status_changed"

        payload = kwargs["payload"]
        assert payload["po_number"] == "PO-999"
        assert payload["line_ref"] == "L-1"
        assert payload["status"] == "ORDERED"
        assert payload["project_id"] == "PROJ-777"
        assert payload["bom_line_label"] == "BOM_LINE:DES-1:BL-99"
        assert payload["id"] == "BOM_LINE:DES-1:BL-99"
        assert payload["project_value"] == 50000.0


@pytest.mark.asyncio
async def test_update_po_line_status_invalid_transition_raises():
    """update_po_line_status raises ValueError and aborts write on invalid transition."""
    ns = uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "status": "RECEIVED",
        "project_id": "PROJ-1",
        "bom_line_label": "BOM_LINE:BL-1",
    }

    with patch(
        "nce.vertical_modules.procurement.po_line.emit_graph_write", new_callable=AsyncMock
    ) as mock_emit:
        with pytest.raises(ValueError, match="Invalid PO_LINE status transition"):
            await update_po_line_status(
                mock_conn,
                ns,
                po_number="PO-1",
                line_ref="L-1",
                new_status=POLineStatus.DRAFT,
            )

        # Ensure emit_graph_write was NOT called
        mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Event contract compatibility with project/automation.py
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_emitted_event_payload_satisfies_project_automation():
    """Verify that payload emitted by update_po_line_status passes _handle_po_status_changed."""
    ns = uuid4()
    mock_conn = AsyncMock()

    async def fetchrow_side_effect(query, *args):
        if "procurement_po_lines" in query:
            return {
                "status": "DRAFT",
                "project_id": "PROJ-101",
                "bom_line_label": "BOM_LINE:PRJ-101:BL-05",
            }
        if "node_ownership_registry" in query:
            return {"owner_engine": "procurement"}
        return None

    mock_conn.fetchrow.side_effect = fetchrow_side_effect

    captured_event = {}

    async def fake_emit_graph_write(conn, ns_val, node_type, op, payload=None):
        captured_event["event"] = {
            "namespace_id": str(ns_val),
            "node_type": node_type,
            "op": op,
            "payload": payload,
        }

    with patch(
        "nce.vertical_modules.procurement.po_line.emit_graph_write",
        side_effect=fake_emit_graph_write,
    ):
        await update_po_line_status(
            mock_conn,
            ns,
            po_number="PO-200",
            line_ref="L-10",
            new_status=POLineStatus.ORDERED,
            project_value=120000.0,
        )

    assert "event" in captured_event
    event = captured_event["event"]

    # Test that project/automation._handle_po_status_changed accepts this event
    action = await _handle_po_status_changed(mock_conn, event)
    # Should return a post-commit action (not None / skipped)
    assert action is not None
    assert callable(action)

    with patch("nce.vertical_modules.project.automation._enqueue_rq_task") as mock_enqueue:
        action()
        mock_enqueue.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Schema, Migration, and RLS registration assertions
# ---------------------------------------------------------------------------
def test_procurement_po_lines_registered_in_expected_tenant_rls_tables():
    """procurement_po_lines must be registered in EXPECTED_TENANT_RLS_TABLES with namespace_id."""
    assert "procurement_po_lines" in EXPECTED_TENANT_RLS_TABLES
    assert EXPECTED_TENANT_RLS_TABLES["procurement_po_lines"] == "namespace_id"


def test_migration_074_and_schema_sql_contain_po_lines_table():
    """Migration 074 and schema.sql must both define procurement_po_lines with RLS enabled."""
    mig_file = REPO_ROOT / "nce" / "migrations" / "074_procurement_po_lines.sql"
    schema_file = REPO_ROOT / "nce" / "schema.sql"

    assert mig_file.exists(), f"Missing migration file: {mig_file}"
    mig_text = mig_file.read_text(encoding="utf-8")
    schema_text = schema_file.read_text(encoding="utf-8")

    for text, src in [(mig_text, "074_procurement_po_lines.sql"), (schema_text, "schema.sql")]:
        assert "CREATE TABLE IF NOT EXISTS procurement_po_lines" in text, (
            f"Table DDL missing in {src}"
        )
        assert "ALTER TABLE procurement_po_lines ENABLE ROW LEVEL SECURITY" in text, (
            f"RLS enable missing in {src}"
        )
        assert "tenant_isolation_policy ON procurement_po_lines" in text, f"Policy missing in {src}"
