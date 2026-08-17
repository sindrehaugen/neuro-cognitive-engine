"""
tests/unit/test_economy_hardening.py
=====================================
Acceptance tests for Batch 128a — Module 8.Wave 13a (hardening).

Covers:
  1. Exact Economy tool-count assertion, scoped to the ``economy_`` prefix
     (exactly 3: ``economy_match_invoice`` / ``economy_compute_periodisering``
     / ``economy_emit_event``). Does NOT touch ``test_economy_surface.py``'s
     repo-wide ``>= 112`` check.
  2. Namespace opt-in gate (``metadata.economy.enabled``) — a non-opted-in
     namespace is cleanly refused on BOTH surfaces: MCP (``McpError(-32005)``)
     and REST (409), mirroring ``test_product_hardening.py``.
  3. Fail-closed proof: NULL metadata, ``enabled=false`` as bool AND as the
     literal string ``"false"``, and an unknown namespace all refuse — never
     a silent pass-through.
  4. Malformed ``namespace_id`` at the REST boundary returns a clean 422
     *before* the opt-in gate's DB query ever runs (never a raw
     ``asyncpg.exceptions.DataError`` / unstructured 500) — the exact
     pre-``d261bff`` defect class this wave closes for Economy.
  5. ``require_economy_enabled``'s own DataError -> EconomyDisabledError
     translation (defence in depth behind the REST boundary check).

All tests are pure unit tests (no DB, no Redis) — mocking conventions follow
``tests/unit/test_product_hardening.py``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asyncpg.exceptions import DataError as _PgDataError

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000099"

_ECONOMY_TOOLS: frozenset[str] = frozenset(
    {"economy_match_invoice", "economy_compute_periodisering", "economy_emit_event"}
)

_INVOICE: dict[str, Any] = {
    "supplier_orgnr": "987654321",
    "project_dimension_present": True,
    "lines": [{"article_no": "X", "description": "test", "line_total": 100}],
}

_PERIODISERING_PARAMS: dict[str, Any] = {"buckets": {}}

_BALANCED_EVENT: dict[str, Any] = {
    "type": "supplier.invoice.approved",
    "postings": [
        {"account": "4300", "amount": 100.0},
        {"account": "2400", "amount": -100.0},
    ],
}


# ---------------------------------------------------------------------------
# 1. Exact economy_-prefix tool count
# ---------------------------------------------------------------------------


def test_exact_economy_tool_count() -> None:
    """Economy tools registered in TOOL_REGISTRY must be exactly the 3 listed tools."""
    from nce.tool_registry import TOOL_REGISTRY

    registered_economy = {name for name in TOOL_REGISTRY if name.startswith("economy_")}
    assert registered_economy == _ECONOMY_TOOLS, (
        f"Economy tool set mismatch.\n"
        f"  Expected:   {sorted(_ECONOMY_TOOLS)}\n"
        f"  Got:        {sorted(registered_economy)}"
    )


# ---------------------------------------------------------------------------
# Fake pool helpers
# ---------------------------------------------------------------------------


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _pg_style_enabled(metadata: dict[str, Any] | None) -> bool:
    """Mimic Postgres's ``(metadata->'economy'->>'enabled')::boolean`` cast +
    ``COALESCE(..., false)`` for a handful of representative shapes, so the
    fixtures below read as "what a real row would coalesce to", not just a
    hard-coded true/false the guard is told to return.
    """
    if not isinstance(metadata, dict):
        return False
    economy = metadata.get("economy")
    if not isinstance(economy, dict):
        return False
    enabled = economy.get("enabled")
    if enabled is None:
        return False
    if isinstance(enabled, bool):
        return enabled
    if isinstance(enabled, str):
        return enabled.strip().lower() in ("t", "true", "yes", "y", "1", "on")
    return bool(enabled)


def _make_engine_for_namespaces(namespaces: dict[str, dict[str, Any] | None]) -> MagicMock:
    """Engine whose pg_pool.acquire().fetchrow() mimics the guard's real SQL:
    known namespace_id -> COALESCE-computed row; unknown namespace_id -> None
    (no row found), exactly like a real ``WHERE id = $1::uuid`` miss.
    """

    async def _fetchrow(_query: str, namespace_id: str) -> dict[str, Any] | None:
        if namespace_id not in namespaces:
            return None
        return {"economy_enabled": _pg_style_enabled(namespaces[namespace_id])}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))

    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_engine_uuid_aware(*, enabled: bool) -> MagicMock:
    """Engine whose namespaces-check connection mimics asyncpg's real
    ``$1::uuid`` cast: raises ``asyncpg.exceptions.DataError`` for a
    non-UUID-parseable namespace_id, otherwise returns the enabled flag.
    """

    async def _fetchrow(_query: str, namespace_id: str) -> dict[str, Any]:
        try:
            uuid.UUID(str(namespace_id))
        except ValueError as exc:
            raise _PgDataError(f"invalid input syntax for type uuid: {namespace_id!r}") from exc
        return {"economy_enabled": enabled}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_starlette_request(body: dict[str, Any]) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


# ---------------------------------------------------------------------------
# 2/3. Fail-closed proof — MCP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_null_metadata_namespace_refused() -> None:
    """A namespace whose metadata carries no 'economy' key at all (the real
    NULL-metadata / column-absent shape) is refused with McpError(-32005)."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {}})
    with pytest.raises(McpError) as exc_info:
        await handle_economy_match_invoice(
            engine, {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE}
        )
    assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_mcp_enabled_false_bool_namespace_refused() -> None:
    """metadata.economy.enabled = false (JSON bool) is refused."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    with pytest.raises(McpError) as exc_info:
        await handle_economy_match_invoice(
            engine, {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE}
        )
    assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_mcp_enabled_false_string_namespace_refused() -> None:
    """metadata.economy.enabled = "false" (the literal string, as Postgres's
    own ::boolean cast would also coerce to false) is refused."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": "false"}}})
    with pytest.raises(McpError) as exc_info:
        await handle_economy_match_invoice(
            engine, {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE}
        )
    assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_mcp_unknown_namespace_refused() -> None:
    """A namespace_id with no matching row at all is refused, never treated
    as implicitly enabled."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_for_namespaces({})  # no namespace known at all
    with pytest.raises(McpError) as exc_info:
        await handle_economy_match_invoice(
            engine, {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE}
        )
    assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_mcp_enabled_true_namespace_proceeds() -> None:
    """Non-regression: metadata.economy.enabled = true still proceeds to the core."""
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": True}}})
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.load_economy_thresholds",
        return_value={"green": 115, "yellow": 70, "supplier_overrides": {}},
    ):
        result = await handle_economy_match_invoice(
            engine, {"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE}
        )
    parsed = json.loads(result)
    assert "error" not in parsed
    assert "score" in parsed


@pytest.mark.asyncio
async def test_mcp_compute_periodisering_disabled_namespace_refused() -> None:
    """The gate applies to all three MCP handlers, not just match-invoice."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import (
        handle_economy_compute_periodisering,
    )

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    with pytest.raises(McpError) as exc_info:
        await handle_economy_compute_periodisering(
            engine, {"namespace_id": _NAMESPACE_ID, "params": _PERIODISERING_PARAMS}
        )
    assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_mcp_emit_event_disabled_namespace_refused() -> None:
    """The gate applies to all three MCP handlers, not just match-invoice."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    with pytest.raises(McpError) as exc_info:
        await handle_economy_emit_event(
            engine, {"namespace_id": _NAMESPACE_ID, "event": _BALANCED_EVENT}
        )
    assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_mcp_disabled_namespace_never_reaches_core() -> None:
    """The refusal must happen before any core logic runs -- an unbalanced
    event's own error path must never even get a chance to fire when the
    namespace itself is refused first."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_emit_event

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    unbalanced_event = {
        "type": "supplier.invoice.approved",
        "postings": [{"account": "4300", "amount": 1.0}, {"account": "2400", "amount": -2.0}],
    }
    with pytest.raises(McpError) as exc_info:
        await handle_economy_emit_event(
            engine, {"namespace_id": _NAMESPACE_ID, "event": unbalanced_event}
        )
    # Refused for opt-in, not for balance -- proves the gate runs first.
    assert exc_info.value.code == -32005


