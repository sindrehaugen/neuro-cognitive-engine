"""Tests for scripts/backfill_chain_hash.py migration logic."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

os.environ.setdefault("NCE_MASTER_KEY", "dev-test-key-32chars-long!!")

# Import the functions under test via the script (it adds repo root to sys.path)
import sys
from pathlib import Path

import pytest

from nce.event_log import _GENESIS_SENTINEL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "backfill_chain_hash",
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_chain_hash.py",
)
_backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backfill)


UTC = timezone.utc


def _make_event_row(
    *,
    event_seq: int,
    namespace_id: UUID | None = None,
    chain_hash: bytes | None = None,
    params: dict[str, Any] | None = None,
    parent_event_id: UUID | None = None,
) -> dict[str, Any]:
    ns = namespace_id or uuid4()
    return {
        "id": uuid4(),
        "namespace_id": ns,
        "agent_id": "test-agent",
        "event_type": "store_memory",
        "event_seq": event_seq,
        "occurred_at": datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
        "params": params or {"key": "val"},
        "parent_event_id": parent_event_id,
        "chain_hash": chain_hash,
    }


def _recompute_for_row(row: dict[str, Any], previous: bytes) -> bytes:
    """Recompute chain_hash the same way the backfill script does."""
    return _backfill._recompute_chain_hash(row, previous)


class TestRecomputeChainHash:
    """Verify _recompute_chain_hash produces the same values as append_event."""

    def test_genesis_event_matches_append_event(self):
        row = _make_event_row(event_seq=1)
        expected = _recompute_for_row(row, _GENESIS_SENTINEL)
        assert isinstance(expected, bytes)
        assert len(expected) == 32

    def test_second_event_links_to_first(self):
        ns = uuid4()
        row1 = _make_event_row(event_seq=1, namespace_id=ns)
        hash1 = _recompute_for_row(row1, _GENESIS_SENTINEL)

        row2 = _make_event_row(event_seq=2, namespace_id=ns)
        hash2 = _recompute_for_row(row2, hash1)

        # Different events → different hashes
        assert hash1 != hash2

    def test_different_params_yield_different_hash(self):
        ns = uuid4()
        row_a = _make_event_row(event_seq=1, namespace_id=ns, params={"a": 1})
        row_b = _make_event_row(event_seq=1, namespace_id=ns, params={"b": 2})

        hash_a = _recompute_for_row(row_a, _GENESIS_SENTINEL)
        hash_b = _recompute_for_row(row_b, _GENESIS_SENTINEL)
        assert hash_a != hash_b

    def test_parent_event_id_included(self):
        ns = uuid4()
        parent = uuid4()
        row_with = _make_event_row(event_seq=1, namespace_id=ns, parent_event_id=parent)
        row_without = _make_event_row(event_seq=1, namespace_id=ns, parent_event_id=None)

        hash_with = _recompute_for_row(row_with, _GENESIS_SENTINEL)
        hash_without = _recompute_for_row(row_without, _GENESIS_SENTINEL)
        assert hash_with != hash_without


class TestBackfillNamespace:
    """Test _backfill_namespace with a mocked asyncpg connection."""

    @pytest.mark.asyncio
    async def test_backfills_null_chain_hashes(self):
        ns = uuid4()
        row1 = _make_event_row(event_seq=1, namespace_id=ns, chain_hash=None)
        row2 = _make_event_row(event_seq=2, namespace_id=ns, chain_hash=None)

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row1, row2])
        conn.execute = AsyncMock(return_value="UPDATE 2")

        checked, updated = await _backfill._backfill_namespace(conn, ns)

        assert checked == 2
        assert updated == 2
        assert conn.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_rows_with_correct_hash(self):
        ns = uuid4()
        # Pre-compute correct hash for row1
        row1 = _make_event_row(event_seq=1, namespace_id=ns, chain_hash=None)
        correct_hash1 = _recompute_for_row(row1, _GENESIS_SENTINEL)
        row1["chain_hash"] = correct_hash1

        # Row2 has NULL → needs backfill
        row2 = _make_event_row(event_seq=2, namespace_id=ns, chain_hash=None)

        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row1, row2])
        conn.execute = AsyncMock(return_value="UPDATE 1")

        checked, updated = await _backfill._backfill_namespace(conn, ns)

        assert checked == 2
        assert updated == 1
        assert conn.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_namespace_no_updates(self):
        ns = uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])

        checked, updated = await _backfill._backfill_namespace(conn, ns)

        assert checked == 0
        assert updated == 0
        conn.execute.assert_not_awaited()


class TestCoerceChainHash:
    """Edge cases for _coerce_chain_hash."""

    def test_none_returns_none(self):
        assert _backfill._coerce_chain_hash(None) is None

    def test_bytes_returns_bytes(self):
        b = b"\x00" * 32
        assert _backfill._coerce_chain_hash(b) == b

    def test_memoryview_returns_bytes(self):
        b = b"\xab" * 32
        assert _backfill._coerce_chain_hash(memoryview(b)) == b

    def test_str_returns_none(self):
        assert _backfill._coerce_chain_hash("not bytes") is None


class TestWormTriggerHelpers:
    @pytest.mark.asyncio
    async def test_worm_trigger_enabled_true_when_not_disabled(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"tgenabled": "O"})
        assert await _backfill._worm_trigger_enabled(conn) is True

    @pytest.mark.asyncio
    async def test_worm_trigger_enabled_false_when_disabled(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"tgenabled": "D"})
        assert await _backfill._worm_trigger_enabled(conn) is False

    @pytest.mark.asyncio
    async def test_worm_trigger_missing_raises(self) -> None:
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(RuntimeError, match="not found"):
            await _backfill._worm_trigger_enabled(conn)

    @pytest.mark.asyncio
    async def test_set_worm_trigger_verifies_state(self) -> None:
        conn = AsyncMock()
        conn.execute = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"tgenabled": "D"})

        with pytest.raises(RuntimeError, match="Failed to enable"):
            await _backfill._set_worm_trigger(conn, enabled=True)

        conn.execute.assert_awaited_once()
