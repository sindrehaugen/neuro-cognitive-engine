"""
tests/unit/test_business_insights_egress.py
===========================================
Unit tests for BI-3: Third-Party AI Data Egress Boundary.

Requirements per Charter §6:
  - Shipped OFF by default.
  - Authenticated and rate-limited.
  - Role-scoped strictly to executive / board principal.
  - Explicit recorded sign-off that financials leave NCE control.
  - Fully audited to v3_cognitive_ledger (who asked what, query text, timestamp).
  - Unapproved attempts raise ThirdPartyEgressUnauthorizedError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.business_insights._guard import (
    ThirdPartyEgressUnauthorizedError,
)
from nce.vertical_modules.business_insights.ask import do_ask_business


class DummyConnection:
    def __init__(self):
        self.queries = []
        self._in_tx = False

    def transaction(self):
        class _Tx:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                self.conn._in_tx = True
                return self

            async def __aexit__(self, *args):
                self.conn._in_tx = False

        return _Tx(self)

    def is_in_transaction(self):
        return self._in_tx

    async def execute(self, query: str, *args):
        self.queries.append((query, args))
        return "INSERT 0 1"

    async def fetch(self, query: str, *args):
        return []

    async def fetchrow(self, query: str, *args):
        return None


class DummyPool:
    def __init__(self):
        self.conn = DummyConnection()

    def acquire(self):
        class _Ctx:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                return self.conn

            async def __aexit__(self, *args):
                pass

        return _Ctx(self.conn)


class DummyEngine:
    def __init__(self):
        self.pg_pool = DummyPool()
        self.pool = self.pg_pool


@pytest.mark.asyncio
async def test_third_party_egress_off_by_default():
    """BI-3: External LLM / third-party AI egress must fail when not enabled."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "board",
        "actor": "board-member@example.test",
        "question": "What was our operating gross margin last quarter?",
        "is_third_party_ai_client": True,
        # Notice: third_party_egress_enabled is NOT set, default OFF!
    }
    with pytest.raises(ThirdPartyEgressUnauthorizedError) as exc:
        await do_ask_business(engine, params)
    assert (
        "disabled by default" in str(exc.value).lower() or "not enabled" in str(exc.value).lower()
    )


@pytest.mark.asyncio
async def test_third_party_egress_requires_recorded_signoff():
    """BI-3: Even if enabled in config, third-party egress requires explicit recorded sign-off."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "board",
        "actor": "board-member@example.test",
        "question": "Show pipeline conversion by quarter",
        "is_third_party_ai_client": True,
        "third_party_egress_enabled": True,
        "recorded_signoff": None,  # Missing recorded sign-off!
    }
    with pytest.raises(ThirdPartyEgressUnauthorizedError) as exc:
        await do_ask_business(engine, params)
    assert "sign-off" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_third_party_egress_authorized_and_audited():
    """BI-3: When enabled with recorded sign-off and authorized board principal, call succeeds and audits to ledger."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "board",
        "actor": "board-chair@example.test",
        "question": "What is our ARR and cash runway?",
        "is_third_party_ai_client": True,
        "third_party_egress_enabled": True,
        "recorded_signoff": {
            "signoff_id": "signoff-2026-09-05-alpha",
            "signed_by": "board-chair@example.test",
            "terms_version": "v1.2",
            "timestamp": "2026-09-05T20:00:00Z",
        },
    }
    with patch(
        "nce.vertical_modules.business_insights.provenance.append_event",
        new=AsyncMock(),
    ) as mock_append:
        result = await do_ask_business(engine, params)
        assert result["status"] == "ok"
        assert "answer" in result
        assert "provenance" in result

        # Verify audit was executed via append_event
        mock_append.assert_awaited_once()
        call_kwargs = mock_append.await_args.kwargs
        assert call_kwargs["event_type"] == "business_insights_access_audited"
        assert call_kwargs["params"]["action"] == "ASK_BUSINESS_QUERY"
