"""
tests/test_economy_reconcile_agreements.py
=============================================
Revenue-side Agreements<->GL reconciliation (charter Wave B-11).

What is gated here:
  1. A matched period (expected recognition == posted GL total) logs
     nothing to divergence_log at all -- mirrors finago.py's own "a zero
     delta is never logged" convention.
  2. A diverged period writes exactly one divergence_log row via
     record_divergence, with the correct nce_value/ext_value/materiality --
     and the SAME materiality/threshold decision record_divergence itself
     would make (never a second, independently-drifting implementation).
  3. A contract not yet due (schedule doesn't cover the period) is skipped
     and reported in not_due, contributing zero to expected_recognized_total
     -- mirrors do_recognize_recurring's own not_due handling exactly.
  4. Missing gl_account AND gl_account_prefix raises before any query runs.
  5. gl_account_prefix works as an alternative to an exact gl_account match.
  6. Tenant isolation: postings/contracts in a different namespace never
     leak into either side of the comparison.
  7. Alerting gate (``ALERTS_ENABLED_ENV``, default off): a material
     divergence still writes its ``divergence_log`` row either way, but
     ``dispatcher.dispatch_alert`` only fires when
     ``NCE_ECONOMY_RECONCILE_AGREEMENTS_ALERTS_ENABLED`` is explicitly
     truthy -- see the module docstring's "Alerting is opt-in" section and
     ``B11_DESIGN_BRIEF.md`` for why (this materiality rule has never been
     calibrated against real data; the host's own equivalent, calibrated by
     people with real account access, still ran 50% false positives).

Integration tests are ``@pytest.mark.integration`` — require a live
Postgres. The signature/registration/advertisement checks are pure logic
and stay unmarked so they run in the job that always runs.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.economy.reconcile_agreements import (
    ALERTS_ENABLED_ENV,
    do_reconcile_agreements,
)

_TOOL = "economy_reconcile_agreements"


# ---------------------------------------------------------------------------
# Pure logic -- checkable without a database.
# ---------------------------------------------------------------------------


def test_tool_registered_with_tenant_write_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert _TOOL in TOOL_REGISTRY, f"{_TOOL!r} not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY[_TOOL]
    assert spec.mutation is True
    assert spec.admin_only is False
    assert spec.cacheable is False
    assert spec.migration is False


def test_tool_is_advertised_with_an_input_schema() -> None:
    from nce.mcp_stdio_tools import TOOLS

    advertised = {t.name: t for t in TOOLS}
    assert _TOOL in advertised, f"{_TOOL} is registered but not advertised"
    schema = advertised[_TOOL].inputSchema
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"namespace_id", "period"}


@pytest.mark.parametrize(
    "params",
    [
        {"namespace_id": None, "period": "2026-01", "gl_account": "3000"},
        {"namespace_id": str(uuid.uuid4()), "period": "", "gl_account": "3000"},
        {"namespace_id": str(uuid.uuid4()), "period": "2026-01"},
    ],
)
def test_invalid_arguments_raise_value_error_before_any_db_work(params: dict[str, Any]) -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(do_reconcile_agreements(None, params))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration helpers.
# ---------------------------------------------------------------------------


class _EngineStub:
    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


def _contract_id(tag: str) -> str:
    return f"B11-{tag}-{uuid.uuid4().hex[:8]}"


async def _seed_contract(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    contract_id: str,
    annual_amount: str,
    start_period: str,
    status: str = "active",
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO economy_contracts
                (namespace_id, contract_id, status, annual_amount, start_period,
                 next_renewal_date)
            VALUES ($1::uuid, $2, $3, $4, $5, '2027-01-01'::date)
            """,
            str(namespace_id),
            contract_id,
            status,
            annual_amount,
            start_period,
        )


_COUNTER_ACCOUNT = "2400"  # matches test_economy_finago.py's own balancing-account precedent


async def _seed_posting(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    account: str,
    amount: str,
    period_id: str,
) -> None:
    """Write a balanced two-line posting event: `account` gets `amount`, a
    counter account gets the exact negative offset. economy_postings
    enforces double-entry (sum=0 per event_id) at the database level via a
    trigger -- a single unbalanced line is refused outright, not silently
    accepted (see test_economy_finago.py's own `lines=[(acct, amt), (ctr,
    -amt)]` convention, followed here rather than invented)."""
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        await conn.execute(
            """
            INSERT INTO economy_postings
                (namespace_id, event_id, event_type, line_no, account, amount, period_id)
            VALUES
                ($1::uuid, $2, 'test.b11_reconcile', 0, $3, $4::numeric, $5),
                ($1::uuid, $2, 'test.b11_reconcile', 1, $6, -($4::numeric), $5)
            """,
            str(namespace_id),
            event_id,
            account,
            amount,
            period_id,
            _COUNTER_ACCOUNT,
        )


