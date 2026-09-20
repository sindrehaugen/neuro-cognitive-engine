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

Integration tests are ``@pytest.mark.integration`` — require a live
Postgres. The signature/registration/advertisement checks are pure logic
and stay unmarked so they run in the job that always runs.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.economy.reconcile_agreements import do_reconcile_agreements

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


async def _seed_posting(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    *,
    account: str,
    amount: str,
    period_id: str,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO economy_postings
                (namespace_id, event_id, event_type, line_no, account, amount, period_id)
            VALUES ($1::uuid, $2, 'test.b11_reconcile', 0, $3, $4, $5)
            """,
            str(namespace_id),
            f"evt-{uuid.uuid4().hex[:12]}",
            account,
            amount,
            period_id,
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
    assert row["field"] == "recognized_revenue"
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
