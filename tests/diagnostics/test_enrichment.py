"""Batch 72 — NetBox context enrichment.

Unit tests for ``nce.vertical_modules.diagnostics.enrichment``.

All tests are pure-unit: no Docker, no network, no database.  The NetBox
GraphQL client is replaced by a tiny in-process fake that records the calls it
receives and returns a canned payload — exercising the same
``execute_query(query, variables) -> {"data": {...}}`` contract as the real
``NetBoxGraphQLClient`` without opening any HTTP session.
"""

from __future__ import annotations

from typing import Any

import pytest

from nce.vertical_modules.diagnostics.enrichment import (
    DEVICE_CONTEXT_QUERY,
    resolve_device_context,
)

# ── Test double ─────────────────────────────────────────────────────────────────


class FakeNetBoxClient:
    """In-process stand-in for ``NetBoxGraphQLClient``.

    Records every ``execute_query`` invocation so tests can assert that the
    passed-in client is reused (one call, correct variables) and never opens a
    new HTTP session.
    """

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def execute_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((query, variables))
        return self._response


def _device_response(device: dict[str, Any] | None) -> dict[str, Any]:
    """Wrap a single device (or nothing) in NetBox's GraphQL envelope."""
    return {"data": {"device_list": [device] if device is not None else []}}


def _full_device() -> dict[str, Any]:
    """A fully-populated device record."""
    return {
        "id": "1",
        "name": "core-sw-1",
        "serial": "SN-ABC-123",
        "site": {"slug": "oslo-dc", "name": "Oslo DC"},
        "location": {"slug": "room-301", "name": "Room 301"},
        "tenant": {"slug": "acme", "name": "Acme Corp"},
    }


# ── Found cases ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_by_slug_returns_full_context() -> None:
    """A slug hit returns every context field and resolved=True."""
    client = FakeNetBoxClient(_device_response(_full_device()))

    result = await resolve_device_context(client, slug="core-sw-1", serial=None)

    assert result == {
        "device_slug": "core-sw-1",
        "site": "Oslo DC",
        "location": "Room 301",
        "room": "Room 301",
        "tenant": "Acme Corp",
        "resolved": True,
    }


@pytest.mark.asyncio
async def test_resolve_by_serial_returns_full_context() -> None:
    """A serial hit resolves the canonical slug from the device name."""
    client = FakeNetBoxClient(_device_response(_full_device()))

    result = await resolve_device_context(client, slug=None, serial="SN-ABC-123")

    assert result["resolved"] is True
    assert result["device_slug"] == "core-sw-1"
    assert result["site"] == "Oslo DC"
    assert result["tenant"] == "Acme Corp"


@pytest.mark.asyncio
async def test_resolve_reuses_passed_in_client_with_filter_variables() -> None:
    """The supplied client is reused exactly once with the right filters.

    Guards the "do not open a new HTTP session / reuse execute_query" contract.
    """
    client = FakeNetBoxClient(_device_response(_full_device()))

    await resolve_device_context(client, slug="core-sw-1", serial="SN-ABC-123")

    assert len(client.calls) == 1
    query, variables = client.calls[0]
    assert query == DEVICE_CONTEXT_QUERY
    # NetBox list filters take arrays; only supplied identifiers are sent.
    assert variables == {"slug": ["core-sw-1"], "serial": ["SN-ABC-123"]}


@pytest.mark.asyncio
async def test_resolve_only_sends_slug_when_serial_absent() -> None:
    """A serial-less lookup must not send an empty serial filter."""
    client = FakeNetBoxClient(_device_response(_full_device()))

    await resolve_device_context(client, slug="core-sw-1", serial=None)

    _query, variables = client.calls[0]
    assert variables == {"slug": ["core-sw-1"]}


@pytest.mark.asyncio
async def test_resolve_handles_null_location_and_tenant() -> None:
    """Unset NetBox relations (null location/tenant) degrade to None, not error."""
    device = _full_device()
    device["location"] = None
    device["tenant"] = None
    client = FakeNetBoxClient(_device_response(device))

    result = await resolve_device_context(client, slug="core-sw-1", serial=None)

    assert result["resolved"] is True
    assert result["site"] == "Oslo DC"
    assert result["location"] is None
    assert result["room"] is None
    assert result["tenant"] is None


# ── Not-found cases (non-fatal) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_device_is_non_fatal_echoes_slug() -> None:
    """An empty device_list echoes the slug and sets resolved=False."""
    client = FakeNetBoxClient(_device_response(None))

    result = await resolve_device_context(client, slug="ghost-device", serial=None)

    assert result == {
        "device_slug": "ghost-device",
        "site": None,
        "location": None,
        "room": None,
        "tenant": None,
        "resolved": False,
    }


@pytest.mark.asyncio
async def test_missing_device_by_serial_echoes_none_slug() -> None:
    """A serial-only miss still returns the not-resolved shape (slug echoes None)."""
    client = FakeNetBoxClient(_device_response(None))

    result = await resolve_device_context(client, slug=None, serial="SN-NOPE")

    assert result["resolved"] is False
    assert result["device_slug"] is None


@pytest.mark.asyncio
async def test_neither_slug_nor_serial_short_circuits_without_querying() -> None:
    """With no identifier we return not-resolved and never hit the client."""
    client = FakeNetBoxClient(_device_response(_full_device()))

    result = await resolve_device_context(client, slug=None, serial=None)

    assert result["resolved"] is False
    assert result["device_slug"] is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_missing_data_envelope_is_non_fatal() -> None:
    """A response with no 'data' key degrades gracefully to not-resolved."""
    client = FakeNetBoxClient({})

    result = await resolve_device_context(client, slug="core-sw-1", serial=None)

    assert result["resolved"] is False
    assert result["device_slug"] == "core-sw-1"


@pytest.mark.asyncio
async def test_non_dict_device_entry_is_non_fatal() -> None:
    """A malformed (non-dict) device entry degrades to not-resolved."""
    client = FakeNetBoxClient({"data": {"device_list": ["unexpected-string"]}})

    result = await resolve_device_context(client, slug="core-sw-1", serial=None)

    assert result["resolved"] is False
    assert result["device_slug"] == "core-sw-1"


# ── Result-shape contract ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_keys_are_stable_across_found_and_not_found() -> None:
    """Both branches return the exact same key set."""
    expected_keys = {"device_slug", "site", "location", "room", "tenant", "resolved"}

    hit = await resolve_device_context(
        FakeNetBoxClient(_device_response(_full_device())), slug="core-sw-1"
    )
    miss = await resolve_device_context(FakeNetBoxClient(_device_response(None)), slug="ghost")

    assert set(hit) == expected_keys
    assert set(miss) == expected_keys
