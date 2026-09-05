"""
Batch 113 — actor-trust-scores integration tests.

Gate requirements:
  1. Seed confirm/reject events for two agents.
  2. Cron tick (_actor_trust_tick) computes diverging trust scores.
  3. High-trust agent's mid-confidence assertion bypasses quarantine.
  4. Low-trust  agent's mid-confidence assertion is quarantined.
  5. Store-time confidence (R) reflects the trust multiplier.

Requires the isolated RL integration stack (port 5433).
"""

from __future__ import annotations

import math
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from nce.active_learning import (
    get_actor_trust,
    quarantine_threshold,
)
from nce.config import cfg
from nce.cron import _actor_trust_tick
from nce.db_utils import scoped_pg_session
from nce.models import AssertionType, MemoryType, StoreMemoryRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HIGH_TRUST_AGENT = "batch113-high-trust-agent"
_LOW_TRUST_AGENT = "batch113-low-trust-agent"
_OPERATOR_ID = "batch113-operator"


@pytest.fixture(autouse=True)
def _hermetic_cron_lock() -> Any:
    """Let ``_actor_trust_tick`` run its body regardless of Redis state.

    The tick acquires a Redis lock and, when it is already held, logs at debug
    level and returns **having written nothing**. A lock left behind by any other
    process therefore turns every trust assertion in this module into a read of
    the default score, with no visible cause. These tests are about trust
    computation; lock behaviour belongs to ``tests/test_scoped_locks.py``.
    """
    with (
        patch("nce.cron.acquire_cron_lock", new=AsyncMock(return_value=object())),
        patch("nce.cron.release_cron_lock", new=AsyncMock()),
    ):
        yield


def _expected_trust(*, confirms: int, rejections: int) -> float:
    """The trust score the cron tick must compute, derived from the seeded counts.

    Mirrors ``_actor_trust_tick``'s Laplace-smoothed formula **independently of
    the database**, so assertions compare the stored value against one the
    system under test did not produce. Deriving the expectation from
    ``get_actor_trust`` instead is self-confirming: when the tick writes nothing
    and the getter returns its default, both sides of the comparison move
    together and the assertion cannot fail. The contradiction term is zero
    because ``_seed_events`` seeds no contradictions.
    """
    return max(0.1, min(0.95, (confirms + 1) / (confirms + rejections + 2)))


def _make_payload(ns_id: uuid.UUID, agent_id: str, confidence: float) -> StoreMemoryRequest:
    return StoreMemoryRequest(
        namespace_id=ns_id,
        agent_id=agent_id,
        content=f"Assertion from {agent_id} at confidence {confidence}.",
        summary=f"Batch 113 test — {agent_id}.",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
        metadata={"confidence": confidence},
    )


async def _seed_events(
    pg_pool: asyncpg.Pool,
    ns_id: uuid.UUID,
    agent_id: str,
    *,
    confirms: int,
    rejections: int,
) -> None:
    """Seed ``quarantine_confirmed`` / ``quarantine_rejected`` WORM events for an agent."""
    from nce import event_log as _el

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        for _ in range(confirms):
            await _el.append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id=_OPERATOR_ID,
                event_type="quarantine_confirmed",
                params={
                    "queue_item_id": str(uuid.uuid4()),
                    "agent_id": agent_id,
                    "operator_id": _OPERATOR_ID,
                },
            )
        for _ in range(rejections):
            await _el.append_event(
                conn=conn,
                namespace_id=ns_id,
                agent_id=_OPERATOR_ID,
                event_type="quarantine_rejected",
                params={
                    "queue_item_id": str(uuid.uuid4()),
                    "agent_id": agent_id,
                    "operator_id": _OPERATOR_ID,
                    "payload_sha256": "aa" * 32,
                },
            )


