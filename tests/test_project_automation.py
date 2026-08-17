"""Integration tests for project/automation.py — Wave 7 (auto-tasking-gate).

Acceptance criteria (Batch_074_Module_7_Wave_7.md):

1. A Procurement (PO_LINE.status_changed) or Warehouse (GOODS_RECEIPT.created)
   event fires the RQ task which calls do_sync_bom_tasks through @governed.
2. Tier-1 (<50K) project: the act auto-executes (confirm=True internally),
   is audited to event_log, and is idempotent on replay (second call with the
   same idempotency key is a no-op).
3. Tier-3/4 (>=500K) project: the act is held for confirm (returns
   {"status": "pending_approval"}) — do_sync_bom_tasks is NOT called.
4. Kill switch (nce:tools:disabled) blocks the autonomous act even for Tier-1.
5. Tiers are read from automation-tiers.json via resolve_tier.

All DB-dependent tests are @pytest.mark.integration.

Pure-logic tests (tier resolution, kill-switch semantics) are plain unit tests
(no DB required).

Design notes
------------
- Tests call ``_run_governed_sync`` directly (the inner async orchestrator)
  rather than the RQ task wrapper to avoid spinning up a Redis Queue.
- ``rq_sync_bom_on_po`` and ``rq_sync_bom_on_goods_receipt`` are exercised via
  a smoke test with a mock engine (verifying the adapter delegates correctly).
- kill-switch tests use an AsyncMock Redis client per the @governed contract.
- Unique quote_id per test avoids kg_nodes collision in a shared DB.

C2 kill-switch gap coverage (re-dispatch fixes)
-----------------------------------------------
- ``test_kill_switch_registry_blocks_rq_task``: verifies that the C4 bus path
  (calling the RQ task function directly with no explicit redis_client) picks
  up the kill-switch client from the module-level registry — exercises the
  path the prior audit found was structurally untested.
- ``test_project_value_none_is_tier4_failclosed``: verifies that omitting
  project_value (None sentinel) in the event payload resolves to Tier-4
  (confirm-required), never Tier-1 (autonomous).
- ``test_c4_handler_absent_project_value_sends_none``: verifies that
  _handle_po_status_changed passes project_value=None (not 0.0) when the
  key is absent from the event payload — closes the enqueue-kwargs gap.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.autonomy.governor import KillSwitchError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.project.automation import (
    _REDIS_REGISTRY,
    _run_governed_sync,
    register_engine,
    register_redis_client,
    resolve_tier,
    rq_sync_bom_on_goods_receipt,
    rq_sync_bom_on_po,
)
from nce.vertical_modules.project.tasks import _task_label_for_kind

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Tier boundary constants (mirror automation-tiers.json)
_TIER1_VALUE = 10_000.0  # < 50K — Autonomous
_TIER2_VALUE = 100_000.0  # 50K–500K — Actor/Confirm
_TIER3_VALUE = 750_000.0  # 500K–3M — Advisor + PL Review
_TIER4_VALUE = 5_000_000.0  # >= 3M — Advisor Only


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _seed_project_and_bom(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    quote_id: str,
    line_refs: list[str],
) -> tuple[str, list[str]]:
    """Seed a minimal PROJECT + BOM_LINE nodes + contains edges."""
    project_lbl = f"PROJECT:{quote_id.upper()}"
    bom_labels = [f"BOM_LINE:{quote_id.upper()}:{r.upper()}" for r in line_refs]

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            await seed_node_ownership_registry(conn, ns_uuid)

            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'PROJECT_PROJECT', $2::uuid)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                project_lbl,
                str(ns_uuid),
            )

            for bom_label in bom_labels:
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'BOM_LINE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    bom_label,
                    str(ns_uuid),
                )
                await conn.execute(
                    """
                    INSERT INTO kg_edges
                        (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, 'contains', $2, 1.0, $3::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id)
                    DO NOTHING
                    """,
                    project_lbl,
                    bom_label,
                    str(ns_uuid),
                )

    return project_lbl, bom_labels


async def _count_task_nodes(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    task_label: str,
) -> int:
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_nodes
                WHERE label        = $1
                  AND entity_type  = 'PROJECT_TASK'
                  AND namespace_id = $2::uuid
                """,
                task_label,
                str(ns_uuid),
            )


