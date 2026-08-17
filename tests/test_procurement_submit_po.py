"""Integration tests for ``do_submit_po`` (Module 1, Wave 11 — submit-po).

Money-safety invariants verified:

  1. Unconfirmed → ``pending_approval``, transport never called.
  2. Over-ceiling → ``pending_approval`` (``@governed`` value-ceiling gate).
  3. Retry with the same idempotency key → ``already_executed`` NO-OP; transport
     not called a second time.
  4. ``rebate_override=True`` triggers Agreements A2A compliance audit:
       a. A2A unavailable/raises → FAIL-CLOSED: ``pending_approval``, NOT submitted.
       b. A2A rejects        → ``pending_approval``, NOT submitted.
       c. A2A approves       → may proceed to transport.
       d. a2a_client=None    → FAIL-CLOSED.
     Every outcome is ledger-audited to ``event_log``.
  5. Kill-switch (Redis) blocks submit when ``nce:tools:disabled`` fires.
  6. NetsetPoTransport stays a ``NotImplementedError`` stub — no real auto-submit.
  7. ``event_log`` receives an audit row on every confirmed gate decision.

Fixtures used:
  ``pg_app_conn`` — asyncpg connection as ``nce_app`` (RLS enforced).
  ``make_namespace`` — factory that inserts a fresh namespace row.

Runs as ``@pytest.mark.integration`` — requires a live Postgres with schema applied.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.autonomy.governor import KillSwitchError, governed
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.procurement.po import (
    _derive_submit_idempotency_key,
    do_submit_po,
)
from nce.vertical_modules.procurement.transports import (
    NetsetPoTransport,
    PoTransport,
)

# ---------------------------------------------------------------------------
# Helpers and constants
# ---------------------------------------------------------------------------


async def _seed(conn: asyncpg.Connection, ns: object) -> None:  # type: ignore[type-arg]
    """Seed ownership registry and set namespace GUC inside a transaction."""
    async with conn.transaction():
        await set_namespace_context(conn, ns)
        await seed_node_ownership_registry(conn, ns)


class _OkTransport(PoTransport):
    """Test transport stub that returns success without any external call."""

    def __init__(self) -> None:
        self.call_count = 0

    async def place_order(
        self,
        po_number: str,
        supplier_id: str,
        line_items: list[dict[str, Any]],
        *,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.call_count += 1
        return {"confirmed": True, "external_ref": f"EXT-{po_number}"}


def _ok_transport() -> _OkTransport:
    """Return a fresh counting OK transport."""
    return _OkTransport()


def _a2a_client_approves() -> Any:
    """Mock A2A client: Agreements returns approved=True."""
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"approved": True, "note": "ok"})
    return client


def _a2a_client_rejects() -> Any:
    """Mock A2A client: Agreements returns approved=False."""
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"approved": False, "note": "kickback flagged"})
    return client


def _a2a_client_unavailable() -> Any:
    """Mock A2A client that raises (Agreements down)."""
    client = MagicMock()
    client.call_tool = AsyncMock(side_effect=RuntimeError("Agreements unreachable"))
    return client


def _redis_kill_switch_active(action_type: str) -> Any:
    """Mock Redis where the kill-switch hash entry exists for ``action_type``."""

    async def _hexists(key: str, field: str) -> bool:
        return field == action_type

    client = MagicMock()
    client.hexists = _hexists
    return client


def _high_ceiling_place_po() -> Any:
    """``_governed_place_po`` re-wrapped with a high ceiling for retry/event_log tests."""
    from nce.vertical_modules.procurement import po as po_mod

    fn = getattr(po_mod._governed_place_po, "__wrapped__", po_mod._governed_place_po)
    return governed(
        action_type="submit_po",
        value_arg="po_value",
        value_ceiling=10_000.0,
    )(fn)


# ---------------------------------------------------------------------------
# Integration test class
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoSubmitPo:
    """Integration tests for do_submit_po (Wave 11 — money-safety suite)."""

    # ------------------------------------------------------------------
    # 1. Unconfirmed → pending_approval; transport not called
    # ------------------------------------------------------------------

    async def test_unconfirmed_returns_pending_approval(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Without confirm=True the outer do_submit_po delegates to _governed which returns pending_approval."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-PEND-001")
        transport = _ok_transport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=False,  # no confirm → pending_approval
                po_number="PO-W11-PEND-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 5}],
                po_value=0.0,  # 0.0 does not exceed default ceiling of 0.0
                transport=transport,
            )

        assert result["status"] == "pending_approval"
        assert result["action_type"] == "submit_po"
        assert transport.call_count == 0, (
            f"Transport must not be called without confirm; call_count={transport.call_count}"
        )

    # ------------------------------------------------------------------
    # 2. Default ceiling=0 blocks any positive po_value (ceiling gate)
    # ------------------------------------------------------------------

    async def test_default_ceiling_zero_blocks_positive_value(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Default ceiling=0.0: po_value=0.01 > 0.0 → pending_approval from value-ceiling gate."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-CEIL-DEF-001")
        transport = _ok_transport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-CEIL-DEF-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 1}],
                po_value=0.01,  # any positive value exceeds ceiling=0.0
                transport=transport,
            )

        assert result["status"] == "pending_approval", (
            f"Default ceiling=0 must block po_value=0.01, got {result}"
        )
        assert transport.call_count == 0

    # ------------------------------------------------------------------
    # 2b. Explicit ceiling check: value above configured ceiling → pending
    # ------------------------------------------------------------------

    async def test_over_explicit_ceiling_forced_pending_approval(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """po_value > configured ceiling forces pending_approval regardless of confirm."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        # Use _governed_place_po re-wrapped with ceiling=500 directly.
        from nce.vertical_modules.procurement import po as po_mod

        fn = getattr(po_mod._governed_place_po, "__wrapped__", po_mod._governed_place_po)
        patched = governed(
            action_type="submit_po",
            value_arg="po_value",
            value_ceiling=500.0,
        )(fn)

        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-CEIL-EXPL-001")
        transport = _ok_transport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await patched(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-CEIL-EXPL-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 5}],
                po_value=999.0,  # above 500.0 ceiling
                transport=transport,
            )

        assert result["status"] == "pending_approval", (
            f"Expected pending_approval for over-ceiling PO, got {result}"
        )
        reason = result.get("reason", "")
        assert "ceil" in reason.lower() or "exceed" in reason.lower(), (
            f"Reason should mention ceiling, got {reason!r}"
        )
        assert transport.call_count == 0

    # ------------------------------------------------------------------
    # 3. Retry same idempotency key → NO-OP (already_executed)
    # ------------------------------------------------------------------

    async def test_retry_same_key_is_noop(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Retry with same idempotency key → already_executed; transport called exactly once."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        patched = _high_ceiling_place_po()
        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-RETRY-001")
        transport = _ok_transport()

        # First call — should execute.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            r1 = await patched(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-RETRY-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 2}],
                po_value=100.0,
                transport=transport,
            )
        assert r1["status"] == "executed", f"First call should execute, got {r1}"
        assert transport.call_count == 1, (
            f"Transport called {transport.call_count} times on first execute (expected 1)"
        )

        # Second call — same key → NO-OP.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            r2 = await patched(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-RETRY-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 2}],
                po_value=100.0,
                transport=transport,
            )
        assert r2["status"] == "already_executed", (
            f"Second call with same key should be NO-OP, got {r2}"
        )
        assert transport.call_count == 1, (
            f"Transport must not be called again on retry; call_count={transport.call_count}"
        )

    # ------------------------------------------------------------------
    # 4a. rebate_override + Agreements unavailable → FAIL-CLOSED
    # ------------------------------------------------------------------

    async def test_rebate_override_agreements_unavailable_fail_closed(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """rebate_override=True with Agreements unavailable → FAIL-CLOSED (pending_approval)."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-REBATE-UNAVAIL-001")
        transport = _ok_transport()

        # Count event_log before.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            before = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1", ns
            )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-REBATE-UNAVAIL-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 1}],
                po_value=0.0,
                rebate_override=True,
                rebate_amount=15.0,
                transport=transport,
                a2a_client=_a2a_client_unavailable(),
            )

        # Must be pending_approval, never submitted.
        assert result["status"] == "pending_approval", (
            f"Agreements unavailable must fail-closed to pending_approval, got {result}"
        )
        assert transport.call_count == 0, (
            f"Transport must NOT be called when Agreements is unavailable; "
            f"call_count={transport.call_count}"
        )

        # Decision must be audited in event_log.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            after = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1", ns
            )
        assert after > before, (
            f"event_log must grow after rebate fail-closed; before={before} after={after}"
        )

    # ------------------------------------------------------------------
    # 4b. rebate_override + Agreements rejects → pending_approval
    # ------------------------------------------------------------------

    async def test_rebate_override_agreements_rejects_blocks_submit(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """rebate_override=True with Agreements approved=False → pending_approval."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-REBATE-REJECT-001")
        transport = _ok_transport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-REBATE-REJECT-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 1}],
                po_value=0.0,
                rebate_override=True,
                rebate_amount=15.0,
                transport=transport,
                a2a_client=_a2a_client_rejects(),
            )

        assert result["status"] == "pending_approval", (
            f"Agreements rejection must block auto-submit, got {result}"
        )
        assert transport.call_count == 0

    # ------------------------------------------------------------------
    # 4c. rebate_override + Agreements approves → proceeds to transport
    # ------------------------------------------------------------------

    async def test_rebate_override_agreements_approves_may_proceed(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """rebate_override=True with Agreements approved=True → transport is called."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        # Use _governed_place_po with high ceiling so value gate doesn't trip.
        # do_submit_po's outer gate handles rebate; inner governed handles transport.
        # We directly use do_submit_po; rebate passes → _governed_place_po is called.
        # But default ceiling=0 would block it; use inner function directly for
        # the transport side, calling outer do_submit_po with po_value=0.0.
        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-REBATE-APPR-001")
        transport = _ok_transport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-REBATE-APPR-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 1}],
                po_value=0.0,  # 0.0 does not exceed default ceiling 0.0
                rebate_override=True,
                rebate_amount=10.0,
                transport=transport,
                a2a_client=_a2a_client_approves(),
            )

        # With ceiling=0 and po_value=0.0, the value gate should pass (0.0 is not > 0.0).
        # The rebate gate passes → transport is called.
        assert result["status"] == "executed", (
            f"Agreements approval should allow execution, got {result}"
        )
        assert transport.call_count == 1, (
            f"Transport should be called exactly once after approval; "
            f"call_count={transport.call_count}"
        )

    # ------------------------------------------------------------------
    # 4d. rebate_override + no a2a_client → FAIL-CLOSED
    # ------------------------------------------------------------------

    async def test_rebate_override_no_a2a_client_fail_closed(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """rebate_override=True with a2a_client=None → FAIL-CLOSED (pending_approval)."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-REBATE-NOCLNT-001")
        transport = _ok_transport()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await do_submit_po(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-REBATE-NOCLNT-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 1}],
                po_value=0.0,
                rebate_override=True,
                rebate_amount=10.0,
                transport=transport,
                a2a_client=None,  # no client → unavailable
            )

        assert result["status"] == "pending_approval", (
            f"Missing A2A client must fail-closed, got {result}"
        )
        assert transport.call_count == 0

    # ------------------------------------------------------------------
    # 5. Kill-switch blocks submit
    # ------------------------------------------------------------------

    async def test_kill_switch_blocks_submit(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Kill-switch active for 'submit_po' raises KillSwitchError (submit blocked)."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        patched = _high_ceiling_place_po()
        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-KILL-001")

        with pytest.raises(KillSwitchError):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await patched(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=True,
                    po_number="PO-W11-KILL-001",
                    supplier_id="SUP-001",
                    line_items=[{"artnr": "A001", "quantity": 1}],
                    po_value=100.0,
                    transport=_ok_transport(),
                    redis_client=_redis_kill_switch_active("submit_po"),
                )

    # ------------------------------------------------------------------
    # 6. NetsetPoTransport raises NotImplementedError (no real auto-submit)
    # ------------------------------------------------------------------

    async def test_netset_transport_stub_prevents_real_submit(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """Default NetsetPoTransport raises NotImplementedError → no real auto-submit."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        patched = _high_ceiling_place_po()
        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-STUB-001")

        with pytest.raises(NotImplementedError) as exc_info:
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                await patched(
                    pg_app_conn,
                    ns,
                    idempotency_key=ikey,
                    confirm=True,
                    po_number="PO-W11-STUB-001",
                    supplier_id="SUP-001",
                    line_items=[{"artnr": "A001", "quantity": 1}],
                    po_value=100.0,
                    transport=None,  # defaults to NetsetPoTransport (🔴 stub)
                )

        assert "Netset Order API not yet available" in str(exc_info.value) or "Netset" in str(
            exc_info.value
        ), f"Expected NetsetPoTransport stub message, got: {exc_info.value}"

    # ------------------------------------------------------------------
    # 7. Confirmed execution writes event_log audit row
    # ------------------------------------------------------------------

    async def test_confirmed_writes_event_log_audit(
        self,
        pg_app_conn: asyncpg.Connection,  # type: ignore[type-arg]
        make_namespace: object,
    ) -> None:
        """confirm=True with all gates passing → @governed writes event_log audit row."""
        ns = await make_namespace()  # type: ignore[operator]
        await _seed(pg_app_conn, ns)

        patched = _high_ceiling_place_po()
        ikey = _derive_submit_idempotency_key(str(ns), "PO-W11-AUDIT-001")

        # Count event_log rows before.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            before = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1", ns
            )

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            result = await patched(
                pg_app_conn,
                ns,
                idempotency_key=ikey,
                confirm=True,
                po_number="PO-W11-AUDIT-001",
                supplier_id="SUP-001",
                line_items=[{"artnr": "A001", "quantity": 1}],
                po_value=100.0,
                transport=_ok_transport(),
            )

        assert result["status"] == "executed", f"Expected executed, got {result}"

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            after = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM event_log WHERE namespace_id = $1", ns
            )
        assert after > before, f"Expected event_log rows to grow; before={before} after={after}"


# ---------------------------------------------------------------------------
# Pure unit tests — idempotency key helpers (no DB required)
# ---------------------------------------------------------------------------


def test_derive_submit_idempotency_key_is_stable() -> None:
    """Same inputs produce the same submit idempotency key."""
    ns = str(uuid.uuid4())
    k1 = _derive_submit_idempotency_key(ns, "PO-001")
    k2 = _derive_submit_idempotency_key(ns, "PO-001")
    assert k1 == k2


def test_derive_submit_idempotency_key_differs_by_po_number() -> None:
    """Different PO numbers produce different idempotency keys."""
    ns = str(uuid.uuid4())
    k1 = _derive_submit_idempotency_key(ns, "PO-001")
    k2 = _derive_submit_idempotency_key(ns, "PO-002")
    assert k1 != k2


def test_derive_submit_idempotency_key_has_submit_prefix() -> None:
    """Submit key has 'submit:' prefix to distinguish from generate-PO keys."""
    ns = str(uuid.uuid4())
    k = _derive_submit_idempotency_key(ns, "PO-001")
    assert k.startswith("submit:")


def test_netset_transport_raises_not_implemented_unit() -> None:
    """NetsetPoTransport.place_order raises NotImplementedError (unit — no DB)."""
    transport = NetsetPoTransport()
    with pytest.raises(NotImplementedError) as exc_info:
        asyncio.run(
            transport.place_order(
                "PO-UNIT-001",
                "SUP-001",
                [{"artnr": "A001", "quantity": 1}],
                namespace_id=str(uuid.uuid4()),
                idempotency_key="unit-key",
            )
        )
    assert "Netset Order API not yet available" in str(exc_info.value)