async def _get_trust_row(
    pg_pool: asyncpg.Pool, ns_id: uuid.UUID, actor_id: str
) -> dict[str, Any] | None:
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT confirmations, rejections, trust
            FROM actor_trust
            WHERE namespace_id = $1::uuid AND actor_id = $2 AND actor_kind = 'agent'
            """,
            ns_id,
            actor_id,
        )
    if row is None:
        return None
    return {
        "confirmations": row["confirmations"],
        "rejections": row["rejections"],
        "trust": float(row["trust"]),
    }


# ---------------------------------------------------------------------------
# Step 1 + 2: actor_trust table exists and cron tick computes diverging trust
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actor_trust_table_exists(pg_pool: asyncpg.Pool) -> None:
    """Verify actor_trust table is present with the expected columns (no DDL here)."""
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = 'actor_trust'
              AND column_name  = 'trust'
            """
        )
    assert row is not None, (
        "actor_trust table or 'trust' column missing — C0 migration not applied. STOP."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cron_computes_diverging_trust(pg_pool: asyncpg.Pool, make_namespace) -> None:
    """
    Seed confirm/reject events for two agents then run the cron tick.
    High-trust agent (many confirms) must have higher trust than low-trust agent.
    Laplace formula is verified against the analytic value.
    """
    ns_id: uuid.UUID = await make_namespace()

    # High-trust: 9 confirms, 1 rejection → raw = (9+1)/(9+1+2) = 10/12 ≈ 0.833
    await _seed_events(pg_pool, ns_id, _HIGH_TRUST_AGENT, confirms=9, rejections=1)
    # Low-trust:  1 confirm, 9 rejections → raw = (1+1)/(1+9+2) = 2/12 ≈ 0.167
    await _seed_events(pg_pool, ns_id, _LOW_TRUST_AGENT, confirms=1, rejections=9)

    # Run the cron tick against the real DB.
    await _actor_trust_tick(pg_pool)

    high_row = await _get_trust_row(pg_pool, ns_id, _HIGH_TRUST_AGENT)
    low_row = await _get_trust_row(pg_pool, ns_id, _LOW_TRUST_AGENT)

    assert high_row is not None, "actor_trust row missing for high-trust agent after cron tick"
    assert low_row is not None, "actor_trust row missing for low-trust agent after cron tick"

    # Divergence: high > low
    assert high_row["trust"] > low_row["trust"], (
        f"Expected high-trust ({high_row['trust']:.3f}) > low-trust ({low_row['trust']:.3f})"
    )

    # Verify Laplace formula exactly (no contradictions → contradictions_sourced=0).
    expected_high = max(0.1, min(0.95, (9 + 1) / (9 + 1 + 2) - 0.05 * math.log1p(0)))
    expected_low = max(0.1, min(0.95, (1 + 1) / (1 + 9 + 2) - 0.05 * math.log1p(0)))

    assert abs(high_row["trust"] - expected_high) < 1e-4, (
        f"High-trust Laplace mismatch: got {high_row['trust']:.5f}, expected {expected_high:.5f}"
    )
    assert abs(low_row["trust"] - expected_low) < 1e-4, (
        f"Low-trust Laplace mismatch: got {low_row['trust']:.5f}, expected {expected_low:.5f}"
    )


# ---------------------------------------------------------------------------
# Step 3: get_actor_trust returns the stored value
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_actor_trust_returns_stored_value(pg_pool: asyncpg.Pool, make_namespace) -> None:
    """get_actor_trust must return the value written by the cron tick."""
    ns_id: uuid.UUID = await make_namespace()
    await _seed_events(pg_pool, ns_id, _HIGH_TRUST_AGENT, confirms=9, rejections=1)
    await _actor_trust_tick(pg_pool)

    trust = await get_actor_trust(pg_pool, ns_id, _HIGH_TRUST_AGENT)
    expected = max(0.1, min(0.95, (9 + 1) / (9 + 1 + 2)))
    assert abs(trust - expected) < 1e-4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_actor_trust_returns_default_for_unknown_actor(
    pg_pool: asyncpg.Pool, make_namespace
) -> None:
    """Unknown actors must fall back to cfg.NCE_TRUST_DEFAULT."""
    ns_id: uuid.UUID = await make_namespace()
    trust = await get_actor_trust(pg_pool, ns_id, "never-seen-agent")
    assert trust == cfg.NCE_TRUST_DEFAULT


# ---------------------------------------------------------------------------
# Step 4 + 5: quarantine threshold is dynamic; store-time R reflects trust
# ---------------------------------------------------------------------------


def test_quarantine_threshold_pure() -> None:
    """quarantine_threshold is a pure function — verify key landmarks."""
    # High trust → lower threshold (easier bypass)
    high = quarantine_threshold(0.95)
    # Low trust → higher threshold (harder bypass)
    low = quarantine_threshold(0.1)
    # Default trust
    default = quarantine_threshold(cfg.NCE_TRUST_DEFAULT)

    assert high > low, "High-trust threshold must exceed low-trust threshold"
    # Clamped to [0.5, 0.8]
    assert 0.5 <= low <= 0.8
    assert 0.5 <= high <= 0.8
    assert 0.5 <= default <= 0.8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_high_trust_agent_bypasses_quarantine(pg_pool: asyncpg.Pool, make_namespace) -> None:
    """
    After the cron tick computes a high trust score, the high-trust agent's
    mid-confidence assertion must NOT be quarantined.

    At confirms=9/rejections=1 the tick must store trust = 10/12 ≈ 0.833, giving
    a threshold of ``0.5 + 0.3 * 0.833`` ≈ 0.75 (clamped to
    ``cfg.NCE_TRUST_QUARANTINE_BYPASS``). Confidence is set above that.
    """
    ns_id: uuid.UUID = await make_namespace()
    await _seed_events(pg_pool, ns_id, _HIGH_TRUST_AGENT, confirms=9, rejections=1)
    await _actor_trust_tick(pg_pool)

    # Expectation comes from the seeded counts, never from get_actor_trust — see
    # _expected_trust. THIS assertion is what fails if the tick wrote nothing.
    expected_trust = _expected_trust(confirms=9, rejections=1)
    trust = await get_actor_trust(pg_pool, ns_id, _HIGH_TRUST_AGENT)
    assert abs(trust - expected_trust) < 1e-4, (
        f"cron tick must store trust={expected_trust:.4f} for the high-trust agent, "
        f"got {trust:.4f} — a default here means the tick did not run"
    )

    # Threshold is derived from the INDEPENDENT expectation, so confidence cannot
    # be positioned relative to whatever the database happened to return.
    threshold = quarantine_threshold(expected_trust)
    confidence = min(1.0, threshold + 0.05)

    payload = _make_payload(ns_id, _HIGH_TRUST_AGENT, confidence)

    # _quarantine_if_needed with bypass=False; we check the dynamic path.
    from nce.orchestrators.memory import MemoryOrchestrator

    orch = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=AsyncMock(),
        redis_client=AsyncMock(),
    )

    result = await orch._quarantine_if_needed(payload, R=confidence, bypass=False)
    assert result is None, (
        f"High-trust agent should NOT be quarantined at R={confidence:.3f} "
        f"(threshold={threshold:.3f}, trust={trust:.3f})"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_low_trust_agent_is_quarantined(pg_pool: asyncpg.Pool, make_namespace) -> None:
    """
    After the cron tick computes a low trust score, the low-trust agent's
    mid-confidence assertion must BE quarantined even at confidence > 0.5.
    """
    ns_id: uuid.UUID = await make_namespace()
    await _seed_events(pg_pool, ns_id, _LOW_TRUST_AGENT, confirms=1, rejections=9)
    await _actor_trust_tick(pg_pool)

    # Expectation comes from the seeded counts, never from get_actor_trust — see
    # _expected_trust. THIS assertion is what fails if the tick wrote nothing.
    expected_trust = _expected_trust(confirms=1, rejections=9)
    trust = await get_actor_trust(pg_pool, ns_id, _LOW_TRUST_AGENT)
    assert abs(trust - expected_trust) < 1e-4, (
        f"cron tick must store trust={expected_trust:.4f} for the low-trust agent, "
        f"got {trust:.4f} — a default here means the tick did not run"
    )

    # Threshold is derived from the INDEPENDENT expectation. Deriving confidence
    # from the observed threshold made this land below it by construction, so the
    # test passed whether or not the tick had computed anything.
    threshold = quarantine_threshold(expected_trust)
    confidence = threshold - 0.02
    assert confidence >= 0.5, "Test confidence must be at or above the hard lower bound"

    payload = _make_payload(ns_id, _LOW_TRUST_AGENT, confidence)

    from nce.orchestrators.memory import MemoryOrchestrator

    orch = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=AsyncMock(),
        redis_client=AsyncMock(),
    )

    result = await orch._quarantine_if_needed(payload, R=confidence, bypass=False)
    assert result is not None, (
        f"Low-trust agent should be quarantined at R={confidence:.3f} "
        f"(threshold={threshold:.3f}, trust={trust:.3f})"
    )
    assert result["quarantined"] is True
    assert "actor_trust" in result
    assert abs(result["actor_trust"] - trust) < 1e-4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_time_confidence_reflects_trust_multiplier(
    pg_pool: asyncpg.Pool, make_namespace
) -> None:
    """
    _compute_salience_score must apply the trust multiplier so that:
      - high-trust agent's R ≥ stated confidence (multiplier ≥ 1 when trust > default)
        or proportionally close to it
      - low-trust agent's R < stated confidence (multiplier < 1 when trust < default)
    """
    ns_id: uuid.UUID = await make_namespace()
    await _seed_events(pg_pool, ns_id, _HIGH_TRUST_AGENT, confirms=9, rejections=1)
    await _seed_events(pg_pool, ns_id, _LOW_TRUST_AGENT, confirms=1, rejections=9)
    await _actor_trust_tick(pg_pool)

    raw_confidence = 0.7

    from nce.orchestrators.memory import MemoryOrchestrator

    orch = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=AsyncMock(),
        redis_client=AsyncMock(),
    )

    payload_high = _make_payload(ns_id, _HIGH_TRUST_AGENT, raw_confidence)
    payload_low = _make_payload(ns_id, _LOW_TRUST_AGENT, raw_confidence)

    R_high = await orch._compute_salience_score(payload_high)
    R_low = await orch._compute_salience_score(payload_low)

    # High-trust agent gets a higher (or equal) effective R than low-trust.
    assert R_high > R_low, (
        f"High-trust R ({R_high:.4f}) must exceed low-trust R ({R_low:.4f}) "
        "for the same raw confidence"
    )

    # Low-trust R must be below the raw confidence (discounted).
    assert R_low < raw_confidence, (
        f"Low-trust R ({R_low:.4f}) must be discounted below raw confidence {raw_confidence}"
    )