# ---------------------------------------------------------------------------
# 2/3. Fail-closed proof — REST surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_null_metadata_namespace_returns_409() -> None:
    from nce.admin_handlers.economy import api_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {}})
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_match_invoice(request)

    assert response.status_code == 409
    body = json.loads(response.body)
    assert "error" in body


@pytest.mark.asyncio
async def test_rest_enabled_false_bool_namespace_returns_409() -> None:
    from nce.admin_handlers.economy import api_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_match_invoice(request)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_rest_enabled_false_string_namespace_returns_409() -> None:
    from nce.admin_handlers.economy import api_economy_match_invoice

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": "false"}}})
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_match_invoice(request)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_rest_unknown_namespace_returns_409() -> None:
    from nce.admin_handlers.economy import api_economy_match_invoice

    engine = _make_engine_for_namespaces({})
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "invoice": _INVOICE})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_match_invoice(request)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_rest_periodisering_disabled_namespace_returns_409() -> None:
    from nce.admin_handlers.economy import api_economy_periodisering

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    request = _make_starlette_request(
        {"namespace_id": _NAMESPACE_ID, "params": _PERIODISERING_PARAMS}
    )

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_periodisering(request)

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_rest_emit_event_disabled_namespace_returns_409() -> None:
    from nce.admin_handlers.economy import api_economy_emit_event

    engine = _make_engine_for_namespaces({_NAMESPACE_ID: {"economy": {"enabled": False}}})
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "event": _BALANCED_EVENT})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_emit_event(request)

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# 4. REST boundary: malformed namespace_id -> structured 422 pre-gate, never
#    an escaped DataError/500. This is the exact pre-d261bff defect class:
#    the opt-in gate used to be reachable (or, before this wave, absent
#    entirely) before any UUID validation, so a malformed namespace_id would
#    hand a raw string straight to asyncpg's `WHERE id = $1::uuid` cast.
# ---------------------------------------------------------------------------