async def _count_idempotency_keys(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    key: str,
) -> int:
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM action_idempotency
                WHERE idempotency_key = $1
                  AND namespace_id    = $2::uuid
                """,
                key,
                str(ns_uuid),
            )


async def _count_event_log_governed(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    action_type: str,
) -> int:
    """Count event_log rows audited for a governed action.

    governor._audit_execution stores the action_type nested under
    params.changes.governed_action.
    """
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM event_log
                WHERE namespace_id = $1::uuid
                  AND params::jsonb -> 'changes' ->> 'governed_action' = $2
                """,
                str(ns_uuid),
                action_type,
            )


# ---------------------------------------------------------------------------
# Pure-logic unit tests — tier resolution (no DB)
# ---------------------------------------------------------------------------


def test_resolve_tier_tier1_boundary_inclusive() -> None:
    """Value exactly at Tier-1 max is Tier 1 (autonomous)."""
    tier = resolve_tier(49_999.99)
    assert tier["tier"] == 1
    assert tier["confirm_required"] is False


def test_resolve_tier_tier2_starts_at_50k() -> None:
    """Value >= 50K triggers Tier 2 (confirm required)."""
    tier = resolve_tier(50_000.0)
    assert tier["tier"] == 2
    assert tier["confirm_required"] is True


def test_resolve_tier_tier3() -> None:
    tier = resolve_tier(_TIER3_VALUE)
    assert tier["tier"] == 3
    assert tier["confirm_required"] is True


def test_resolve_tier_tier4() -> None:
    tier = resolve_tier(_TIER4_VALUE)
    assert tier["tier"] == 4
    assert tier["confirm_required"] is True


def test_resolve_tier_zero_value_is_tier1() -> None:
    """Zero value (default) must be Tier 1."""
    tier = resolve_tier(0.0)
    assert tier["tier"] == 1
    assert tier["confirm_required"] is False


def test_resolve_tier_boundary_tier3_max() -> None:
    """Value at Tier-3 max (2 999 999.99) is still Tier 3."""
    tier = resolve_tier(2_999_999.99)
    assert tier["tier"] == 3
    assert tier["confirm_required"] is True


def test_resolve_tier_tier4_boundary() -> None:
    """Value >= 3M is Tier 4."""
    tier = resolve_tier(3_000_000.0)
    assert tier["tier"] == 4
    assert tier["confirm_required"] is True


