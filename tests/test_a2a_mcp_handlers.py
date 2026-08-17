"""Contract tests for A2A MCP handlers (nce.a2a_mcp_handlers).

Mocks engine.pg_pool and domain functions — no live Postgres.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from nce import a2a_mcp_handlers
from nce.a2a import (
    A2AAuthorizationError,
    A2AGrantResponse,
    A2AScope,
    A2AScopeViolationError,
    VerifiedGrant,
)
from nce.mcp_errors import MCP_INVALID_PARAMS, McpError

NS = "00000000-0000-4000-8000-000000000001"
CONSUMER_NS = "00000000-0000-4000-8000-000000000002"
OWNER_NS = "00000000-0000-4000-8000-000000000003"


class _FakeAcquire:
    __slots__ = ("_conn",)

    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _engine_pool_context(conn: object | None = None) -> MagicMock:
    conn = conn or AsyncMock()
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.pg_pool.acquire = MagicMock(side_effect=lambda *_a, **_k: _FakeAcquire(conn))
    engine.semantic_search = AsyncMock(return_value=[])
    return engine


def _namespace_scope(ns: str = NS) -> A2AScope:
    return A2AScope(
        resource_type="namespace",
        resource_id=ns,
        permissions=["read"],
    )


def _query_shared_base(**overrides: object) -> dict:
    base = {
        "consumer_namespace_id": CONSUMER_NS,
        "consumer_agent_id": "consumer-agent",
        "sharing_token": "valid-sharing-token",
        "query": "hello world",
    }
    base.update(overrides)
    return base


def _verified_grant(
    *,
    owner_namespace_id: uuid.UUID | None = None,
    scopes: list[A2AScope] | None = None,
    can_delegate: bool = False,
) -> VerifiedGrant:
    owner_ns = owner_namespace_id or uuid.UUID(OWNER_NS)
    return VerifiedGrant(
        grant_id=uuid.uuid4(),
        owner_namespace_id=owner_ns,
        owner_agent_id="owner-agent",
        scopes=scopes or [_namespace_scope(str(owner_ns))],
        expires_at=datetime.now(timezone.utc),
        can_delegate=can_delegate,
    )


def _unwrap(handler):  # noqa: ANN001
    return getattr(handler, "__wrapped__", handler)


@pytest.fixture
def engine() -> MagicMock:
    return _engine_pool_context()


@pytest.fixture
def scopes() -> list[A2AScope]:
    return [_namespace_scope(OWNER_NS)]


@pytest.fixture(autouse=True)
def mock_append_a2a_event() -> Generator[AsyncMock, None, None]:
    with patch("nce.a2a._append_a2a_event", new_callable=AsyncMock) as m:
        yield m


# ---------------------------------------------------------------------------
# handle_a2a_revoke_grant — grant_id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_grant_missing_grant_id_raises(engine: MagicMock) -> None:
    with pytest.raises(McpError) as exc:
        await a2a_mcp_handlers.handle_a2a_revoke_grant(engine, {"namespace_id": NS})
    assert exc.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
async def test_revoke_grant_invalid_uuid_raises(engine: MagicMock) -> None:
    with pytest.raises(McpError) as exc:
        await a2a_mcp_handlers.handle_a2a_revoke_grant(
            engine,
            {"namespace_id": NS, "grant_id": "not-a-uuid"},
        )
    assert exc.value.code == MCP_INVALID_PARAMS
    assert "UUID" in exc.value.message or exc.value.data.get("reason") == "invalid_arguments"


@pytest.mark.asyncio
async def test_revoke_grant_valid_uuid_passed_to_domain(engine: MagicMock) -> None:
    grant_id = uuid.uuid4()
    with patch("nce.a2a_mcp_handlers.revoke_grant", new_callable=AsyncMock) as revoke:
        revoke.return_value = True
        out = await a2a_mcp_handlers.handle_a2a_revoke_grant(
            engine,
            {"namespace_id": NS, "grant_id": str(grant_id)},
        )
    assert json.loads(out)["revoked"] is True
    revoke.assert_awaited_once()
    assert revoke.call_args[0][1] == grant_id


# ---------------------------------------------------------------------------
# handle_a2a_list_grants — JSON serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_grants_serializes_uuid_and_status_ok(engine: MagicMock) -> None:
    grant_id = uuid.uuid4()
    expires = datetime.now(timezone.utc)
    with patch("nce.a2a_mcp_handlers.list_grants", new_callable=AsyncMock) as list_grants:
        list_grants.return_value = [
            {"grant_id": grant_id, "expires_at": expires},
        ]
        out = await a2a_mcp_handlers.handle_a2a_list_grants(
            engine,
            {"namespace_id": NS, "include_inactive": False},
        )
    data = json.loads(out)
    assert data["status"] == "ok"
    assert "grants" in data
    assert data["grants"][0]["grant_id"] == str(grant_id)
    assert isinstance(data["grants"][0]["expires_at"], str)


# ---------------------------------------------------------------------------
# handle_a2a_query_shared — Pydantic / token length
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_shared_missing_sharing_token_validation_error(
    engine: MagicMock,
) -> None:
    args = _query_shared_base()
    del args["sharing_token"]
    with pytest.raises(ValidationError):
        await _unwrap(a2a_mcp_handlers.handle_a2a_query_shared)(engine, args)


@pytest.mark.asyncio
async def test_query_shared_sharing_token_too_long_raises(engine: MagicMock) -> None:
    token = "x" * (a2a_mcp_handlers._MAX_SHARING_TOKEN_LEN + 1)
    with pytest.raises(ValueError, match="4096"):
        await _unwrap(a2a_mcp_handlers.handle_a2a_query_shared)(
            engine, _query_shared_base(sharing_token=token)
        )

    with pytest.raises(McpError) as exc:
        await a2a_mcp_handlers.handle_a2a_query_shared(
            engine, _query_shared_base(sharing_token=token)
        )
    assert exc.value.code == MCP_INVALID_PARAMS
    assert exc.value.data.get("reason") == "invalid_arguments"


@pytest.mark.asyncio
async def test_query_shared_valid_token_calls_verify_token(
    engine: MagicMock, scopes: list[A2AScope]
) -> None:
    verified = _verified_grant(scopes=scopes)
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
    ):
        verify.return_value = verified
        await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base())
    verify.assert_awaited_once()
    assert verify.call_args[0][1] == "valid-sharing-token"


@pytest.mark.asyncio
async def test_query_shared_invalid_resource_type_validation_error(
    engine: MagicMock,
) -> None:
    with pytest.raises(ValidationError):
        await _unwrap(a2a_mcp_handlers.handle_a2a_query_shared)(
            engine, _query_shared_base(resource_type="invalid")
        )


@pytest.mark.asyncio
async def test_query_shared_memory_without_resource_id_raises(
    engine: MagicMock, scopes: list[A2AScope]
) -> None:
    verified = _verified_grant(scopes=scopes)
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope") as enforce,
    ):
        verify.return_value = verified
        with pytest.raises(ValueError, match="resource_id"):
            await _unwrap(a2a_mcp_handlers.handle_a2a_query_shared)(
                engine,
                _query_shared_base(resource_type="memory", resource_id=None),
            )
    enforce.assert_not_called()


@pytest.mark.asyncio
async def test_query_shared_namespace_without_resource_id_uses_owner_fallback(
    engine: MagicMock, scopes: list[A2AScope]
) -> None:
    owner_ns = uuid.UUID(OWNER_NS)
    verified = _verified_grant(owner_namespace_id=owner_ns, scopes=scopes)
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope") as enforce,
    ):
        verify.return_value = verified
        await a2a_mcp_handlers.handle_a2a_query_shared(
            engine,
            _query_shared_base(resource_type="namespace", resource_id=None),
        )
    enforce.assert_called_once()
    assert enforce.call_args[0][2] == str(owner_ns)


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, 101])
async def test_query_shared_top_k_out_of_range_validation_error(
    engine: MagicMock, top_k: int
) -> None:
    with pytest.raises(ValidationError):
        await _unwrap(a2a_mcp_handlers.handle_a2a_query_shared)(
            engine, _query_shared_base(top_k=top_k)
        )


@pytest.mark.asyncio
async def test_query_shared_top_k_passed_to_semantic_search(
    engine: MagicMock, scopes: list[A2AScope]
) -> None:
    verified = _verified_grant(scopes=scopes)
    engine.semantic_search = AsyncMock(return_value=[])
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
    ):
        verify.return_value = verified
        await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base(top_k=5))
    engine.semantic_search.assert_awaited_once()
    assert engine.semantic_search.call_args.kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_query_shared_extra_field_forbidden(engine: MagicMock) -> None:
    with pytest.raises(ValidationError):
        await _unwrap(a2a_mcp_handlers.handle_a2a_query_shared)(
            engine, _query_shared_base(foo="bar")
        )


@pytest.mark.asyncio
async def test_query_shared_results_datetime_json_default_str(
    engine: MagicMock, scopes: list[A2AScope]
) -> None:
    verified = _verified_grant(scopes=scopes)
    ts = datetime.now(timezone.utc)
    engine.semantic_search = AsyncMock(return_value=[{"memory_id": uuid.uuid4(), "created_at": ts}])
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
    ):
        verify.return_value = verified
        out = await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base())
    data = json.loads(out)
    assert isinstance(data["results"][0]["created_at"], str)


# ---------------------------------------------------------------------------
# Self-access warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_shared_self_access_logs_warning(
    engine: MagicMock, caplog: pytest.LogCaptureFixture, scopes: list[A2AScope]
) -> None:
    owner_ns = uuid.UUID(CONSUMER_NS)
    verified = _verified_grant(owner_namespace_id=owner_ns, scopes=scopes)
    caplog.set_level(logging.WARNING, logger="nce.a2a_mcp_handlers")
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
    ):
        verify.return_value = verified
        await a2a_mcp_handlers.handle_a2a_query_shared(
            engine,
            _query_shared_base(consumer_namespace_id=CONSUMER_NS),
        )
    assert any("self-access" in rec.message.lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_query_shared_different_namespace_no_self_access_warning(
    engine: MagicMock, caplog: pytest.LogCaptureFixture, scopes: list[A2AScope]
) -> None:
    verified = _verified_grant(scopes=scopes)
    caplog.set_level(logging.WARNING, logger="nce.a2a_mcp_handlers")
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
    ):
        verify.return_value = verified
        await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base())
    assert not any("self-access" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Namespace isolation — domain errors propagate (handler does not swallow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_shared_enforce_scope_failure_propagates(
    engine: MagicMock, scopes: list[A2AScope]
) -> None:
    verified = _verified_grant(scopes=scopes)
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope") as enforce,
    ):
        verify.return_value = verified
        enforce.side_effect = A2AScopeViolationError("scope denied")
        with pytest.raises(McpError):
            await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base())
    engine.semantic_search.assert_not_called()


@pytest.mark.asyncio
async def test_query_shared_verify_token_failure_propagates(
    engine: MagicMock,
) -> None:
    with patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify:
        verify.side_effect = A2AAuthorizationError("Invalid or revoked sharing token.")
        with pytest.raises(McpError):
            await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base())
    engine.semantic_search.assert_not_called()


# ---------------------------------------------------------------------------
# Smoke: create_grant still wires through pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_grant_returns_json(engine: MagicMock) -> None:
    exp = datetime.now(timezone.utc)
    grant = A2AGrantResponse(
        grant_id=uuid.uuid4(),
        sharing_token="tok-create",
        expires_at=exp,
    )
    scopes_json = json.dumps(
        [{"resource_type": "namespace", "resource_id": NS, "permissions": ["read"]}]
    )
    with patch("nce.a2a_mcp_handlers.create_grant", new_callable=AsyncMock) as create:
        create.return_value = grant
        out = await a2a_mcp_handlers.handle_a2a_create_grant(
            engine,
            {
                "namespace_id": NS,
                "scopes": scopes_json,
                "expires_in_seconds": 120,
            },
        )
    data = json.loads(out)
    assert data["sharing_token"] == "tok-create"
    assert data["grant_id"]


@pytest.mark.asyncio
async def test_query_shared_writes_a2a_shared_query_event(
    engine: MagicMock, scopes: list[A2AScope], mock_append_a2a_event: AsyncMock
) -> None:
    verified = _verified_grant(scopes=scopes)
    with (
        patch("nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock) as verify,
        patch("nce.a2a_mcp_handlers.enforce_scope"),
    ):
        verify.return_value = verified
        await a2a_mcp_handlers.handle_a2a_query_shared(engine, _query_shared_base())

    mock_append_a2a_event.assert_called_once()
    call_args = mock_append_a2a_event.call_args[1]
    assert call_args["event_type"] == "a2a_shared_query"
    params = call_args["params"]
    assert params["consumer_namespace_id"] == CONSUMER_NS
    assert params["consumer_agent_id"] == "consumer-agent"
    assert params["grant_id"] == str(verified.grant_id)
    assert params["query"] == "hello world"
