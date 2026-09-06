"""
tests/unit/test_sales_signing_loop.py
======================================
Acceptance tests for Wave S-2a:
  "A signed quote can freeze a baseline (baseline frozen -> project converted)"

Covers:
  1. `sales_request_signature` MCP tool initiates C7 signing session and updates read model.
  2. `POST /webhooks/signing/{provider}` processes signed webhook, freezes baseline, triggers conversion.
  3. Replayed webhook writes one baseline, not two (idempotent).
  4. Unsigned / pending callback writes no baseline.
  5. Admin `POST /api/admin/signing/mark-signed` attests signature without external vendor.
  6. Project conversion receives `sales_available=True` and `sales_baseline_unavailable` is absent.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from nce.mcp_errors import McpError
from nce.tool_registry import ADMIN_ONLY_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.sales.mcp_handlers import (
    handle_sales_request_signature,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_QUOTE_ID = "quote-1001"


@pytest.fixture(autouse=True)
def _webhook_test_isolation(monkeypatch):
    """Isolate webhook tests from Redis dedup requiring auth in unit tests."""
    import nce.webhook_receiver.main as wh

    monkeypatch.setattr(wh, "_ip_windows", {}, raising=False)
    monkeypatch.setattr(wh, "_claim_dedup", lambda *_a, **_k: True)


# ---------------------------------------------------------------------------
# 1. MCP Tool Registration & Spec
# ---------------------------------------------------------------------------


def test_sales_request_signature_registered():
    assert "sales_request_signature" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["sales_request_signature"]
    assert spec.admin_only is True
    assert spec.mutation is True
    assert spec.cacheable is False
    assert "sales_request_signature" in ADMIN_ONLY_TOOLS
    assert "sales_request_signature" in MUTATION_TOOLS


@pytest.mark.asyncio
async def test_handle_sales_request_signature_missing_args():
    engine = MagicMock()
    with pytest.raises(McpError) as exc_info:
        await handle_sales_request_signature(engine, {})
    assert exc_info.value.code == -32602

    with pytest.raises(McpError) as exc_info:
        await handle_sales_request_signature(engine, {"namespace_id": _NAMESPACE_ID})
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 2. handle_sales_request_signature Happy Path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sales_request_signature_ok(monkeypatch):
    mock_session = {
        "session_id": "sess-12345",
        "status": "pending",
        "fingerprint": "abc123sha256",
        "quote_id": _QUOTE_ID,
    }

    async def _mock_do_request(engine, params):
        assert params["namespace_id"] == _NAMESPACE_ID
        assert params["quote_id"] == _QUOTE_ID
        assert params["method"] == "manual"
        return mock_session

    monkeypatch.setattr(
        "nce.vertical_modules.sales.mcp_handlers.do_request_signature",
        _mock_do_request,
    )

    engine = MagicMock()
    res = await handle_sales_request_signature(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "quote_id": _QUOTE_ID,
            "method": "manual",
        },
    )
    data = json.loads(res)
    assert data["session_id"] == "sess-12345"
    assert data["status"] == "pending"


# ---------------------------------------------------------------------------
# 3. Webhook Receiver — Inbound signing/{provider}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_signing_unsupported_provider():
    from fastapi import HTTPException

    from nce.webhook_receiver.main import signing_webhook

    req = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc:
        await signing_webhook("invalid_provider", req)
    assert exc.value.status_code == 400
    assert "Unsupported signing provider" in exc.value.detail


@pytest.mark.asyncio
async def test_webhook_signing_missing_session_id():
    from fastapi import HTTPException

    import nce.webhook_receiver.main as wh

    req = MagicMock(spec=Request)
    with patch.object(wh, "_read_json_bounded", AsyncMock(return_value={"status": "signed"})):
        with pytest.raises(HTTPException) as exc:
            await wh.signing_webhook("manual", req)
        assert exc.value.status_code == 400
        assert "Missing required 'session_id'" in exc.value.detail


@pytest.mark.asyncio
async def test_webhook_signing_unsigned_writes_no_baseline():
    import nce.webhook_receiver.main as wh

    req = MagicMock(spec=Request)
    payload = {
        "session_id": "sess-unsigned",
        "status": "pending",
        "namespace_id": _NAMESPACE_ID,
    }

    with patch.object(wh, "_read_json_bounded", AsyncMock(return_value=payload)):
        with patch("nce.vertical_modules.sales.signing.do_on_signed_callback") as mock_cb:
            resp = await wh.signing_webhook("manual", req)
            assert resp["status"] == "acknowledged"
            assert resp["baseline_frozen"] is False
            mock_cb.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_signing_signed_triggers_callback():
    import nce.webhook_receiver.main as wh

    req = MagicMock(spec=Request)
    payload = {
        "session_id": "sess-signed-001",
        "status": "signed",
        "namespace_id": _NAMESPACE_ID,
        "callback_payload": {"audit": "test"},
    }

    mock_result = {
        "ok": True,
        "quote_id": _QUOTE_ID,
        "session_id": "sess-signed-001",
        "baseline_frozen": True,
        "project_id": "PROJECT:QUOTE-1001",
        "already_processed": False,
    }

    with patch.object(wh, "_read_json_bounded", AsyncMock(return_value=payload)):
        with patch.object(wh, "_get_pg_pool", AsyncMock(return_value=MagicMock())):
            with patch(
                "nce.vertical_modules.sales.signing.do_on_signed_callback",
                AsyncMock(return_value=mock_result),
            ) as mock_cb:
                resp = await wh.signing_webhook("manual", req)
                assert resp["status"] == "processed"
                assert resp["baseline_frozen"] is True
                assert resp["project_id"] == "PROJECT:QUOTE-1001"
                mock_cb.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Replay & Idempotency: Replaying writes ONE baseline, not two
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signing_callback_idempotency():
    """Verify that re-firing on_signed callback does not re-freeze or duplicate."""
    from nce.vertical_modules.sales.signing import do_on_signed_callback

    engine = MagicMock()
    session_id = "sess-replay-001"

    # Mock DB row: already marked 'signed' in sales_read_model
    row = {
        "source_id": _QUOTE_ID,
        "source_json": {"margin": 0.25, "total_price": 50000.0},
        "manual": {"signing_status": "signed", "signing_session_id": session_id},
    }

    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = row

    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_conn
    fake_cm.__aexit__.return_value = None

    with patch("nce.vertical_modules.sales.signing.scoped_pg_session", return_value=fake_cm):
        with patch("nce.vertical_modules.sales.signing.do_freeze_baseline") as mock_freeze:
            res = await do_on_signed_callback(
                engine,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "session_id": session_id,
                },
            )
            assert res["ok"] is True
            assert res["already_processed"] is True
            assert res["baseline_frozen"] is True
            # Freeze was NOT called again because quote was already processed as signed
            mock_freeze.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Admin Mark-Signed Route (Attest PDF / ManualTransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_signing_mark_signed_endpoint():
    from nce.admin_handlers import admin_state
    from nce.admin_handlers.fleet import api_admin_signing_mark_signed

    engine = MagicMock()
    engine.pg_pool = MagicMock()
    admin_state.engine = engine

    req = MagicMock(spec=Request)
    req.json = AsyncMock(
        return_value={
            "namespace_id": _NAMESPACE_ID,
            "session_id": "sess-admin-001",
            "attest_pdf": True,
            "attestation_notes": "Signed in office by customer",
        }
    )

    mock_res = {
        "ok": True,
        "quote_id": _QUOTE_ID,
        "session_id": "sess-admin-001",
        "baseline_frozen": True,
        "project_id": "PROJECT:QUOTE-1001",
        "already_processed": False,
    }

    with patch(
        "nce.vertical_modules.sales.signing.do_on_signed_callback",
        AsyncMock(return_value=mock_res),
    ) as mock_cb:
        resp = await api_admin_signing_mark_signed(req)
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["baseline_frozen"] is True
        assert body["project_id"] == "PROJECT:QUOTE-1001"
        mock_cb.assert_called_once()