# ---------------------------------------------------------------------------
# Security fix: self-confirm / self-reject guards (Batch 113 post-review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_confirm_denied_raises() -> None:
    """confirm_memory must reject operator_id == authoring agent_id (trust-gaming guard)."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock, patch

    from nce.active_learning import ActiveLearningManager

    ns_id = uuid.uuid4()
    item_id = uuid.uuid4()
    agent_id = "agent-alpha"

    # Build a fake DB row whose agent_id matches the operator_id we will pass.
    fake_row = {
        "payload": '{"namespace_id": "' + str(ns_id) + '", "agent_id": "' + agent_id + '", '
        '"content": "test", "summary": "test", "memory_type": "episodic", '
        '"assertion_type": "fact", "metadata": {}}',
        "status": "pending",
        "agent_id": agent_id,
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=fake_row)

    mock_pool = MagicMock()
    manager = ActiveLearningManager(mock_pool)

    @asynccontextmanager
    async def _fake_session(pool, ns):
        yield mock_conn

    with patch("nce.active_learning.scoped_pg_session", _fake_session):
        with pytest.raises(ValueError, match="Self-confirm denied"):
            await manager.confirm_memory(
                namespace_id=ns_id,
                queue_item_id=item_id,
                operator_id=agent_id,  # same as authoring agent → must raise
                memory_orchestrator=AsyncMock(),
            )


@pytest.mark.asyncio
async def test_self_reject_denied_raises() -> None:
    """reject_memory must reject operator_id == authoring agent_id (trust-gaming guard)."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock, patch

    from nce.active_learning import ActiveLearningManager

    ns_id = uuid.uuid4()
    item_id = uuid.uuid4()
    agent_id = "agent-beta"

    fake_row = {
        "status": "pending",
        "payload": '{"x": 1}',
        "agent_id": agent_id,
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=fake_row)

    mock_pool = MagicMock()
    manager = ActiveLearningManager(mock_pool)

    @asynccontextmanager
    async def _fake_session(pool, ns):
        yield mock_conn

    with patch("nce.active_learning.scoped_pg_session", _fake_session):
        with pytest.raises(ValueError, match="Self-reject denied"):
            await manager.reject_memory(
                namespace_id=ns_id,
                queue_item_id=item_id,
                operator_id=agent_id,  # same as authoring agent → must raise
            )


def test_quarantine_threshold_bypass_knob_controls_ceiling() -> None:
    """NCE_TRUST_QUARANTINE_BYPASS must control the upper bound of quarantine_threshold."""
    from nce import config as _nce_config
    from nce.active_learning import quarantine_threshold

    original_bypass = _nce_config.cfg.NCE_TRUST_QUARANTINE_BYPASS
    try:
        _nce_config.cfg.__class__.NCE_TRUST_QUARANTINE_BYPASS = 0.7  # type: ignore[attr-defined]

        # At max trust (1.0) the raw value is 0.5 + 0.3 = 0.8, but the ceiling is
        # now 0.7, so the result must be clamped to 0.7.
        result_high = quarantine_threshold(1.0)
        assert result_high == pytest.approx(0.7, abs=1e-9), (
            f"Expected ceiling 0.7 with bypass knob=0.7, got {result_high}"
        )

        # At low trust the floor still holds.
        result_low = quarantine_threshold(0.0)
        assert result_low == pytest.approx(0.5, abs=1e-9), f"Expected floor 0.5, got {result_low}"
    finally:
        _nce_config.cfg.__class__.NCE_TRUST_QUARANTINE_BYPASS = original_bypass  # type: ignore[attr-defined]
