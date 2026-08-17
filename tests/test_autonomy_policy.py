"""
Unit and integration tests for the C2 Contract-B policy gates (Wave 16).

Tests cover:
  - ``evaluate_policy`` pure logic (unit — no DB, no Redis)
  - Kill-switch check via ``_check_kill_switch`` (unit — mock Redis)
  - ``@governed`` decorator wiring of kill switch + policy gates

All ``evaluate_policy`` tests are plain unit tests (no ``@pytest.mark.integration``).
Kill-switch and decorator-wiring tests use mock Redis — also unit tests.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.autonomy.governor import (
    KillSwitchError,
    _check_kill_switch,
    governed,
)
from nce.autonomy.policy import PolicyDecision, evaluate_policy

# ---------------------------------------------------------------------------
# evaluate_policy — pure unit tests
# ---------------------------------------------------------------------------


def test_policy_all_gates_pass_returns_ok() -> None:
    """No gates configured → ok."""
    result = evaluate_policy()
    assert result == PolicyDecision(requires_confirm=False, reason="ok")


def test_policy_over_ceiling_requires_confirm() -> None:
    """Value exceeding ceiling triggers requires_confirm."""
    result = evaluate_policy(value=1001.0, value_ceiling=1000.0)
    assert result.requires_confirm is True
    assert "ceiling" in result.reason


def test_policy_at_ceiling_passes() -> None:
    """Value equal to ceiling is allowed."""
    result = evaluate_policy(value=1000.0, value_ceiling=1000.0)
    assert result.requires_confirm is False


def test_policy_under_ceiling_passes() -> None:
    """Value under ceiling is allowed."""
    result = evaluate_policy(value=999.99, value_ceiling=1000.0)
    assert result.requires_confirm is False


def test_policy_over_volume_cap_requires_confirm() -> None:
    """volume_state exceeding cap triggers requires_confirm."""
    result = evaluate_policy(volume_state=11.0, volume_rate_cap=10.0)
    assert result.requires_confirm is True
    assert "cap" in result.reason


def test_policy_at_volume_cap_passes() -> None:
    """volume_state at cap is allowed."""
    result = evaluate_policy(volume_state=10.0, volume_rate_cap=10.0)
    assert result.requires_confirm is False


def test_policy_non_allowlisted_counterparty_requires_confirm() -> None:
    """Counterparty absent from the allowlist triggers requires_confirm."""
    result = evaluate_policy(
        counterparty="vendor_x",
        allowlist=["vendor_a", "vendor_b"],
    )
    assert result.requires_confirm is True
    assert "allowlist" in result.reason


def test_policy_allowlisted_counterparty_passes() -> None:
    """Counterparty present in the allowlist passes."""
    result = evaluate_policy(
        counterparty="vendor_a",
        allowlist=["vendor_a", "vendor_b"],
    )
    assert result.requires_confirm is False


def test_policy_empty_allowlist_skips_gate() -> None:
    """Empty allowlist means gate is disabled — any counterparty passes."""
    result = evaluate_policy(counterparty="vendor_x", allowlist=[])
    assert result.requires_confirm is False


def test_policy_risk_flag_flagship_requires_confirm() -> None:
    """flagship risk flag forces requires_confirm even under ceiling."""
    result = evaluate_policy(
        value=1.0,
        value_ceiling=10_000.0,
        risk_flags=["flagship"],
    )
    assert result.requires_confirm is True
    assert "flagship" in result.reason


def test_policy_risk_flag_first_of_kind_requires_confirm() -> None:
    """first_of_kind risk flag forces requires_confirm regardless of value."""
    result = evaluate_policy(risk_flags=["first_of_kind"])
    assert result.requires_confirm is True
    assert "first_of_kind" in result.reason


def test_policy_risk_flag_regulated_requires_confirm() -> None:
    """regulated risk flag forces requires_confirm regardless of value."""
    result = evaluate_policy(risk_flags=["regulated"])
    assert result.requires_confirm is True
    assert "regulated" in result.reason


def test_policy_unknown_risk_flag_does_not_require_confirm() -> None:
    """Risk flags not in the named set are ignored."""
    result = evaluate_policy(risk_flags=["low_priority"])
    assert result.requires_confirm is False


def test_policy_risk_flag_overrides_under_ceiling_value() -> None:
    """Risk flag forces confirm even when value is well under ceiling."""
    result = evaluate_policy(
        value=100.0,
        value_ceiling=50_000.0,
        risk_flags=["regulated"],
    )
    assert result.requires_confirm is True


def test_policy_multiple_gates_all_fire() -> None:
    """All gates firing produces a combined reason string."""
    result = evaluate_policy(
        value=2000.0,
        value_ceiling=1000.0,
        volume_state=20.0,
        volume_rate_cap=10.0,
        counterparty="unknown",
        allowlist=["known"],
        risk_flags=["flagship"],
    )
    assert result.requires_confirm is True
    # All four gate reasons should appear
    assert "flagship" in result.reason
    assert "ceiling" in result.reason
    assert "cap" in result.reason
    assert "allowlist" in result.reason


# ---------------------------------------------------------------------------
# _check_kill_switch — unit tests (mock Redis)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_no_redis_skips_check() -> None:
    """No Redis client → gate is skipped (no exception)."""
    await _check_kill_switch(None, "submit_po")  # must not raise


@pytest.mark.asyncio
async def test_kill_switch_no_redis_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    """No Redis client → gate skipped but a warning is emitted (observable at runtime)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="nce.autonomy.governor"):
        await _check_kill_switch(None, "my_action")

    assert any(
        "kill-switch gate NOT wired" in record.message and "my_action" in record.message
        for record in caplog.records
    ), "Expected a kill-switch-not-wired warning to be logged"


