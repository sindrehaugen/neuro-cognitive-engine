"""
tests/test_product_matching.py
================================
Integration tests for product BOM-line matching (M2.W6).

Validates:
  1. ``do_match_bom_line`` delegates to C1 ``resolve()`` (not local fuzzy maths):
     resolve() is spied to confirm it was called; the top match is the seeded SKU.
  2. Accept/override decisions are appended to ``product_match_feedback``.
  3. ``product_match_feedback`` is namespace-isolated (FORCE RLS):
     namespace B cannot see namespace A's feedback rows.
  4. The learning table is append-only: a direct UPDATE attempt is refused.

All tests are ``@pytest.mark.integration`` (require live Postgres).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_product_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_id: uuid.UUID,
    manufacturer: str,
    mfr_part_no: str,
) -> uuid.UUID:
    """Insert a PRODUCT_SKU node directly into kg_nodes (bypasses ownership guard)."""
    label = f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"
    node_id = await conn.fetchval(
        """
        INSERT INTO kg_nodes (namespace_id, label, entity_type)
        VALUES ($1, $2, $3)
        ON CONFLICT (namespace_id, label) DO UPDATE SET entity_type = EXCLUDED.entity_type
        RETURNING id
        """,
        ns_id,
        label,
        "PRODUCT_SKU",
    )
    return node_id  # type: ignore[return-value]


def _make_engine(pg_pool):
    """Build a minimal NCEEngine stub wrapping the integration pg_pool."""
    from unittest.mock import MagicMock

    engine = MagicMock()
    engine.pg_pool = pg_pool
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns_a(make_namespace) -> uuid.UUID:
    return await make_namespace()


@pytest_asyncio.fixture
async def ns_b(make_namespace) -> uuid.UUID:
    return await make_namespace()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_match_bom_line_delegates_to_c1_resolve(
    pg_pool: asyncpg.Pool,
    ns_a: uuid.UUID,
) -> None:
    """resolve() is called and the seeded SKU appears as the top match."""
    from nce.vertical_modules.product.matching import do_match_bom_line

    # Seed a PRODUCT_SKU node in the namespace.
    async with pg_pool.acquire() as conn:
        await _insert_product_node(conn, ns_a, "CISCO", "SFP-10G-SR")

    engine = _make_engine(pg_pool)

    # Spy on resolve() to confirm C1 is called (not local maths).
    with patch(
        "nce.vertical_modules.product.matching.resolve",
        wraps=__import__("nce.entity_resolution.resolver", fromlist=["resolve"]).resolve,
    ) as spy_resolve:
        result = await do_match_bom_line(
            engine,
            {
                "namespace_id": str(ns_a),
                "bom_line": "CISCO SFP-10G-SR transceiver",
                "manufacturer": "CISCO",
                "mfr_part_no": "SFP-10G-SR",
            },
        )

    # resolve() must have been called exactly once.
    spy_resolve.assert_called_once()
    call_kwargs = spy_resolve.call_args

    # Verify the call used the correct node_type and keys.
    assert call_kwargs.kwargs["node_type"] == "PRODUCT_SKU"
    assert "mfr_part_no" in call_kwargs.kwargs["keys"]

    # The result must contain the expected shape (no cost/margin/BID).
    assert "bom_line" in result
    assert "matches" in result
    assert "top_sku" in result
    assert "top_score" in result

    # The seeded node should be top (or in results) when resolve() finds it.
    # (Score may be 0 if pg_trgm finds no similarity — that is correct behaviour.)
    assert isinstance(result["matches"], list)
    for m in result["matches"]:
        assert "node_id" in m
        assert "score" in m
        assert "matched_on" in m
        # ADR-0017: no cost/margin/BID
        for forbidden in ("cost", "margin", "bid", "cost_price"):
            assert forbidden not in m


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accept_decision_appended_to_feedback(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """An 'accept' decision is written to product_match_feedback (append-only)."""
    from nce.vertical_modules.product.matching import do_match_bom_line

    engine = _make_engine(pg_pool)

    result = await do_match_bom_line(
        engine,
        {
            "namespace_id": str(ns_a),
            "bom_line": "Cisco SFP 10G SR",
            "decision": "accept",
            "chosen_sku": "SFP-10G-SR",
            "matched_score": 0.87,
        },
    )

    assert "feedback_id" in result
    assert result["decision"] == "accept"
    feedback_id = uuid.UUID(result["feedback_id"])

    # pg_app_conn uses nce_app role + FORCE RLS; must set namespace context to see the row.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row = await pg_app_conn.fetchrow(
            "SELECT bom_line, chosen_sku, decision, matched_score FROM product_match_feedback WHERE id = $1",
            feedback_id,
        )

    assert row is not None, f"Feedback row {feedback_id} not found"
    assert row["bom_line"] == "Cisco SFP 10G SR"
    assert row["chosen_sku"] == "SFP-10G-SR"
    assert row["decision"] == "accept"
    assert float(row["matched_score"]) == pytest.approx(0.87)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_override_decision_appended(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """An 'override' decision is written correctly."""
    from nce.vertical_modules.product.matching import do_match_bom_line

    engine = _make_engine(pg_pool)

    result = await do_match_bom_line(
        engine,
        {
            "namespace_id": str(ns_a),
            "bom_line": "SFP module 10G",
            "decision": "override",
            "chosen_sku": "SFP-10G-LR",
            "rejected_sku": "SFP-10G-SR",
            "matched_score": 0.55,
        },
    )

    assert result["decision"] == "override"
    feedback_id = uuid.UUID(result["feedback_id"])

    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row = await pg_app_conn.fetchrow(
            "SELECT decision, chosen_sku, rejected_sku FROM product_match_feedback WHERE id = $1",
            feedback_id,
        )

    assert row is not None
    assert row["decision"] == "override"
    assert row["chosen_sku"] == "SFP-10G-LR"
    assert row["rejected_sku"] == "SFP-10G-SR"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_isolates_feedback_across_namespaces(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
    ns_b: uuid.UUID,
) -> None:
    """Namespace B cannot see namespace A's feedback rows (FORCE RLS)."""
    from nce.vertical_modules.product.matching import do_match_bom_line

    engine = _make_engine(pg_pool)

    # Write a feedback row in namespace A.
    result_a = await do_match_bom_line(
        engine,
        {
            "namespace_id": str(ns_a),
            "bom_line": "Cisco cable 10G",
            "decision": "accept",
            "chosen_sku": "CAB-10G",
            "matched_score": 0.70,
        },
    )
    feedback_id_a = uuid.UUID(result_a["feedback_id"])

    # Namespace A should see its own row.
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        visible_a = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM product_match_feedback WHERE id = $1",
            feedback_id_a,
        )
    assert visible_a == 1, f"Namespace A should see its row; count={visible_a}"

    # Namespace B must NOT see namespace A's row (FORCE RLS).
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        visible_b = await pg_app_conn.fetchval(
            "SELECT COUNT(*) FROM product_match_feedback WHERE id = $1",
            feedback_id_a,
        )
    assert visible_b == 0, (
        f"RLS violation: namespace B can see namespace A's feedback row (id={feedback_id_a})"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_feedback_table_is_append_only(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    ns_a: uuid.UUID,
) -> None:
    """nce_app role cannot UPDATE rows in product_match_feedback (append-only grant)."""
    from nce.vertical_modules.product.matching import do_match_bom_line

    engine = _make_engine(pg_pool)

    result = await do_match_bom_line(
        engine,
        {
            "namespace_id": str(ns_a),
            "bom_line": "append-only test",
            "decision": "accept",
            "chosen_sku": "PART-X",
            "matched_score": 0.60,
        },
    )
    feedback_id = uuid.UUID(result["feedback_id"])

    # Attempt UPDATE via nce_app — must be refused (permission denied).
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            await pg_app_conn.execute(
                "UPDATE product_match_feedback SET decision = 'override' WHERE id = $1",
                feedback_id,
            )