async def _divergence_count(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> int:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        return await conn.fetchval(
            "SELECT count(*) FROM divergence_log WHERE namespace_id = $1 AND engine = 'economy'",
            namespace_id,
        )


# ---------------------------------------------------------------------------
# 1. Matched period -- nothing logged.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matched_period_logs_no_divergence(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    contract_id = _contract_id("MATCH")
    # annual_amount=1200.00, start_period=2026-01 -> base period amount 100.00
    await _seed_contract(
        pg_pool,
        namespace_id,
        contract_id=contract_id,
        annual_amount="1200.00",
        start_period="2026-01",
    )
    await _seed_posting(pg_pool, namespace_id, account="3900", amount="100.00", period_id="2026-01")

    engine = _EngineStub(pg_pool)
    result = await do_reconcile_agreements(
        engine, {"namespace_id": namespace_id, "period": "2026-01", "gl_account": "3900"}
    )

    assert result["expected_recognized_total"] == pytest.approx(100.00)
    assert result["actual_gl_total"] == pytest.approx(100.00)
    assert result["delta"] == pytest.approx(0.0)
    assert result["materiality"] is None
    assert result["material"] is False
    assert result["contracts_due"] == 1
    # The aggregate-scope caveat is present on every call, not only diverged
    # ones -- a caller must never have to guess the scope from field names.
    assert result["comparison_scope"] == "aggregate"
    assert "cannot attribute" in result["attribution_caveat"].lower()

    assert await _divergence_count(pg_pool, namespace_id) == 0


# ---------------------------------------------------------------------------
# 2. Diverged period -- exactly one divergence_log row.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diverged_period_writes_one_divergence_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    contract_id = _contract_id("DIVERGE")
    await _seed_contract(
        pg_pool,
        namespace_id,
        contract_id=contract_id,
        annual_amount="1200.00",
        start_period="2026-02",
    )
    # Posted GL is 80.00 against an expected 100.00 -- a real gap.
    await _seed_posting(pg_pool, namespace_id, account="3900", amount="80.00", period_id="2026-02")

    engine = _EngineStub(pg_pool)
    result = await do_reconcile_agreements(
        engine, {"namespace_id": namespace_id, "period": "2026-02", "gl_account": "3900"}
    )

    assert result["expected_recognized_total"] == pytest.approx(100.00)
    assert result["actual_gl_total"] == pytest.approx(80.00)
    assert result["delta"] == pytest.approx(20.00)
    assert result["materiality"] is not None
    assert result["materiality"] == pytest.approx(20.00 / 100.00)
    assert result["comparison_scope"] == "aggregate"
    assert "cannot attribute" in result["attribution_caveat"].lower()

    assert await _divergence_count(pg_pool, namespace_id) == 1

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        row = await conn.fetchrow(
            "SELECT entity, field, nce_value, ext_value, materiality FROM divergence_log "
            "WHERE namespace_id = $1 AND engine = 'economy'",
            namespace_id,
        )
    assert row is not None
    assert row["entity"] == "agreement_gl:2026-02:3900"
    # The field name itself states the aggregate scope -- a reader querying
    # divergence_log directly, with no access to this module's source, must
    # not be able to mistake this for a per-contract finding.
    assert row["field"] == "recognized_revenue_aggregate_not_attributable_to_contract"
    assert row["nce_value"] == "100.00"
    assert row["ext_value"] == "80.00"
    assert row["materiality"] == pytest.approx(20.00 / 100.00)


# ---------------------------------------------------------------------------
# 3. A contract not yet due contributes nothing and is reported.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contract_not_due_is_skipped_and_reported(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    contract_id = _contract_id("NOTDUE")
    # Starts 2027-01 -- its schedule does not cover 2026-01 at all.
    await _seed_contract(
        pg_pool,
        namespace_id,
        contract_id=contract_id,
        annual_amount="1200.00",
        start_period="2027-01",
    )

    engine = _EngineStub(pg_pool)
    result = await do_reconcile_agreements(
        engine, {"namespace_id": namespace_id, "period": "2026-01", "gl_account": "3900"}
    )

    assert result["expected_recognized_total"] == pytest.approx(0.0)
    assert result["contracts_due"] == 0
    assert contract_id in result["not_due"]


# ---------------------------------------------------------------------------
# 4. gl_account_prefix works as an alternative.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gl_account_prefix_matches_like_exact_account(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    contract_id = _contract_id("PREFIX")
    await _seed_contract(
        pg_pool,
        namespace_id,
        contract_id=contract_id,
        annual_amount="1200.00",
        start_period="2026-03",
    )
    await _seed_posting(pg_pool, namespace_id, account="3901", amount="100.00", period_id="2026-03")

    engine = _EngineStub(pg_pool)
    result = await do_reconcile_agreements(
        engine,
        {"namespace_id": namespace_id, "period": "2026-03", "gl_account_prefix": "39"},
    )

    assert result["actual_gl_total"] == pytest.approx(100.00)
    assert result["delta"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 5. Tenant isolation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_isolation_other_namespace_contracts_and_postings_excluded(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    other_ns = uuid.uuid4()
    async with pg_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO namespaces (id, slug) VALUES ($1, $2)",
            other_ns,
            f"tenant-b11-{other_ns.hex[:8]}",
        )

    other_contract = _contract_id("OTHERNS")
    await _seed_contract(
        pg_pool,
        other_ns,
        contract_id=other_contract,
        annual_amount="1200.00",
        start_period="2026-04",
    )
    await _seed_posting(pg_pool, other_ns, account="3900", amount="999.00", period_id="2026-04")

    engine = _EngineStub(pg_pool)
    result = await do_reconcile_agreements(
        engine, {"namespace_id": namespace_id, "period": "2026-04", "gl_account": "3900"}
    )

    assert result["expected_recognized_total"] == pytest.approx(0.0)
    assert result["actual_gl_total"] == pytest.approx(0.0)
    assert result["contracts_due"] == 0


# ---------------------------------------------------------------------------
# 6. Alerting gate -- off by default, opt-in via env var.
#
# The row write and the page are two separate questions: both tests below
# use the same diverged-period setup as
# test_diverged_period_writes_one_divergence_row (materiality 0.2, above the
# default 0.1 threshold), so `material` is True in both -- what differs is
# whether dispatch_alert actually fires.
# ---------------------------------------------------------------------------


async def _seed_diverged_period(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    tag: str,
    period: str,
) -> None:
    contract_id = _contract_id(tag)
    await _seed_contract(
        pg_pool,
        namespace_id,
        contract_id=contract_id,
        annual_amount="1200.00",
        start_period=period,
    )
    # Posted GL is 80.00 against an expected 100.00 -- a real, material gap.
    await _seed_posting(pg_pool, namespace_id, account="3900", amount="80.00", period_id=period)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_material_divergence_does_not_alert_by_default(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this gate exists to prevent: an uncalibrated rule must
    not page anyone with the env var unset -- the default state in
    production until someone flips it on after validating against real
    data."""
    monkeypatch.delenv(ALERTS_ENABLED_ENV, raising=False)
    period = "2026-05"
    await _seed_diverged_period(pg_pool, namespace_id, tag="NOALERT", period=period)

    engine = _EngineStub(pg_pool)
    mock_dispatch = AsyncMock()
    with patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch):
        result = await do_reconcile_agreements(
            engine, {"namespace_id": namespace_id, "period": period, "gl_account": "3900"}
        )

    assert result["material"] is True
    assert result["alerts_enabled"] is False
    mock_dispatch.assert_not_called()

    # The row must still be written -- the gate suppresses the page, not the
    # data. Losing the divergence itself would be the over-reach the module
    # docstring explicitly warns against.
    assert await _divergence_count(pg_pool, namespace_id) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_material_divergence_alerts_when_explicitly_enabled(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction the gate must not break: once someone flips
    ``ALERTS_ENABLED_ENV`` on, the exact same material divergence DOES
    page -- this is an opt-in switch, not a channel this module has quietly
    disabled for good."""
    monkeypatch.setenv(ALERTS_ENABLED_ENV, "true")
    period = "2026-06"
    await _seed_diverged_period(pg_pool, namespace_id, tag="ALERTON", period=period)

    engine = _EngineStub(pg_pool)
    mock_dispatch = AsyncMock()
    with patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch):
        result = await do_reconcile_agreements(
            engine, {"namespace_id": namespace_id, "period": period, "gl_account": "3900"}
        )

    assert result["material"] is True
    assert result["alerts_enabled"] is True
    mock_dispatch.assert_called_once()

    assert await _divergence_count(pg_pool, namespace_id) == 1
