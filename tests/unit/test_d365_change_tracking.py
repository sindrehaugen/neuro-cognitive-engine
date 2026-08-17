"""Unit tests for the D365 Dataverse client change-tracking capability.

Covers the delta query (`Prefer: odata.track-changes`), nextLink paging, the
trailing `@odata.deltaLink`, and `@removed` (deletion) parsing — see
`nce/vertical_modules/dynamics365/client.py::track_changes`.
"""

from __future__ import annotations

import httpx
import pytest

from nce.vertical_modules.dynamics365.client import DataverseClient

_BASE = "https://org.crm.dynamics.com"


def _client(handler) -> DataverseClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DataverseClient(_BASE, "tok", client=http)


@pytest.mark.asyncio
async def test_track_changes_initial_requests_tracking_and_returns_delta_link():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "odata.track-changes" in request.headers.get("prefer", "")
        return httpx.Response(
            200,
            json={
                "value": [
                    {"accountid": "a1", "name": "Acme"},
                    {"accountid": "a2", "name": "Globex"},
                ],
                "@odata.deltaLink": f"{_BASE}/api/data/v9.2/accounts?$deltatoken=XYZ",
            },
        )

    changed, removed, delta = await _client(handler).track_changes(
        "accounts", select=["accountid", "name"]
    )
    assert [r["accountid"] for r in changed] == ["a1", "a2"]
    assert removed == []
    assert delta is not None and "deltatoken=XYZ" in delta


@pytest.mark.asyncio
async def test_track_changes_delta_parses_removed_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "$deltatoken=XYZ" in str(request.url)
        return httpx.Response(
            200,
            json={
                "value": [
                    {"accountid": "a1", "name": "Acme Renamed"},
                    {"@removed": {"reason": "deleted"}, "id": "a2"},
                ],
                "@odata.deltaLink": f"{_BASE}/api/data/v9.2/accounts?$deltatoken=NEXT",
            },
        )

    changed, removed, delta = await _client(handler).track_changes(
        "accounts", delta_link=f"{_BASE}/api/data/v9.2/accounts?$deltatoken=XYZ"
    )
    assert [r["accountid"] for r in changed] == ["a1"]
    assert removed == ["a2"]
    assert delta is not None and "deltatoken=NEXT" in delta


@pytest.mark.asyncio
async def test_track_changes_follows_nextlink_pages():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"accountid": "a1"}],
                    "@odata.nextLink": f"{_BASE}/api/data/v9.2/accounts?$skiptoken=p2",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [{"accountid": "a2"}],
                "@odata.deltaLink": f"{_BASE}/api/data/v9.2/accounts?$deltatoken=END",
            },
        )

    changed, removed, delta = await _client(handler).track_changes("accounts")
    assert [r["accountid"] for r in changed] == ["a1", "a2"]
    assert delta is not None and "deltatoken=END" in delta
    assert calls["n"] == 2
