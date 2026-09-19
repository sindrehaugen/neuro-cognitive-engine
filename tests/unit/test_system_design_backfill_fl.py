"""
tests/unit/test_system_design_backfill_fl.py
=============================================
Unit tests for MLv1.6 Lane G Wave G-3: the FL-tree backfill decision logic.

Covers only what is real today (charter §9's "propose a shape, never
invent a literal" applies to tests too):
  - config: NCE_IMPORT_SOURCE_SCHEMA_FL is required, never a default.
  - candidate_fl_label() matches system_design.graph's private _fl_label
    exactly (drift guard on a duplicated pure function).
  - decide_fl_backfill_action()'s four branches: already-imported,
    exact-label match, C1 fuzzy match/queue, and insert.
  - dry_run_fl_backfill() aggregates counts and never writes.
  - fetch_source_fl_rows() and apply_fl_backfill() are explicit stubs,
    not silently-wrong implementations — asserted via NotImplementedError.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nce.config import DeploymentConfigurationError
from nce.vertical_modules.system_design import backfill_fl
from nce.vertical_modules.system_design.graph import _fl_label

_TEST_NS = str(uuid.uuid4())
_TEST_SLUG = "acme"


class _FakeConn:
    """Query-text-dispatching fake asyncpg connection — no live Postgres."""

    def __init__(
        self,
        *,
        source_id_match: uuid.UUID | None = None,
        label_match: uuid.UUID | None = None,
        resolve_rows: list[dict[str, Any]] | None = None,
        enqueue_id: uuid.UUID | None = None,
        watermark_max_updated_at: Any = None,
    ) -> None:
        self._source_id_match = source_id_match
        self._label_match = label_match
        self._resolve_rows = resolve_rows or []
        self._enqueue_id = enqueue_id or uuid.uuid4()
        self._watermark_max_updated_at = watermark_max_updated_at
        self.fetchval_queries: list[str] = []
        self.fetch_queries: list[str] = []

    async def fetchval(self, query: str, *params: Any) -> Any:
        self.fetchval_queries.append(query)
        if "system_design_source_id = $3" in query:
            return self._source_id_match
        if "label = $3" in query:
            return self._label_match
        if "INSERT INTO entity_merge_queue" in query:
            return self._enqueue_id
        if "MAX(updated_at)" in query:
            return self._watermark_max_updated_at
        raise AssertionError(f"unexpected fetchval query:\n{query}")

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.fetch_queries.append(query)
        return self._resolve_rows


def _row(source_id: str = "guid-1", *parts: str) -> backfill_fl.SourceFLRow:
    return backfill_fl.SourceFLRow(
        source_id=source_id,
        path_parts=parts or ("Building A", "Floor 1", "Room 101"),
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_source_schema_name_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backfill_fl.SOURCE_SCHEMA_ENV, raising=False)
    # DeploymentConfigurationError, not ValueError: an unset env var is the
    # operator's failure to fix, not a caller-supplied bad argument — see
    # source_schema_name()'s docstring and nce.config.DeploymentConfigurationError's.
    with pytest.raises(DeploymentConfigurationError) as exc_info:
        backfill_fl.source_schema_name()
    assert not isinstance(exc_info.value, ValueError)


def test_source_schema_name_reads_env_never_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backfill_fl.SOURCE_SCHEMA_ENV, "d365_mirror_rig")
    assert backfill_fl.source_schema_name() == "d365_mirror_rig"


def test_source_key_is_source_system_prefixed() -> None:
    assert backfill_fl.source_key("guid-1") == "d365_fl:guid-1"


# ---------------------------------------------------------------------------
# Label convention — drift guard against system_design.graph._fl_label
# ---------------------------------------------------------------------------


def test_candidate_fl_label_matches_graphs_private_convention() -> None:
    cases = [
        (_TEST_SLUG, ("Building A", "Floor 1", "Room 101")),
        ("Different-Slug", ("Site Only",)),
        ("x", ("a", "b", "c", "d", "e")),
    ]
    for slug, parts in cases:
        assert backfill_fl.candidate_fl_label(slug, parts) == _fl_label(slug, *parts)


# ---------------------------------------------------------------------------
# decide_fl_backfill_action — four branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_already_imported_is_a_match_not_an_insert() -> None:
    existing_id = uuid.uuid4()
    conn = _FakeConn(source_id_match=existing_id)
    decision = await backfill_fl.decide_fl_backfill_action(
        conn, _TEST_NS, _row(), namespace_slug=_TEST_SLUG
    )
    assert decision.action == "match"
    assert decision.matched_node_id == existing_id
    assert "already imported" in decision.match_reason


@pytest.mark.asyncio
async def test_decide_exact_label_match_short_circuits_before_c1() -> None:
    existing_id = uuid.uuid4()
    conn = _FakeConn(label_match=existing_id)
    decision = await backfill_fl.decide_fl_backfill_action(
        conn, _TEST_NS, _row(), namespace_slug=_TEST_SLUG
    )
    assert decision.action == "match"
    assert decision.matched_node_id == existing_id
    assert conn.fetch_queries == []  # never asked C1 — the exact match already answered it


@pytest.mark.asyncio
async def test_decide_high_confidence_c1_match_is_reported_never_written() -> None:
    match_id = uuid.uuid4()
    conn = _FakeConn(
        resolve_rows=[{"node_id": str(match_id), "score": 0.97}],
    )
    decision = await backfill_fl.decide_fl_backfill_action(
        conn, _TEST_NS, _row(), namespace_slug=_TEST_SLUG
    )
    assert decision.action == "match"
    assert decision.matched_node_id == match_id
    assert decision.match_score == 0.97
    # No enqueue call — a high-confidence match never touches the merge queue.
    assert all("entity_merge_queue" not in q for q in conn.fetchval_queries)


@pytest.mark.asyncio
async def test_decide_ambiguous_c1_match_goes_to_merge_queue() -> None:
    match_id = uuid.uuid4()
    queue_id = uuid.uuid4()
    conn = _FakeConn(
        resolve_rows=[{"node_id": str(match_id), "score": 0.7}],
        enqueue_id=queue_id,
    )
    decision = await backfill_fl.decide_fl_backfill_action(
        conn, _TEST_NS, _row(), namespace_slug=_TEST_SLUG
    )
    assert decision.action == "queue"
    assert decision.matched_node_id == match_id
    assert decision.match_score == 0.7
    assert str(queue_id) in decision.match_reason
    assert any("INSERT INTO entity_merge_queue" in q for q in conn.fetchval_queries)


@pytest.mark.asyncio
async def test_decide_no_match_at_all_is_an_insert_candidate() -> None:
    conn = _FakeConn(resolve_rows=[])
    decision = await backfill_fl.decide_fl_backfill_action(
        conn, _TEST_NS, _row(), namespace_slug=_TEST_SLUG
    )
    assert decision.action == "insert"
    assert decision.matched_node_id is None


@pytest.mark.asyncio
async def test_decide_low_score_match_below_queue_floor_is_still_an_insert() -> None:
    conn = _FakeConn(resolve_rows=[{"node_id": str(uuid.uuid4()), "score": 0.1}])
    decision = await backfill_fl.decide_fl_backfill_action(
        conn, _TEST_NS, _row(), namespace_slug=_TEST_SLUG
    )
    assert decision.action == "insert"


# ---------------------------------------------------------------------------
# dry_run_fl_backfill — aggregation, no writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_aggregates_counts_across_mixed_rows() -> None:
    already_id = uuid.uuid4()

    class _RoutingConn(_FakeConn):
        """Route by source_id so three different rows get three different outcomes."""

        async def fetchval(self, query: str, *params: Any) -> Any:  # type: ignore[override]
            self.fetchval_queries.append(query)
            if "system_design_source_id = $3" in query:
                # params[0]=node_type, params[1]=namespace_id, params[2]=source_key
                return already_id if params[2].endswith("already") else None
            if "label = $3" in query:
                return None
            if "INSERT INTO entity_merge_queue" in query:
                return uuid.uuid4()
            raise AssertionError(f"unexpected fetchval query:\n{query}")

        async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
            self.fetch_queries.append(query)
            # candidate name is params[-1] via resolve()'s normalized_keys.values()
            name = params[-1]
            if name == "queueme":
                return [{"node_id": str(uuid.uuid4()), "score": 0.6}]
            return []

    conn = _RoutingConn()
    rows = [
        _row("already", "Site", "Building", "already"),
        _row("brandnew", "Site", "Building", "brandnew"),
        _row("iffy", "Site", "Building", "queueme"),
    ]
    report = await backfill_fl.dry_run_fl_backfill(conn, _TEST_NS, rows, namespace_slug=_TEST_SLUG)
    assert report.total_source_rows == 3
    assert report.would_match_existing == 1
    assert report.would_insert == 1
    assert report.would_queue_for_review == 1
    assert len(report.decisions) == 3


# ---------------------------------------------------------------------------
# Explicit stubs — genuine unknowns, not invented behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_source_fl_rows_is_an_explicit_stub() -> None:
    with pytest.raises(NotImplementedError, match="information_schema|not yet confirmed"):
        await backfill_fl.fetch_source_fl_rows(None, "some_schema")


@pytest.mark.asyncio
async def test_apply_fl_backfill_is_an_explicit_stub() -> None:
    with pytest.raises(NotImplementedError, match="upsert_fl_path"):
        await backfill_fl.apply_fl_backfill()


# ---------------------------------------------------------------------------
# Watermark, derived (no dedicated table)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_watermark_none_with_no_connection() -> None:
    assert await backfill_fl.load_watermark(None, _TEST_NS) is None


@pytest.mark.asyncio
async def test_load_watermark_none_when_nothing_imported_yet() -> None:
    conn = _FakeConn(watermark_max_updated_at=None)
    assert await backfill_fl.load_watermark(conn, _TEST_NS) is None


@pytest.mark.asyncio
async def test_load_watermark_subtracts_overlap() -> None:
    import datetime as dt

    now = dt.datetime(2026, 9, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
    conn = _FakeConn(watermark_max_updated_at=now)
    watermark = await backfill_fl.load_watermark(conn, _TEST_NS)
    expected = (now - backfill_fl.WATERMARK_OVERLAP).isoformat()
    assert watermark == expected
