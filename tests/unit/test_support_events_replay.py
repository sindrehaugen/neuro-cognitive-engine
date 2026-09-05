"""Unit tests for Support Engine events, replay handlers, and Contract-A node ownership.

ML10-B7: Phase 5 - Events, Replay & Graph Ownership.
Verifies:
  1. EventType and VALID_EVENT_TYPES include support_ticket_opened,
     support_ticket_resolved, support_touchpoint_recorded, support_diagnosis_authored.
  2. ForkedReplay handler registry coverage (_validate_handler_coverage) includes all support events.
  3. Replay handler for support events returns expected provenance replay dict.
  4. Contract-A node ownership in nce/config_data/node-ownership.json:
     - TICKET, SLA, SUPPORT_HEALTH_SCORE, SUPPORT_DIAGNOSIS are registered with owner_engine="support"
       and transition=null.
     - Single-transition ratchet: none of these 4 node types have non-null transition rows.
  5. assert_owner / OwnershipError semantics against registered support ownership rules:
     - 'support' engine passes.
     - other engines ('sales', 'assets', 'field_tech') are refused with OwnershipError.
     - unregistered node types are refused under deny-by-default with owner_engine=None.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.entity_resolution.ownership import OwnershipError
from nce.event_types import VALID_EVENT_TYPES, EventType
from nce.replay import (
    _HANDLER_REGISTRY,
    _EventRow,
    _validate_handler_coverage,
)

_OWNERSHIP_MAP_PATH = (
    Path(__file__).resolve().parent.parent.parent / "nce" / "config_data" / "node-ownership.json"
)

_SUPPORT_EVENT_TYPES = frozenset(
    {
        "support_ticket_opened",
        "support_ticket_resolved",
        "support_touchpoint_recorded",
        "support_diagnosis_authored",
    }
)

_SUPPORT_NODE_TYPES = frozenset(
    {
        "TICKET",
        "SLA",
        "SUPPORT_HEALTH_SCORE",
        "SUPPORT_DIAGNOSIS",
    }
)


def test_support_event_types_defined_in_event_type_union() -> None:
    """All 4 support event types must be members of EventType and VALID_EVENT_TYPES."""
    all_types = frozenset(get_args(EventType))
    missing = _SUPPORT_EVENT_TYPES - all_types
    assert not missing, f"EventType missing support event types: {sorted(missing)}"

    missing_valid = _SUPPORT_EVENT_TYPES - VALID_EVENT_TYPES
    assert not missing_valid, f"VALID_EVENT_TYPES missing: {sorted(missing_valid)}"


def test_support_replay_handler_coverage() -> None:
    """_validate_handler_coverage must pass and all support event types must have handlers in _HANDLER_REGISTRY."""
    # Must not raise ReplayHandlerMissingError
    _validate_handler_coverage()

    for et in _SUPPORT_EVENT_TYPES:
        assert et in _HANDLER_REGISTRY, f"No replay handler registered for {et!r}"


@pytest.mark.asyncio
async def test_support_replay_handler_execution() -> None:
    """Invoking the registered replay handler for support events returns provenance result."""
    from datetime import datetime, timezone

    conn = MagicMock()
    ctx = uuid4()
    for et in _SUPPORT_EVENT_TYPES:
        handler = _HANDLER_REGISTRY[et]
        row = _EventRow(
            event_id=uuid4(),
            event_seq=42,
            event_type=et,
            occurred_at=datetime.now(timezone.utc),
            agent_id="support-agent",
            params={"ticket_id": str(uuid4()), "detail": "test"},
            result_summary=None,
            parent_event_id=None,
            llm_payload_uri=None,
            llm_payload_hash=None,
        )
        result = await handler(conn, row, ctx, None, None)
        assert result.get("replayed") is True
        assert result.get("event_type") == et


def test_node_ownership_json_contains_support_spine_nodes() -> None:
    """nce/config_data/node-ownership.json must contain whole-node ownership for support spine nodes."""
    raw = _OWNERSHIP_MAP_PATH.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    entries: list[dict[str, Any]] = data.get("ownership", [])

    ownership_by_type: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        ownership_by_type.setdefault(entry["node_type"], []).append(entry)

    for node_type in _SUPPORT_NODE_TYPES:
        assert node_type in ownership_by_type, (
            f"node-ownership.json missing entry for {node_type!r}"
        )
        rows = ownership_by_type[node_type]
        assert len(rows) == 1, (
            f"expected exactly 1 whole-node ownership row for {node_type}, got {len(rows)}"
        )
        row = rows[0]
        assert row["owner_engine"] == "support", (
            f"expected owner_engine='support' for {node_type}, got {row.get('owner_engine')!r}"
        )
        assert row["transition"] is None, (
            f"expected transition=null (whole-node ownership) for {node_type}, got {row.get('transition')!r}"
        )


def test_no_support_node_type_has_conflicting_transition() -> None:
    """Verify that none of the support node types violate the null-vs-non-null transition ratchet."""
    raw = _OWNERSHIP_MAP_PATH.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    entries: list[dict[str, Any]] = data.get("ownership", [])

    for node_type in _SUPPORT_NODE_TYPES:
        matching = [e for e in entries if e["node_type"] == node_type]
        has_null = any(e.get("transition") is None for e in matching)
        has_non_null = any(e.get("transition") is not None for e in matching)
        assert not (has_null and has_non_null), (
            f"Node type {node_type} has conflicting null and non-null transition rows"
        )


@pytest.mark.asyncio
async def test_assert_owner_support_engine_contract() -> None:
    """Verify assert_owner permissions and refusals for Support nodes."""
    from nce.entity_resolution.ownership import assert_owner

    # Read the actual node-ownership.json entries to feed our test
    raw = _OWNERSHIP_MAP_PATH.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    entries: list[dict[str, Any]] = data.get("ownership", [])

    registry_map: dict[str, str] = {
        e["node_type"]: e["owner_engine"] for e in entries if e.get("transition") is None
    }

    async def fake_lookup_owner(
        conn: Any, ns: Any, node_type: str, transition: str | None = None
    ) -> str | None:
        return registry_map.get(node_type)

    ns_id = uuid4()
    mock_conn = MagicMock()

    # Patch _lookup_owner in ownership module
    import nce.entity_resolution.ownership as ownership_mod

    orig_lookup = ownership_mod._lookup_owner
    ownership_mod._lookup_owner = fake_lookup_owner
    try:
        # 1. 'support' engine should pass for all support node types
        for node_type in _SUPPORT_NODE_TYPES:
            await assert_owner(mock_conn, ns_id, node_type, "support")

        # 2. Non-support engines ('sales', 'assets', 'field_tech') should be refused with OwnershipError
        for node_type in _SUPPORT_NODE_TYPES:
            for non_owner in ("sales", "assets", "field_tech"):
                with pytest.raises(OwnershipError) as exc_info:
                    await assert_owner(mock_conn, ns_id, node_type, non_owner)
                err = exc_info.value
                assert err.node_type == node_type
                assert err.writer_engine == non_owner
                assert err.owner_engine == "support"

        # 3. An unregistered node type should be refused with owner_engine=None (deny-by-default)
        with pytest.raises(OwnershipError) as exc_info:
            await assert_owner(mock_conn, ns_id, "UNREGISTERED_SPINE_NODE_XYZ", "support")
        err = exc_info.value
        assert err.node_type == "UNREGISTERED_SPINE_NODE_XYZ"
        assert err.owner_engine is None
    finally:
        ownership_mod._lookup_owner = orig_lookup
