"""
tests/test_economy_customer_invoices.py
==========================================
B-13: governed confirm-first customer-invoice proposal generation
(2026-09-20).

Unit tier (no DB) mirrors ``test_economy_billing_runs.py``'s own split:
only the "no confirm -> pending, body never runs" path is mockable without
a real Postgres connection.

Integration tier proves, against a real database:
  1. An unknown billing_candidate_id raises before any write happens.
  2. The happy path: a real BILLING_CANDIDATE (seeded directly, mirroring
     what do_generate_billing_run would have produced) becomes a
     'proposal' CUSTOMER_INVOICE with correct VAT math (25% default) and
     no invoice_number/kid assigned yet (B-13 ships proposal only).
  3. A second proposal attempt against the SAME candidate (a different
     idempotency key, not a replay of the same call) is refused --
     UNIQUE(namespace_id, billing_candidate_id) as defense-in-depth beyond
     @governed's own idempotency-key dedup.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.economy.customer_invoices import (
    BillingCandidateNotFoundError,
    CustomerInvoiceAlreadyExistsError,
    do_propose_customer_invoice,
)

# ---------------------------------------------------------------------------
# Unit tier -- no DB.
# ---------------------------------------------------------------------------


def _make_mock_conn(queue_id: uuid.UUID | None = None) -> MagicMock:
    conn = MagicMock()
    conn.is_in_transaction.return_value = True
    qid = queue_id or uuid.uuid4()

    async def _mock_fetchrow(query: str, *args: Any) -> Any:
        if "economy_billing_candidates" in query or "economy_customer_invoices" in query:
            raise AssertionError(
                f"do_propose_customer_invoice's body ran without confirm=True -- "
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
async def test_propose_without_confirm_is_pending_and_does_not_run() -> None:
    mock_conn = _make_mock_conn()
    ns_id = uuid.uuid4()
    candidate_id = uuid.uuid4()

    result = await do_propose_customer_invoice(
        mock_conn,
        ns_id,
        idempotency_key=f"propose-invoice-{candidate_id}",
        confirm=False,
        engine=None,
        billing_candidate_id=str(candidate_id),
    )

    assert result["status"] == "pending_approval"
    mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tier -- real Postgres via pg_pool/namespace_id fixtures.
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: Any, namespace_id: uuid.UUID) -> None:
    """Seed node_ownership_registry -- same gap as test_economy_billing_runs.py's
    own helper (the namespace_id fixture never runs the orchestrator path
    that seeds it)."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _seed_billing_candidate(
    pg_pool: Any,
    namespace_id: uuid.UUID,
    *,
    total_amount: str = "500.00",
    currency: str = "NOK",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a real economy_billing_candidates row directly, mirroring what
    do_generate_billing_run (B-12) would have produced -- avoids a
    cross-module dependency on that function's own fixture chain."""
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

        run_id = uuid.uuid4()
        run_label = f"BILLING_RUN:{run_id}"
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'BILLING_RUN', $2::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            run_label,
            namespace_id,
        )
        await conn.execute(
            """
            INSERT INTO economy_billing_runs
                (id, namespace_id, node_label, period_start, period_end, status, candidate_count)
            VALUES ($1, $2::uuid, $3, '2026-09-01', '2026-09-30', 'confirmed', 1)
            """,
            run_id,
            namespace_id,
            run_label,
        )

        candidate_id = uuid.uuid4()
        candidate_label = f"BILLING_CANDIDATE:{candidate_id}"
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'BILLING_CANDIDATE', $2::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            candidate_label,
            namespace_id,
        )
        await conn.execute(
            """
            INSERT INTO economy_billing_candidates
                (id, namespace_id, node_label, billing_run_id, customer_id,
                 agreement_id, period_start, period_end, currency, total_amount, status)
            VALUES ($1, $2::uuid, $3, $4, $5, $6, '2026-09-01', '2026-09-30', $7, $8, 'draft')
            """,
            candidate_id,
            namespace_id,
            candidate_label,
            run_id,
            customer["id"],
            agreement["id"],
            currency,
            total_amount,
        )
    return candidate_id, customer["id"], agreement["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_propose_customer_invoice_candidate_not_found_raises(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        with pytest.raises(BillingCandidateNotFoundError):
            await do_propose_customer_invoice(
                conn,
                namespace_id,
                idempotency_key=f"propose-missing-{uuid.uuid4()}",
                confirm=True,
                engine=None,
                billing_candidate_id=str(uuid.uuid4()),
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_propose_customer_invoice_happy_path_computes_vat(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    candidate_id, customer_id, _agreement_id = await _seed_billing_candidate(
        pg_pool, namespace_id, total_amount="500.00"
    )

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        outer = await do_propose_customer_invoice(
            conn,
            namespace_id,
            idempotency_key=f"propose-happy-{candidate_id}",
            confirm=True,
            engine=None,
            billing_candidate_id=str(candidate_id),
        )

    assert outer["status"] == "executed"
    result = outer["result"]
    assert result["status"] == "proposal"
    assert result["billing_candidate_id"] == str(candidate_id)
    assert result["customer_id"] == str(customer_id)
    assert result["currency"] == "NOK"
    assert result["subtotal_amount"] == pytest.approx(500.00)
    assert result["vat_rate_pct"] == pytest.approx(25.00)
    assert result["vat_amount"] == pytest.approx(125.00)
    assert result["total_amount"] == pytest.approx(625.00)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, invoice_number, kid, subtotal_amount, vat_amount, total_amount, "
            "billing_candidate_id, customer_id FROM economy_customer_invoices "
            "WHERE id = $1::uuid",
            uuid.UUID(result["customer_invoice_id"]),
        )

    assert row["status"] == "proposal"
    assert row["invoice_number"] is None
    assert row["kid"] is None
    assert row["billing_candidate_id"] == candidate_id
    assert row["customer_id"] == customer_id
    assert row["subtotal_amount"] == pytest.approx(500.00)
    assert row["vat_amount"] == pytest.approx(125.00)
    assert row["total_amount"] == pytest.approx(625.00)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_propose_customer_invoice_second_attempt_on_same_candidate_refuses(
    pg_pool: Any, namespace_id: uuid.UUID
) -> None:
    """UNIQUE(namespace_id, billing_candidate_id) as defense-in-depth: a
    SECOND, differently-keyed proposal call against the same candidate must
    refuse -- distinct from @governed's own idempotency-key replay dedup,
    which only catches an exact-same-call replay."""
    await _seed_ownership(pg_pool, namespace_id)
    candidate_id, _customer_id, _agreement_id = await _seed_billing_candidate(pg_pool, namespace_id)

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        first = await do_propose_customer_invoice(
            conn,
            namespace_id,
            idempotency_key=f"propose-first-{candidate_id}",
            confirm=True,
            engine=None,
            billing_candidate_id=str(candidate_id),
        )
    assert first["status"] == "executed"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        with pytest.raises(CustomerInvoiceAlreadyExistsError):
            await do_propose_customer_invoice(
                conn,
                namespace_id,
                idempotency_key=f"propose-second-{candidate_id}",
                confirm=True,
                engine=None,
                billing_candidate_id=str(candidate_id),
            )
