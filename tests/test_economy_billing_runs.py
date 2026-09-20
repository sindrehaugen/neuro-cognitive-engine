"""
tests/test_economy_billing_runs.py
====================================
B-12: governed confirm-first billing-run generation (2026-09-20).

Unit tier (no DB) mirrors ``test_field_tech_approve_time_entry.py``'s own
split: only the "no confirm -> pending, body never runs" path is mockable
without a real Postgres connection.

Integration tier proves, against a real database:
  1. An unknown agreement_id raises before any pricing or writes happen.
  2. An agreement with no ``covers`` edge is skipped, not billed and not an
     error -- a run over only such agreements still confirms, empty.
  3. The happy path: one agreement covering two HUDDLE rooms produces one
     BILLING_CANDIDATE with one aggregated line, priced from
     ``price_rules.json``'s real ``sla_room_pricing`` rates.
  4. A room whose C-2 category has no price tier (ALL_HANDS -- a true
     orphan per ``room_category_pricing.py``) refuses the WHOLE run: the
     confirmed agreement from case 3's fixture is NOT billed either when
     it's in the same run as the unpriced room, and the run row alone
     records the refusal (module docstring: "refuse-and-name, at the run
     level").
  5. A room whose category is CUSTOM_PROJECT (CONFERENCE_MEDIUM/
     CONFERENCE_LARGE, Sindre's follow-up ruling on the three-way collapse)
     is skipped, not billed and NOT a refusal -- distinct from case 4's
     true-orphan refusal, proving the two outcomes aren't conflated.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.economy.billing_runs import (
    AgreementNotFoundError,
    do_generate_billing_run,
)

# ---------------------------------------------------------------------------
# Unit tier -- no DB.
# ---------------------------------------------------------------------------


def _make_mock_conn(queue_id: uuid.UUID | None = None) -> MagicMock:
    conn = MagicMock()
    conn.is_in_transaction.return_value = True
    qid = queue_id or uuid.uuid4()

    async def _mock_fetchrow(query: str, *args: Any) -> Any:
        if "agreements" in query or "economy_billing" in query:
            raise AssertionError(
                f"do_generate_billing_run's body ran without confirm=True -- "
                f"query was: {query[:80]!r}"
            )
        if "INSERT INTO action_approval_queue" in query:
            return {"id": qid}
        return None

    conn.fetchrow = AsyncMock(side_effect=_mock_fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_generate_without_confirm_is_pending_and_does_not_run() -> None:
    mock_conn = _make_mock_conn()
    ns_id = uuid.uuid4()
    agreement_id = uuid.uuid4()

    result = await do_generate_billing_run(
        mock_conn,
        ns_id,
        idempotency_key=f"billing-run-{agreement_id}",
        confirm=False,
        engine=None,
        period_start="2026-09-01",
        period_end="2026-09-30",
        agreement_ids=[str(agreement_id)],
    )

    assert result["status"] == "pending_approval"
    mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tier -- real Postgres via pg_pool/namespace_id fixtures.
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: Any, namespace_id: uuid.UUID) -> None:
    """Seed node_ownership_registry for this test namespace.

    The ``namespace_id`` fixture only inserts a bare row into ``namespaces``
    -- it does not go through the orchestrator path that seeds ownership on
    namespace creation. Without this, ``assert_owner`` denies BILLING_RUN/
    BILLING_CANDIDATE writes by default even though node-ownership.json
    registers them, exactly as ``tests/test_agreements_sla.py`` does for its
    own namespace fixture.
    """
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _seed_customer_and_agreement(
    pg_pool: Any, namespace_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with pg_pool.acquire() as conn:
        customer = await conn.fetchrow(
            """
            INSERT INTO sales_customers (namespace_id, name)
            VALUES ($1::uuid, $2)
            RETURNING id
            """,
            namespace_id,
            f"Test Customer {uuid.uuid4().hex[:8]}",
        )
        agreement = await conn.fetchrow(
            """
            INSERT INTO agreements (namespace_id, title, customer_id)
            VALUES ($1::uuid, $2, $3::uuid)
            RETURNING id
            """,
            namespace_id,
            f"Test Agreement {uuid.uuid4().hex[:8]}",
            customer["id"],
        )
    return customer["id"], agreement["id"]


async def _seed_covered_fl(
    pg_pool: Any,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
    *,
    category_id: str,
    fl_label: str | None = None,
) -> str:
    """Create a FUNCTIONAL_LOCATION node, assign it a C-2 category, and wire
    the Agreement -[covers]-> FL edge -- the exact graph shape
    ``do_set_sla_coverage``/``set_fl_room_category`` each produce, built
    directly here rather than through those functions to keep this fixture
    independent of either module's own internals changing shape."""
    fl_label = fl_label or f"FL:TESTNS:{uuid.uuid4().hex[:8].upper()}"
    agreement_label = f"Agreement:{agreement_id}"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'FUNCTIONAL_LOCATION', $2::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            fl_label,
            namespace_id,
        )
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'has_category', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            fl_label,
            f"ROOM_CATEGORY:{category_id.upper()}",
            namespace_id,
        )
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
            VALUES ($1, 'covers', $2, 1.0, $3::uuid, 'agent')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            agreement_label,
            fl_label,
            namespace_id,
        )
    return fl_label


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_billing_run_unknown_agreement_raises(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        with pytest.raises(AgreementNotFoundError):
            await do_generate_billing_run(
                conn,
                namespace_id,
                idempotency_key=f"billing-run-missing-{uuid.uuid4()}",
                confirm=True,
                engine=None,
                period_start="2026-09-01",
                period_end="2026-09-30",
                agreement_ids=[str(uuid.uuid4())],
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_billing_run_skips_agreement_with_no_coverage(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    _customer_id, agreement_id = await _seed_customer_and_agreement(pg_pool, namespace_id)

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        outer = await do_generate_billing_run(
            conn,
            namespace_id,
            idempotency_key=f"billing-run-uncovered-{agreement_id}",
            confirm=True,
            engine=None,
            period_start="2026-09-01",
            period_end="2026-09-30",
            agreement_ids=[str(agreement_id)],
        )

    assert outer["status"] == "executed"
    result = outer["result"]
    assert result["status"] == "confirmed"
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert result["skipped_agreements"] == [str(agreement_id)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_billing_run_happy_path_prices_from_real_rates(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    customer_id, agreement_id = await _seed_customer_and_agreement(pg_pool, namespace_id)
    await _seed_covered_fl(pg_pool, namespace_id, agreement_id, category_id="HUDDLE")
    await _seed_covered_fl(pg_pool, namespace_id, agreement_id, category_id="HUDDLE")

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        outer = await do_generate_billing_run(
            conn,
            namespace_id,
            idempotency_key=f"billing-run-happy-{agreement_id}",
            confirm=True,
            engine=None,
            period_start="2026-09-01",
            period_end="2026-09-30",
            agreement_ids=[str(agreement_id)],
        )

    assert outer["status"] == "executed"
    result = outer["result"]
    assert result["status"] == "confirmed"
    assert result["candidate_count"] == 1
    assert result["skipped_agreements"] == []

    candidate = result["candidates"][0]
    assert candidate["agreement_id"] == str(agreement_id)
    assert candidate["customer_id"] == str(customer_id)
    assert candidate["currency"] == "NOK"
    assert len(candidate["lines"]) == 1
    line = candidate["lines"][0]
    assert line["room_category"] == "huddle_space"
    assert line["count"] == 2
    # Two rooms at the same unit rate -- total is exactly double the unit rate,
    # not a hard-coded NOK figure, so this stays correct if price-rules.json's
    # rate ever changes.
    assert candidate["total_amount"] == pytest.approx(line["unit_monthly_rate"] * 2)

    async with pg_pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT status, candidate_count FROM economy_billing_runs WHERE id = $1::uuid",
            uuid.UUID(result["billing_run_id"]),
        )
        candidate_row = await conn.fetchrow(
            "SELECT status, total_amount, agreement_id, customer_id FROM economy_billing_candidates "
            "WHERE id = $1::uuid",
            uuid.UUID(candidate["billing_candidate_id"]),
        )
        line_rows = await conn.fetch(
            "SELECT price_tier, room_count, covered_fl_labels FROM economy_billing_candidate_lines "
            "WHERE billing_candidate_id = $1::uuid",
            uuid.UUID(candidate["billing_candidate_id"]),
        )

    assert run_row["status"] == "confirmed"
    assert run_row["candidate_count"] == 1
    assert candidate_row["status"] == "draft"
    assert candidate_row["agreement_id"] == agreement_id
    assert candidate_row["customer_id"] == customer_id
    assert len(line_rows) == 1
    assert line_rows[0]["price_tier"] == "huddle_space"
    assert line_rows[0]["room_count"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_billing_run_refuses_whole_run_on_unpriced_room(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    _customer_id, priceable_agreement_id = await _seed_customer_and_agreement(pg_pool, namespace_id)
    await _seed_covered_fl(pg_pool, namespace_id, priceable_agreement_id, category_id="HUDDLE")

    _customer_id2, unpriced_agreement_id = await _seed_customer_and_agreement(pg_pool, namespace_id)
    # ALL_HANDS is a true orphan (room_category_pricing.py) -- no price tier
    # exists at all, by Sindre's own ruling, not a bug to fix here.
    await _seed_covered_fl(pg_pool, namespace_id, unpriced_agreement_id, category_id="ALL_HANDS")

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        outer = await do_generate_billing_run(
            conn,
            namespace_id,
            idempotency_key=f"billing-run-refuse-{unpriced_agreement_id}",
            confirm=True,
            engine=None,
            period_start="2026-09-01",
            period_end="2026-09-30",
            agreement_ids=[str(priceable_agreement_id), str(unpriced_agreement_id)],
        )

    assert outer["status"] == "executed"
    result = outer["result"]
    assert result["status"] == "failed"
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert "ALL_HANDS" in result["failure_reason"]

    async with pg_pool.acquire() as conn:
        run_row = await conn.fetchrow(
            "SELECT status, failure_reason FROM economy_billing_runs WHERE id = $1::uuid",
            uuid.UUID(result["billing_run_id"]),
        )
        candidate_rows = await conn.fetch(
            "SELECT id FROM economy_billing_candidates WHERE billing_run_id = $1::uuid",
            uuid.UUID(result["billing_run_id"]),
        )

    assert run_row["status"] == "failed"
    assert "ALL_HANDS" in run_row["failure_reason"]
    # The whole run refuses -- the priceable agreement gets NO candidate either.
    assert len(candidate_rows) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_billing_run_skips_custom_project_room_without_refusing(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    """CONFERENCE_MEDIUM/CONFERENCE_LARGE (Sindre's three-way-collapse ruling,
    2026-09-20) resolve to CUSTOM_PROJECT -- priced elsewhere, not a gap.
    Distinct from test_..._refuses_whole_run_on_unpriced_room: a
    custom-project room must never trigger a refusal, and a HUDDLE room in
    the SAME agreement must still be billed."""
    await _seed_ownership(pg_pool, namespace_id)
    customer_id, agreement_id = await _seed_customer_and_agreement(pg_pool, namespace_id)
    await _seed_covered_fl(pg_pool, namespace_id, agreement_id, category_id="HUDDLE")
    await _seed_covered_fl(pg_pool, namespace_id, agreement_id, category_id="CONFERENCE_MEDIUM")

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        outer = await do_generate_billing_run(
            conn,
            namespace_id,
            idempotency_key=f"billing-run-custom-project-{agreement_id}",
            confirm=True,
            engine=None,
            period_start="2026-09-01",
            period_end="2026-09-30",
            agreement_ids=[str(agreement_id)],
        )

    assert outer["status"] == "executed"
    result = outer["result"]
    assert result["status"] == "confirmed"
    assert result["candidate_count"] == 1
    assert result["skipped_agreements"] == []

    candidate = result["candidates"][0]
    assert len(candidate["lines"]) == 1
    assert candidate["lines"][0]["room_category"] == "huddle_space"
    assert candidate["lines"][0]["count"] == 1

    custom_project_labels = result["custom_project_fl_labels"]
    assert len(custom_project_labels) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_billing_run_skips_agreement_that_is_entirely_custom_project(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    """An agreement covering ONLY custom-project rooms has nothing for
    sla_room_pricing to bill -- skipped like a no-coverage agreement, not a
    refusal and not a candidate with zero lines."""
    await _seed_ownership(pg_pool, namespace_id)
    _customer_id, agreement_id = await _seed_customer_and_agreement(pg_pool, namespace_id)
    await _seed_covered_fl(pg_pool, namespace_id, agreement_id, category_id="CONFERENCE_LARGE")

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        outer = await do_generate_billing_run(
            conn,
            namespace_id,
            idempotency_key=f"billing-run-all-custom-project-{agreement_id}",
            confirm=True,
            engine=None,
            period_start="2026-09-01",
            period_end="2026-09-30",
            agreement_ids=[str(agreement_id)],
        )

    assert outer["status"] == "executed"
    result = outer["result"]
    assert result["status"] == "confirmed"
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert result["skipped_agreements"] == [str(agreement_id)]
    assert result["custom_project_fl_labels"] != []