@pytest.mark.asyncio
async def test_kill_switch_per_tool_disabled_raises() -> None:
    """Per-tool key present in the hash → KillSwitchError."""
    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(side_effect=lambda key, field: field == "submit_po")

    with pytest.raises(KillSwitchError, match="submit_po"):
        await _check_kill_switch(mock_redis, "submit_po")


@pytest.mark.asyncio
async def test_kill_switch_global_disabled_raises() -> None:
    """Global ``"*"`` key present → KillSwitchError."""
    mock_redis = AsyncMock()

    async def _hexists(key: str, field: str) -> bool:
        # per-tool not set; global "*" is set
        return field == "*"

    mock_redis.hexists = AsyncMock(side_effect=_hexists)

    with pytest.raises(KillSwitchError, match="global"):
        await _check_kill_switch(mock_redis, "submit_po")


@pytest.mark.asyncio
async def test_kill_switch_redis_unreachable_raises_fail_closed() -> None:
    """Redis exception → KillSwitchError (fail-closed, never treated as enabled)."""
    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(side_effect=ConnectionError("Redis down"))

    with pytest.raises(KillSwitchError, match="fail-closed"):
        await _check_kill_switch(mock_redis, "submit_po")


@pytest.mark.asyncio
async def test_kill_switch_neither_disabled_passes() -> None:
    """Neither per-tool nor global key present → no exception."""
    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(return_value=False)

    await _check_kill_switch(mock_redis, "submit_po")  # must not raise


# ---------------------------------------------------------------------------
# @governed decorator integration — kill switch + policy wiring (mock conn)
# ---------------------------------------------------------------------------


def _make_governed_handler(
    value_ceiling: float | None = None,
    volume_rate_cap: float | None = None,
    allowlist: list[str] | None = None,
) -> tuple[Any, list[int]]:
    """Return a governed handler wired with Contract-B params + call counter."""
    call_log: list[int] = []

    @governed(
        action_type="test_action_b16",
        value_ceiling=value_ceiling,
        volume_rate_cap=volume_rate_cap,
        allowlist=allowlist,
    )
    async def handler(
        conn: Any,
        namespace_id: uuid.UUID,
        *,
        idempotency_key: str,
        confirm: bool = False,
        redis_client: Any = None,
        value: float | None = None,
        volume_state: float | None = None,
        counterparty: str | None = None,
        risk_flags: list[str] | None = None,
    ) -> dict[str, Any]:
        call_log.append(1)
        return {"done": True}

    return handler, call_log


def _mock_conn_in_tx() -> MagicMock:
    """Return a mock asyncpg connection that reports being inside a transaction."""
    mock_conn: MagicMock = MagicMock()
    mock_conn.is_in_transaction.return_value = True
    mock_conn.fetchrow = AsyncMock(return_value=None)  # key does not exist yet
    mock_conn.execute = AsyncMock()
    return mock_conn


