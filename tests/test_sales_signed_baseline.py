"""Integration tests for Sales signed baseline freeze (Wave 8 — signed-baseline-freeze)."""

from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.baseline import do_freeze_baseline


def _make_engine_stub(pg_pool: asyncpg.Pool) -> NCEEngine:
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    stub.redis_client = None  # type: ignore[attr-defined]
    return stub  # type: ignore[return-value]


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesSignedBaseline:
    """Integration tests for Sales signed baselines."""

    async def test_freeze_baseline_lifecycle(
        self,
        pg_app_conn: asyncpg.Connection,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify freezing a baseline successfully and idempotently, and reading it."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # 1. Freeze a quote baseline
        quote_id = "quote-12345"
        params = {
            "namespace_id": ns,
            "quote_id": quote_id,
            "signed_margin_pct": 0.35,
            "signed_total_nok": 150000.0,
            "signed_at": "2026-06-24T12:00:00+00:00",
        }

        res = await do_freeze_baseline(engine, params)
        assert res["ok"] is True
        assert res["status"] == "frozen"
        assert res["quote_id"] == quote_id
        assert res["signed_margin_pct"] == 0.35
        assert res["signed_total_nok"] == 150000.0
        assert res["signed_at"] == "2026-06-24T12:00:00+00:00"
        assert res["id"] is not None

        # 2. Re-freezing the same baseline is a no-op/rejected and returns already_frozen status
        res_dup = await do_freeze_baseline(engine, params)
        assert res_dup["ok"] is True
        assert res_dup["status"] == "already_frozen"
        assert res_dup["id"] == res["id"]

        # 3. Verify it is readable by a downstream reader query
        async with pg_pool.acquire() as conn:
            await set_namespace_context(conn, ns)
            row = await conn.fetchrow(
                "SELECT id, quote_id, signed_margin_pct, signed_total_nok, signed_at FROM sales_signed_baselines WHERE namespace_id = $1 AND quote_id = $2",
                ns,
                quote_id,
            )
            assert row is not None
            assert row["quote_id"] == quote_id
            assert float(row["signed_margin_pct"]) == 0.35
            assert float(row["signed_total_nok"]) == 150000.0

    async def test_freeze_baseline_invalid_inputs(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify invalid parameters raise ValueError."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # Missing namespace_id
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_freeze_baseline(
                engine, {"quote_id": "q", "signed_margin_pct": 0.5, "signed_total_nok": 100}
            )

        # Missing quote_id
        with pytest.raises(ValueError, match="quote_id is required"):
            await do_freeze_baseline(
                engine, {"namespace_id": ns, "signed_margin_pct": 0.5, "signed_total_nok": 100}
            )

        # Invalid quote_id type
        with pytest.raises(ValueError, match="quote_id must be a non-empty string"):
            await do_freeze_baseline(
                engine,
                {
                    "namespace_id": ns,
                    "quote_id": 123,
                    "signed_margin_pct": 0.5,
                    "signed_total_nok": 100,
                },
            )

        # Out-of-bounds signed_margin_pct
        with pytest.raises(ValueError, match="signed_margin_pct must be between 0.0 and 1.0"):
            await do_freeze_baseline(
                engine,
                {
                    "namespace_id": ns,
                    "quote_id": "q",
                    "signed_margin_pct": 1.5,
                    "signed_total_nok": 100,
                },
            )

        # Negative signed_margin_pct
        with pytest.raises(ValueError, match="signed_margin_pct must be between 0.0 and 1.0"):
            await do_freeze_baseline(
                engine,
                {
                    "namespace_id": ns,
                    "quote_id": "q",
                    "signed_margin_pct": -0.1,
                    "signed_total_nok": 100,
                },
            )

    async def test_freeze_baseline_immutability(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace: Any,
    ) -> None:
        """Verify that updates and deletes on the table are blocked for nce_app."""
        ns = await make_namespace()

        # Seed a baseline using pg_app_conn (since it has INSERT privilege)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await pg_app_conn.execute(
                """
                INSERT INTO sales_signed_baselines (namespace_id, quote_id, signed_margin_pct, signed_total_nok)
                VALUES ($1, $2, $3, $4)
                """,
                ns,
                "quote-immutable",
                0.4,
                200000.0,
            )

        # Verify UPDATE fails with InsufficientPrivilegeError (PermissionDeniedError)
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await pg_app_conn.execute(
                    """
                    UPDATE sales_signed_baselines
                    SET signed_margin_pct = 0.5
                    WHERE namespace_id = $1 AND quote_id = $2
                    """,
                    ns,
                    "quote-immutable",
                )

        # Verify DELETE fails with InsufficientPrivilegeError
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await pg_app_conn.execute(
                    """
                    DELETE FROM sales_signed_baselines
                    WHERE namespace_id = $1 AND quote_id = $2
                    """,
                    ns,
                    "quote-immutable",
                )

    async def test_freeze_baseline_rls(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace: Any,
    ) -> None:
        """Verify that RLS isolates baselines across namespaces."""
        ns_a = await make_namespace()
        ns_b = await make_namespace()

        # Insert a baseline in ns_a
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            await pg_app_conn.execute(
                """
                INSERT INTO sales_signed_baselines (namespace_id, quote_id, signed_margin_pct, signed_total_nok)
                VALUES ($1, $2, $3, $4)
                """,
                ns_a,
                "quote-shared",
                0.3,
                50000.0,
            )

        # Query from ns_a -> should see it
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            row_a = await pg_app_conn.fetchrow(
                "SELECT * FROM sales_signed_baselines WHERE quote_id = $1", "quote-shared"
            )
            assert row_a is not None

        # Query from ns_b -> should not see it
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            row_b = await pg_app_conn.fetchrow(
                "SELECT * FROM sales_signed_baselines WHERE quote_id = $1", "quote-shared"
            )
            assert row_b is None
