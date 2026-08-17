"""
tests/unit/test_system_design_sharepoint.py
============================================
Unit tests for the System Design SharePoint adapter (Wave 10, Phase 1b).

All HTTP is mocked via ``pytest-httpx`` / ``respx`` — no real network calls.
No DB required (pure unit test).

Tested behaviours:
  1. ``store_sow`` POSTs content and returns the opaque SharePoint item id.
  2. ``fetch_sow`` round-trips: the same doc comes back from the ref.
  3. Both functions return ``None`` (clean no-op) when the credential is unset.
  4. Credentials never appear in log output.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal SoWDoc fixture
# ---------------------------------------------------------------------------

_SOW_DOC: dict[str, Any] = {
    "documentRef": "DESIGN-001-v1",
    "generatedAt": "2026-01-01T00:00:00+00:00",
    "versionNumber": 1,
    "project": {
        "id": "DESIGN-001",
        "name": "Test Design",
        "customer": "ACME",
        "contractValue": 100_000.0,
        "startDate": "2026-01-01",
        "endDate": "2026-06-01",
        "pm": "Test PM",
        "tierLabel": "Prosjekt (L)",
    },
    "summary": "Test summary.",
    "deliverables": [],
    "timeline": [],
    "laborByCategory": [],
    "laborTotalHours": 0.0,
    "laborTotalSell": 0.0,
    "managedServices": [],
    "invoicing": None,
    "acceptance": [],
    "capturedIntelligence": None,
    "terms": [],
}

_DESIGN_ID = "DESIGN-001"
_FAKE_ITEM_ID = "sharepoint-item-abc123"
_FAKE_SITE_ID = "site-xyz"
_FAKE_TOKEN = "FAKE_TOKEN_VALUE_NEVER_IN_LOGS"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(status_code: int, body: Any) -> MagicMock:
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=body)
    resp.text = json.dumps(body)
    return resp


# ---------------------------------------------------------------------------
# Tests: store_sow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_sow_posts_and_returns_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """store_sow should POST the document and return the item id from the response."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", _FAKE_SITE_ID)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN", _FAKE_TOKEN)

    store_response = _make_mock_response(200, {"id": _FAKE_ITEM_ID, "name": "DESIGN-001-v1.json"})

    with patch(
        "nce.vertical_modules.system_design.sharepoint.request_with_retry",
        new=AsyncMock(return_value=store_response),
    ) as mock_req:
        from nce.vertical_modules.system_design import sharepoint

        ref = await sharepoint.store_sow(_DESIGN_ID, _SOW_DOC)

    assert ref == _FAKE_ITEM_ID
    mock_req.assert_awaited_once()
    call_kwargs = mock_req.call_args
    assert call_kwargs.args[1] == "PUT"


@pytest.mark.asyncio
async def test_store_sow_noop_when_site_id_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """store_sow returns None and makes no HTTP call when credentials are unset."""
    monkeypatch.delenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", raising=False)

    with patch(
        "nce.vertical_modules.system_design.sharepoint.request_with_retry",
        new=AsyncMock(),
    ) as mock_req:
        from nce.vertical_modules.system_design import sharepoint

        ref = await sharepoint.store_sow(_DESIGN_ID, _SOW_DOC)

    assert ref is None
    mock_req.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: fetch_sow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_sow_returns_doc_for_valid_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_sow should return the deserialized SoWDoc for a known ref."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", _FAKE_SITE_ID)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN", _FAKE_TOKEN)

    fetch_response = _make_mock_response(200, _SOW_DOC)

    with patch(
        "nce.vertical_modules.system_design.sharepoint.request_with_retry",
        new=AsyncMock(return_value=fetch_response),
    ) as mock_req:
        from nce.vertical_modules.system_design import sharepoint

        doc = await sharepoint.fetch_sow(_FAKE_ITEM_ID)

    assert doc is not None
    assert doc["documentRef"] == _SOW_DOC["documentRef"]
    mock_req.assert_awaited_once()
    call_kwargs = mock_req.call_args
    assert call_kwargs.args[1] == "GET"


@pytest.mark.asyncio
async def test_fetch_sow_noop_when_site_id_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_sow returns None and makes no HTTP call when credentials are unset."""
    monkeypatch.delenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", raising=False)

    with patch(
        "nce.vertical_modules.system_design.sharepoint.request_with_retry",
        new=AsyncMock(),
    ) as mock_req:
        from nce.vertical_modules.system_design import sharepoint

        doc = await sharepoint.fetch_sow(_FAKE_ITEM_ID)

    assert doc is None
    mock_req.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_sow_noop_for_empty_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_sow returns None without any HTTP call when ref is empty."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", _FAKE_SITE_ID)

    with patch(
        "nce.vertical_modules.system_design.sharepoint.request_with_retry",
        new=AsyncMock(),
    ) as mock_req:
        from nce.vertical_modules.system_design import sharepoint

        doc = await sharepoint.fetch_sow("")

    assert doc is None
    mock_req.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: store → fetch round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_fetch_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """store_sow followed by fetch_sow returns the original document."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", _FAKE_SITE_ID)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN", _FAKE_TOKEN)

    store_response = _make_mock_response(200, {"id": _FAKE_ITEM_ID})
    fetch_response = _make_mock_response(200, _SOW_DOC)

    call_count = 0

    async def _mock_request_with_retry(client: Any, method: str, url: str, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if method == "PUT":
            return store_response
        return fetch_response

    with patch(
        "nce.vertical_modules.system_design.sharepoint.request_with_retry",
        new=_mock_request_with_retry,
    ):
        from nce.vertical_modules.system_design import sharepoint

        ref = await sharepoint.store_sow(_DESIGN_ID, _SOW_DOC)
        assert ref == _FAKE_ITEM_ID

        doc = await sharepoint.fetch_sow(ref)  # type: ignore[arg-type]
        assert doc is not None
        assert doc["documentRef"] == _SOW_DOC["documentRef"]
        assert doc["versionNumber"] == _SOW_DOC["versionNumber"]

    assert call_count == 2


# ---------------------------------------------------------------------------
# Tests: credentials never appear in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_credential_in_log_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The access token value must never appear in any log record."""
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID", _FAKE_SITE_ID)
    monkeypatch.setenv("NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN", _FAKE_TOKEN)

    store_response = _make_mock_response(200, {"id": _FAKE_ITEM_ID})
    fetch_response = _make_mock_response(200, _SOW_DOC)

    async def _dispatch(client: Any, method: str, url: str, **kwargs: Any) -> Any:
        if method == "PUT":
            return store_response
        return fetch_response

    with caplog.at_level(logging.DEBUG, logger="nce.vertical_modules.system_design.sharepoint"):
        with patch(
            "nce.vertical_modules.system_design.sharepoint.request_with_retry",
            new=_dispatch,
        ):
            from nce.vertical_modules.system_design import sharepoint

            await sharepoint.store_sow(_DESIGN_ID, _SOW_DOC)
            await sharepoint.fetch_sow(_FAKE_ITEM_ID)

    full_log = "\n".join(caplog.messages)
    assert _FAKE_TOKEN not in full_log, f"Credential token leaked into log output: {full_log!r}"