# ---------------------------------------------------------------------------
# Integration: Tier-1 auto-acts + idempotency + event_log audit
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier1_auto_sync_executes_and_audits(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Tier-1 (<50K): do_sync_bom_tasks executes autonomously (confirm=True internally).
    The act is audited to event_log and the idempotency key is recorded.
    """
    quote_id = f"B074-T1-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["AMP01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    register_engine(engine)

    result = await _run_governed_sync(
        namespace_id=namespace_id,
        project_id=project_lbl,
        bom_line_label=bom_label,
        status="PLANNED",
        project_value=_TIER1_VALUE,
        redis_client=None,
    )

    # @governed must have executed (not pending)
    assert result["status"] == "executed", f"Unexpected result: {result}"
    inner = result.get("result", {})
    assert inner.get("ok") is True, f"do_sync_bom_tasks failed: {inner}"

    # TASK node must exist in kg_nodes
    expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
    task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert task_count == 1, f"Expected 1 PROJECT_TASK node, got {task_count}"

    # Idempotency key must be recorded
    idem_key = f"bom_sync:{namespace_id}:{bom_label}:PLANNED"
    key_count = await _count_idempotency_keys(pg_pool, namespace_id, idem_key)
    assert key_count == 1, f"Expected idempotency key in action_idempotency, got {key_count}"

    # event_log must have one audit entry for this governed action
    audit_count = await _count_event_log_governed(
        pg_pool, namespace_id, "project_auto_sync_bom_tasks"
    )
    assert audit_count >= 1, "Expected at least one audit row in event_log"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier1_replay_is_noop(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Tier-1 replay with the same idempotency key is a no-op (side effect runs once)."""
    quote_id = f"B074-T1-replay-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["SPEAKER01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    register_engine(engine)

    # First call — should execute
    r1 = await _run_governed_sync(
        namespace_id=namespace_id,
        project_id=project_lbl,
        bom_line_label=bom_label,
        status="ORDERED",
        project_value=_TIER1_VALUE,
        redis_client=None,
    )
    assert r1["status"] == "executed", r1

    # Second call — same params → same idempotency key → no-op
    r2 = await _run_governed_sync(
        namespace_id=namespace_id,
        project_id=project_lbl,
        bom_line_label=bom_label,
        status="ORDERED",
        project_value=_TIER1_VALUE,
        redis_client=None,
    )
    assert r2["status"] == "already_executed", f"Replay must be a no-op, got: {r2}"

    # Verify only ONE idempotency key row exists
    idem_key = f"bom_sync:{namespace_id}:{bom_label}:ORDERED"
    key_count = await _count_idempotency_keys(pg_pool, namespace_id, idem_key)
    assert key_count == 1, f"Expected exactly 1 idempotency key row, got {key_count}"


# ---------------------------------------------------------------------------
# Integration: Tier-3 and Tier-4 → confirm-first
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier3_held_for_confirm(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Tier-3 project (500K–3M): @governed returns pending_approval; no task created."""
    quote_id = f"B074-T3-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["PROJ01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    register_engine(engine)

    result = await _run_governed_sync(
        namespace_id=namespace_id,
        project_id=project_lbl,
        bom_line_label=bom_label,
        status="DELIVERED",
        project_value=_TIER3_VALUE,
        redis_client=None,
    )

    assert result["status"] == "pending_approval", f"Tier-3 must be confirm-first, got: {result}"

    # No TASK node must have been created
    expected_task = _task_label_for_kind(bom_label, "INSTALLATION")
    task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert task_count == 0, f"Tier-3 must not create tasks autonomously; found {task_count} nodes"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tier4_held_for_confirm(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Tier-4 project (>=3M): @governed returns pending_approval; no task created."""
    quote_id = f"B074-T4-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["PROJ02"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    register_engine(engine)

    result = await _run_governed_sync(
        namespace_id=namespace_id,
        project_id=project_lbl,
        bom_line_label=bom_label,
        status="INSTALLED",
        project_value=_TIER4_VALUE,
        redis_client=None,
    )

    assert result["status"] == "pending_approval", f"Tier-4 must be confirm-first, got: {result}"

    # No TASK node must have been created
    expected_task = _task_label_for_kind(bom_label, "TESTING")
    task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert task_count == 0, f"Tier-4 must not create tasks autonomously; found {task_count} nodes"


# ---------------------------------------------------------------------------
# Kill switch — blocks autonomous act even for Tier-1
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kill_switch_blocks_tier1_autonomous_act(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Kill switch (nce:tools:disabled) blocks Tier-1 autonomous act (fail-closed)."""
    quote_id = f"B074-KS-{uuid.uuid4().hex[:8]}"
    project_lbl, bom_labels = await _seed_project_and_bom(
        pg_pool, namespace_id, quote_id, ["CTRL01"]
    )
    bom_label = bom_labels[0]
    engine = _make_engine_stub(pg_pool)
    register_engine(engine)

    # Redis mock: kill switch is active for this tool
    redis_mock = AsyncMock()
    redis_mock.hexists = AsyncMock(return_value=True)  # tool disabled

    with pytest.raises(KillSwitchError):
        await _run_governed_sync(
            namespace_id=namespace_id,
            project_id=project_lbl,
            bom_line_label=bom_label,
            status="PLANNED",
            project_value=_TIER1_VALUE,
            redis_client=redis_mock,
        )

    # No TASK node must have been created (kill switch fired before execution)
    expected_task = _task_label_for_kind(bom_label, "PROCUREMENT")
    task_count = await _count_task_nodes(pg_pool, namespace_id, expected_task)
    assert task_count == 0, f"Kill switch must prevent task creation; found {task_count} nodes"


@pytest.mark.asyncio
async def test_kill_switch_global_blocks_tier1() -> None:
    """Global kill switch (*) blocks Tier-1 even without a DB connection (unit test).

    Uses a mock conn that looks in-transaction, to exercise only the kill-switch
    path without DB I/O.
    """
    mock_conn = MagicMock()
    mock_conn.is_in_transaction.return_value = True
    from nce.vertical_modules.project.automation import _do_governed_bom_sync

    redis_mock = AsyncMock()
    # First hexists (tool-specific) → False; second (global *) → True
    redis_mock.hexists = AsyncMock(side_effect=[False, True])

    with pytest.raises(KillSwitchError):
        await _do_governed_bom_sync(
            mock_conn,
            uuid.uuid4(),
            idempotency_key="kill-switch-test-key",
            confirm=True,
            value=_TIER1_VALUE,
            redis_client=redis_mock,
            engine=MagicMock(),
            project_id="PROJECT:TEST",
            bom_line_label="BOM_LINE:TEST:01",
            status="PLANNED",
        )


# ---------------------------------------------------------------------------
# Tier gating from automation-tiers.json — verify JSON is authoritative
# ---------------------------------------------------------------------------


def test_tiers_loaded_from_json() -> None:
    """resolve_tier reads from automation-tiers.json (not hardcoded constants)."""
    from nce.vertical_modules.project.automation import _TIERS

    assert len(_TIERS) == 4, f"Expected 4 tiers, got {len(_TIERS)}"
    labels = {t["tier"]: t["confirm_required"] for t in _TIERS}
    assert labels[1] is False, "Tier 1 must be autonomous (confirm_required=False)"
    assert labels[2] is True, "Tier 2 must require confirm"
    assert labels[3] is True, "Tier 3 must require confirm"
    assert labels[4] is True, "Tier 4 must require confirm"


# ---------------------------------------------------------------------------
# RQ task adapter smoke tests (mock engine — no DB needed)
# ---------------------------------------------------------------------------


def test_rq_sync_bom_on_po_bad_namespace_returns_error() -> None:
    """rq_sync_bom_on_po returns error dict on invalid namespace_id."""
    result = rq_sync_bom_on_po(
        namespace_id="not-a-uuid",
        project_id="PROJECT:X",
        bom_line_label="BOM_LINE:X:01",
        status="PLANNED",
    )
    assert result["ok"] is False
    assert "invalid_namespace_id" in result["error"]


def test_rq_sync_bom_on_goods_receipt_bad_namespace_returns_error() -> None:
    """rq_sync_bom_on_goods_receipt returns error dict on invalid namespace_id."""
    result = rq_sync_bom_on_goods_receipt(
        namespace_id="not-a-uuid",
        project_id="PROJECT:X",
        bom_line_label="BOM_LINE:X:01",
    )
    assert result["ok"] is False
    assert "invalid_namespace_id" in result["error"]


# ---------------------------------------------------------------------------
# C4 bus subscriber smoke (unit — no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_po_handler_skips_incomplete_event() -> None:
    """_handle_po_status_changed skips events with missing fields (no exception)."""
    from nce.vertical_modules.project.automation import _handle_po_status_changed

    # Incomplete event — missing project_id — must not raise
    await _handle_po_status_changed(
        MagicMock(),  # conn — not used
        {"namespace_id": str(uuid.uuid4()), "payload": {"status": "ORDERED"}},
    )


@pytest.mark.asyncio
async def test_goods_receipt_handler_skips_incomplete_event() -> None:
    """_handle_goods_receipt_created skips events with missing fields."""
    from nce.vertical_modules.project.automation import _handle_goods_receipt_created

    # Incomplete event — must not raise
    await _handle_goods_receipt_created(
        MagicMock(),
        {"namespace_id": str(uuid.uuid4()), "payload": {}},
    )


# ---------------------------------------------------------------------------
# C2 kill-switch gap coverage (re-dispatch fixes)
# ---------------------------------------------------------------------------


def test_kill_switch_registry_lookup_unit() -> None:
    """Unit test: _get_registered_redis_client returns the registered client.

    Verifies the registry pattern itself — not the full async path.
    """
    from nce.vertical_modules.project.automation import _get_registered_redis_client

    mock_client = MagicMock()
    register_redis_client(mock_client)
    try:
        retrieved = _get_registered_redis_client()
        assert retrieved is mock_client, "Registry must return the exact client that was registered"
    finally:
        _REDIS_REGISTRY.clear()

    # After clear, registry must return None
    assert _get_registered_redis_client() is None


def test_rq_po_task_uses_registry_when_no_explicit_redis_client() -> None:
    """rq_sync_bom_on_po passes the registry client to _run_governed_sync.

    Uses a mock of ``_run_governed_sync`` to capture what redis_client
    argument the RQ task passes — without spinning up a DB pool.
    """
    mock_client = MagicMock()
    register_redis_client(mock_client)

    captured: list[Any] = []

    async def _fake_run_governed_sync(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs.get("redis_client"))
        return {"status": "pending_approval"}

    try:
        with patch(
            "nce.vertical_modules.project.automation._run_governed_sync",
            side_effect=_fake_run_governed_sync,
        ):
            rq_sync_bom_on_po(
                namespace_id=str(uuid.uuid4()),
                project_id="PROJECT:REGREG",
                bom_line_label="BOM_LINE:REGREG:01",
                status="PLANNED",
                project_value=_TIER1_VALUE,
                # redis_client intentionally NOT passed
            )
    finally:
        _REDIS_REGISTRY.clear()

    assert len(captured) == 1
    assert captured[0] is mock_client, (
        f"RQ task must pass the registry client to _run_governed_sync; got {captured[0]!r}"
    )


def test_rq_goods_receipt_task_uses_registry_when_no_explicit_redis_client() -> None:
    """rq_sync_bom_on_goods_receipt passes the registry client to _run_governed_sync."""
    mock_client = MagicMock()
    register_redis_client(mock_client)

    captured: list[Any] = []

    async def _fake_run(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs.get("redis_client"))
        return {"status": "pending_approval"}

    try:
        with patch(
            "nce.vertical_modules.project.automation._run_governed_sync",
            side_effect=_fake_run,
        ):
            rq_sync_bom_on_goods_receipt(
                namespace_id=str(uuid.uuid4()),
                project_id="PROJECT:RGGR",
                bom_line_label="BOM_LINE:RGGR:01",
                status="DELIVERED",
                project_value=_TIER1_VALUE,
            )
    finally:
        _REDIS_REGISTRY.clear()

    assert len(captured) == 1
    assert captured[0] is mock_client, (
        f"RQ task must pass the registry client to _run_governed_sync; got {captured[0]!r}"
    )


def test_project_value_none_is_tier4_failclosed() -> None:
    """project_value=None (absent from event payload) resolves to Tier-4 fail-closed.

    A publisher that omits project_value MUST NOT gain autonomous execution.
    The tier must be Tier-4 (confirm_required=True), never Tier-1.
    """
    # resolve_tier is called with _TIER_FAILCLOSED_VALUE when project_value is None.
    # We import the constant to verify the sentinel is above the Tier-3 max.
    from nce.vertical_modules.project.automation import _TIER_FAILCLOSED_VALUE

    tier = resolve_tier(_TIER_FAILCLOSED_VALUE)
    assert tier["tier"] == 4, (
        f"_TIER_FAILCLOSED_VALUE={_TIER_FAILCLOSED_VALUE} must resolve Tier-4, got {tier['tier']}"
    )
    assert tier["confirm_required"] is True


@pytest.mark.asyncio
async def test_c4_handler_absent_project_value_sends_none() -> None:
    """_handle_po_status_changed passes project_value=None when key is absent.

    Prior bug: ``payload.get("project_value", 0.0)`` defaulted to 0.0 (Tier-1).
    Fix: absent key → None → Tier-4 in the RQ task.

    We mock ``_enqueue_rq_task`` to capture what kwargs were passed and verify
    project_value is None (not 0.0) when the event payload omits the key.
    """
    from nce.vertical_modules.project.automation import _handle_po_status_changed

    captured_kwargs: list[dict[str, Any]] = []

    def _fake_enqueue(fn: Any, **kwargs: Any) -> None:
        captured_kwargs.append(kwargs)

    with patch(
        "nce.vertical_modules.project.automation._enqueue_rq_task",
        side_effect=_fake_enqueue,
    ):
        ns_id = str(uuid.uuid4())
        await _handle_po_status_changed(
            MagicMock(),
            {
                "namespace_id": ns_id,
                "payload": {
                    "project_id": "PROJECT:TEST-NOVALUE",
                    "bom_line_label": "BOM_LINE:TEST-NOVALUE:01",
                    "status": "ORDERED",
                    # project_value intentionally absent
                },
            },
        )

    assert len(captured_kwargs) == 1, "Handler must have enqueued exactly one task"
    assert captured_kwargs[0]["project_value"] is None, (
        f"Absent project_value must be passed as None, got: {captured_kwargs[0]['project_value']!r}"
    )


@pytest.mark.asyncio
async def test_c4_handler_explicit_zero_project_value_passes_zero() -> None:
    """_handle_po_status_changed passes project_value=0.0 when key is explicitly 0.

    A publisher explicitly setting project_value=0 has a legitimate zero-value
    project (Tier-1).  The handler must honour this, not coerce it to None.
    """
    from nce.vertical_modules.project.automation import _handle_po_status_changed

    captured_kwargs: list[dict[str, Any]] = []

    def _fake_enqueue(fn: Any, **kwargs: Any) -> None:
        captured_kwargs.append(kwargs)

    with patch(
        "nce.vertical_modules.project.automation._enqueue_rq_task",
        side_effect=_fake_enqueue,
    ):
        ns_id = str(uuid.uuid4())
        await _handle_po_status_changed(
            MagicMock(),
            {
                "namespace_id": ns_id,
                "payload": {
                    "project_id": "PROJECT:TEST-ZERO",
                    "bom_line_label": "BOM_LINE:TEST-ZERO:01",
                    "status": "ORDERED",
                    "project_value": 0,
                },
            },
        )

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["project_value"] == 0.0, (
        f"Explicit 0 must be passed as 0.0, got: {captured_kwargs[0]['project_value']!r}"
    )


@pytest.mark.asyncio
async def test_negative_project_value_fails_closed() -> None:
    """A negative project value must immediately fail closed in _run_governed_sync.

    Verifies that calling _run_governed_sync with a negative project_value rejects the
    execution with {"ok": False, "error": "invalid_project_value"}.
    """
    mock_engine = MagicMock()
    register_engine(mock_engine)

    try:
        result = await _run_governed_sync(
            namespace_id=uuid.uuid4(),
            project_id="PROJECT:NEG-TEST",
            bom_line_label="BOM_LINE:NEG-TEST:01",
            status="PLANNED",
            project_value=-1.0,
            redis_client=None,
        )
        assert result["ok"] is False
        assert result["error"] == "invalid_project_value"
    finally:
        register_engine(None)


def test_kill_switch_registry_blocks_rq_task() -> None:
    """The end-to-end path: RQ task (no explicit redis_client) -> registry client -> kill-switch active -> blocked.

    Verifies that calling the RQ task function directly with no explicit redis_client
    picks up the kill-switch client from the registry, finds it active (disabled),
    and raises KillSwitchError.
    """
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.transaction = MagicMock()
    mock_conn.is_in_transaction.return_value = True

    class AsyncTransactionMock:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_conn.transaction.return_value = AsyncTransactionMock()
    mock_conn.execute = AsyncMock()

    mock_pool = MagicMock()

    class AsyncContextManagerMock:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_pool.acquire.return_value = AsyncContextManagerMock()
    mock_engine.pg_pool = mock_pool

    register_engine(mock_engine)

    redis_mock = AsyncMock()
    redis_mock.hexists = AsyncMock(return_value=True)
    register_redis_client(redis_mock)

    try:
        with pytest.raises(KillSwitchError):
            rq_sync_bom_on_po(
                namespace_id=str(uuid.uuid4()),
                project_id="PROJECT:KS-E2E",
                bom_line_label="BOM_LINE:KS-E2E:01",
                status="PLANNED",
                project_value=_TIER1_VALUE,
                # redis_client is intentionally omitted to check registry fallback
            )
    finally:
        _REDIS_REGISTRY.clear()
        register_engine(None)