_MALFORMED_NAMESPACE_IDS = ["x", "not-a-uuid", "12345678-1234-1234-1234-12345678901"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_match_invoice_malformed_namespace_id_returns_4xx_never_escapes(
    bad_ns: str,
) -> None:
    """api_economy_match_invoice: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    from nce.admin_handlers.economy import api_economy_match_invoice

    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_starlette_request({"namespace_id": bad_ns, "invoice": _INVOICE})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_match_invoice(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_periodisering_malformed_namespace_id_returns_4xx_never_escapes(
    bad_ns: str,
) -> None:
    """api_economy_periodisering: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    from nce.admin_handlers.economy import api_economy_periodisering

    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_starlette_request({"namespace_id": bad_ns, "params": _PERIODISERING_PARAMS})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_periodisering(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_rest_emit_event_malformed_namespace_id_returns_4xx_never_escapes(
    bad_ns: str,
) -> None:
    """api_economy_emit_event: malformed namespace_id -> 4xx JSON, never an
    escaped exception, and the opt-in-gate DB query is never reached."""
    from nce.admin_handlers.economy import api_economy_emit_event

    engine = _make_engine_uuid_aware(enabled=True)
    request = _make_starlette_request({"namespace_id": bad_ns, "event": _BALANCED_EVENT})

    with patch("nce.admin_handlers.economy.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_economy_emit_event(request)

    assert 400 <= response.status_code < 500
    body = json.loads(response.body)
    assert "error" in body
    engine.pg_pool.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Guard defence-in-depth: DataError -> EconomyDisabledError translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_require_economy_enabled_translates_dataerror_defence_in_depth() -> None:
    """require_economy_enabled (Layer 2) must translate an asyncpg DataError
    (malformed ``::uuid`` cast) into EconomyDisabledError rather than letting
    the driver exception escape -- belt-and-braces behind the REST-boundary
    check exercised above."""
    from nce.vertical_modules.economy._guard import (
        EconomyDisabledError,
        require_economy_enabled,
    )

    engine = _make_engine_uuid_aware(enabled=True)

    with pytest.raises(EconomyDisabledError):
        await require_economy_enabled(engine.pg_pool, "not-a-uuid")


# ---------------------------------------------------------------------------
# 6. MCP surface: malformed namespace_id never crashes / never reaches the
#    opt-in gate's DB query. require_namespace_id's own UUID parsing
#    (mcp_args.py:extract_namespace_id) rejects it first with a ValueError,
#    which this handler's pre-existing except (ValueError, ...) clause turns
#    into a returned {"error": ...} string -- the exact same pre-existing
#    treatment as a missing namespace_id (see test 7 below), never a raised
#    exception and never a raw asyncpg.exceptions.DataError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED_NAMESPACE_IDS)
async def test_mcp_match_invoice_malformed_namespace_id_never_escapes_raw(bad_ns: str) -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_uuid_aware(enabled=True)
    result = await handle_economy_match_invoice(
        engine, {"namespace_id": bad_ns, "invoice": _INVOICE}
    )

    parsed = json.loads(result)
    assert "error" in parsed
    engine.pg_pool.acquire.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Non-regression: missing namespace_id keeps its pre-existing behaviour
#    (a returned {"error": ...} JSON string, never a raised exception) --
#    this wave adds a NEW raised-exception failure mode (McpError for a
#    disabled namespace) alongside the OLD returned-string failure mode
#    (missing namespace_id); the two must not collide.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_missing_namespace_id_still_returns_json_error_not_raise() -> None:
    from nce.vertical_modules.economy.mcp_handlers import handle_economy_match_invoice

    engine = _make_engine_for_namespaces({})
    result = await handle_economy_match_invoice(engine, {"invoice": _INVOICE})
    parsed = json.loads(result)
    assert "error" in parsed