@pytest.mark.asyncio
async def test_governed_over_ceiling_returns_pending_approval() -> None:
    """Over-ceiling value with confirm=True → pending_approval, no side effect."""
    handler, call_log = _make_governed_handler(value_ceiling=100.0)

    result = await handler(
        _mock_conn_in_tx(),
        uuid.uuid4(),
        idempotency_key="k-ceiling-1",
        confirm=True,
        value=500.0,
    )

    assert result["status"] == "pending_approval"
    assert "ceiling" in result.get("reason", "")
    assert call_log == [], "handler body must not execute when ceiling gate fires"


@pytest.mark.asyncio
async def test_governed_risk_flagged_returns_pending_regardless_of_value() -> None:
    """Risk flag fires even when value is far under ceiling."""
    handler, call_log = _make_governed_handler(value_ceiling=1_000_000.0)

    result = await handler(
        _mock_conn_in_tx(),
        uuid.uuid4(),
        idempotency_key="k-risk-1",
        confirm=True,
        value=1.0,
        risk_flags=["flagship"],
    )

    assert result["status"] == "pending_approval"
    assert "flagship" in result.get("reason", "")
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_over_volume_cap_returns_pending() -> None:
    """Volume exceeding cap → pending_approval."""
    handler, call_log = _make_governed_handler(volume_rate_cap=5.0)

    result = await handler(
        _mock_conn_in_tx(),
        uuid.uuid4(),
        idempotency_key="k-vol-1",
        confirm=True,
        volume_state=10.0,
    )

    assert result["status"] == "pending_approval"
    assert "cap" in result.get("reason", "")
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_non_allowlisted_counterparty_returns_pending() -> None:
    """Counterparty not on allowlist → pending_approval."""
    handler, call_log = _make_governed_handler(allowlist=["trusted_vendor"])

    result = await handler(
        _mock_conn_in_tx(),
        uuid.uuid4(),
        idempotency_key="k-allow-1",
        confirm=True,
        counterparty="unknown_vendor",
    )

    assert result["status"] == "pending_approval"
    assert "allowlist" in result.get("reason", "")
    assert call_log == []


@pytest.mark.asyncio
async def test_governed_kill_switch_per_tool_blocks() -> None:
    """Per-tool kill switch with confirm=True → KillSwitchError raised."""
    handler, call_log = _make_governed_handler()

    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(side_effect=lambda k, f: f == "test_action_b16")

    with pytest.raises(KillSwitchError):
        await handler(
            _mock_conn_in_tx(),
            uuid.uuid4(),
            idempotency_key="k-kill-1",
            confirm=True,
            redis_client=mock_redis,
        )

    assert call_log == []


@pytest.mark.asyncio
async def test_governed_kill_switch_global_blocks() -> None:
    """Global kill switch with confirm=True → KillSwitchError raised."""
    handler, call_log = _make_governed_handler()

    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(side_effect=lambda k, f: f == "*")

    with pytest.raises(KillSwitchError):
        await handler(
            _mock_conn_in_tx(),
            uuid.uuid4(),
            idempotency_key="k-kill-global-1",
            confirm=True,
            redis_client=mock_redis,
        )

    assert call_log == []


@pytest.mark.asyncio
async def test_governed_kill_switch_redis_unreachable_blocks_fail_closed() -> None:
    """Redis unreachable with confirm=True → KillSwitchError (fail-closed)."""
    handler, call_log = _make_governed_handler()

    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(side_effect=OSError("connection refused"))

    with pytest.raises(KillSwitchError):
        await handler(
            _mock_conn_in_tx(),
            uuid.uuid4(),
            idempotency_key="k-kill-down-1",
            confirm=True,
            redis_client=mock_redis,
        )

    assert call_log == []


@pytest.mark.asyncio
async def test_governed_no_confirm_skips_kill_switch() -> None:
    """confirm=False → pending_approval returned before kill switch is checked."""
    handler, call_log = _make_governed_handler()

    # Even with a Redis client that would block, no-confirm path must not touch it.
    mock_redis = AsyncMock()
    mock_redis.hexists = AsyncMock(return_value=True)  # would block if reached

    result = await handler(
        None,  # conn not needed on confirm=False path
        uuid.uuid4(),
        idempotency_key="k-no-confirm-1",
        confirm=False,
        redis_client=mock_redis,
    )

    assert result["status"] == "pending_approval"
    mock_redis.hexists.assert_not_awaited()  # kill switch must not be reached
    assert call_log == []
