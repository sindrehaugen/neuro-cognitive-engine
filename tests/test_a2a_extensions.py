"""Tests for enriched A2A lifecycle operations and MCP handlers."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import a2a_mcp_handlers
from nce.a2a import (
    A2AAuthorizationError,
    A2AScope,
    inspect_grant,
    update_grant_scopes,
    verify_grant_status,
)
from nce.auth import NamespaceContext


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
    return engine


def _grant_row_dict(
    *,
    grant_id: uuid.UUID | None = None,
    owner_ns: uuid.UUID | None = None,
    owner_agent: str = "agent-a",
    consumer_ns: uuid.UUID | None = None,
    consumer_agent: str | None = None,
    scopes: list[dict] | None = None,
    status: str = "active",
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    can_delegate: bool = False,
) -> dict:
    return {
        "id": grant_id or uuid.uuid4(),
        "owner_namespace_id": owner_ns or uuid.uuid4(),
        "owner_agent_id": owner_agent,
        "target_namespace_id": consumer_ns,
        "target_agent_id": consumer_agent,
        "scopes": json.dumps(
            scopes
            or [
                {
                    "resource_type": "namespace",
                    "resource_id": str(uuid.uuid4()),
                    "permissions": ["read"],
                }
            ]
        ),
        "status": status,
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        "created_at": created_at or datetime.now(timezone.utc),
        "can_delegate": can_delegate,
        "one_time": False,
        "usage_count": 0,
    }


# ============================================================================
# 1. Domain Operations Tests (verify_grant_status)
# ============================================================================


@pytest.mark.asyncio
async def test_verify_grant_status_parameter_bounds() -> None:
    """verify_grant_status must raise ValueError if neither or both parameters are passed."""
    conn = AsyncMock()
    ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="agent-caller")

    with pytest.raises(ValueError, match="Must provide exactly one of"):
        await verify_grant_status(conn, ctx, sharing_token=None, grant_id=None)

    with pytest.raises(ValueError, match="Must provide exactly one of"):
        await verify_grant_status(conn, ctx, sharing_token="tok", grant_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_verify_grant_status_not_found() -> None:
    """verify_grant_status raises A2AAuthorizationError if grant does not exist."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="agent-caller")

    with pytest.raises(A2AAuthorizationError, match="Grant not found."):
        await verify_grant_status(conn, ctx, grant_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_verify_grant_status_auto_expires() -> None:
    """verify_grant_status updates status to 'expired' in DB if expires_at is in the past."""
    conn = AsyncMock()
    grant_id = uuid.uuid4()
    owner_ns = uuid.uuid4()
    past_expiry = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = _grant_row_dict(
        grant_id=grant_id, owner_ns=owner_ns, expires_at=past_expiry, status="active"
    )
    conn.fetchrow = AsyncMock(return_value=row)

    ctx = NamespaceContext(namespace_id=owner_ns, agent_id="agent-caller")
    res = await verify_grant_status(conn, ctx, grant_id=grant_id)

    # Verify transition in response
    assert res["status"] == "expired"
    # Verify UPDATE statement was run
    conn.execute.assert_called_once()
    assert "UPDATE a2a_grants SET status = 'expired'" in conn.execute.call_args[0][0]


@pytest.mark.asyncio
async def test_verify_grant_status_security_boundaries() -> None:
    """verify_grant_status enforces caller access privileges correctly."""
    conn = AsyncMock()
    grant_id = uuid.uuid4()
    owner_ns = uuid.uuid4()
    target_ns = uuid.uuid4()

    row = _grant_row_dict(
        grant_id=grant_id, owner_ns=owner_ns, consumer_ns=target_ns, consumer_agent="bot-a"
    )
    conn.fetchrow = AsyncMock(return_value=row)

    # 1. Unauthorized caller (different namespace) raises error
    unauth_ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="bot-a")
    with pytest.raises(A2AAuthorizationError, match="Unauthorized access to grant status."):
        await verify_grant_status(conn, unauth_ctx, grant_id=grant_id)

    # 2. Owner caller is authorized
    owner_ctx = NamespaceContext(namespace_id=owner_ns, agent_id="owner-agent")
    res_owner = await verify_grant_status(conn, owner_ctx, grant_id=grant_id)
    assert res_owner["grant_id"] == str(grant_id)

    # 3. Target caller (wrong agent) raises error
    target_wrong_agent_ctx = NamespaceContext(namespace_id=target_ns, agent_id="bot-b")
    with pytest.raises(A2AAuthorizationError, match="Unauthorized access to grant status."):
        await verify_grant_status(conn, target_wrong_agent_ctx, grant_id=grant_id)

    # 4. Target caller (correct namespace + agent) is authorized
    target_ctx = NamespaceContext(namespace_id=target_ns, agent_id="bot-a")
    res_target = await verify_grant_status(conn, target_ctx, grant_id=grant_id)
    assert res_target["grant_id"] == str(grant_id)

    # 5. Target caller in unrestricted target namespace (target_ns = None) is authorized
    row_unrestricted = _grant_row_dict(
        grant_id=grant_id, owner_ns=owner_ns, consumer_ns=None, consumer_agent=None
    )
    conn.fetchrow = AsyncMock(return_value=row_unrestricted)
    any_ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="any-agent")
    res_any = await verify_grant_status(conn, any_ctx, grant_id=grant_id)
    assert res_any["grant_id"] == str(grant_id)


# ============================================================================
# 2. Domain Operations Tests (update_grant_scopes)
# ============================================================================


@pytest.mark.asyncio
async def test_update_grant_scopes_not_found_or_unauthorized() -> None:
    """update_grant_scopes raises error if grant does not exist or caller is not owner."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    owner_ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="owner-agent")

    with pytest.raises(A2AAuthorizationError, match="Grant not found or unauthorized."):
        await update_grant_scopes(
            conn,
            owner_ctx,
            uuid.uuid4(),
            [
                A2AScope(
                    resource_type="namespace", resource_id=str(uuid.uuid4()), permissions=["read"]
                )
            ],
        )


@pytest.mark.asyncio
async def test_update_grant_scopes_inactive_or_expired() -> None:
    """update_grant_scopes rejects inactive or expired grants."""
    conn = AsyncMock()
    grant_id = uuid.uuid4()
    owner_ns = uuid.uuid4()
    owner_ctx = NamespaceContext(namespace_id=owner_ns, agent_id="owner-agent")

    # 1. Inactive grant
    row_inactive = _grant_row_dict(grant_id=grant_id, owner_ns=owner_ns, status="revoked")
    conn.fetchrow = AsyncMock(return_value=row_inactive)
    with pytest.raises(A2AAuthorizationError, match="Cannot update scopes of an inactive grant."):
        await update_grant_scopes(
            conn,
            owner_ctx,
            grant_id,
            [
                A2AScope(
                    resource_type="namespace", resource_id=str(uuid.uuid4()), permissions=["read"]
                )
            ],
        )

    # 2. Expired grant
    past_expiry = datetime.now(timezone.utc) - timedelta(minutes=5)
    row_expired = _grant_row_dict(
        grant_id=grant_id, owner_ns=owner_ns, status="active", expires_at=past_expiry
    )
    conn.fetchrow = AsyncMock(return_value=row_expired)
    with pytest.raises(A2AAuthorizationError, match="Cannot update scopes of an expired grant."):
        await update_grant_scopes(
            conn,
            owner_ctx,
            grant_id,
            [
                A2AScope(
                    resource_type="namespace", resource_id=str(uuid.uuid4()), permissions=["read"]
                )
            ],
        )


@pytest.mark.asyncio
async def test_update_grant_scopes_success_strategies() -> None:
    """update_grant_scopes applies replace/append correctly and appends audit event."""
    conn = AsyncMock()
    grant_id = uuid.uuid4()
    owner_ns = uuid.uuid4()
    owner_ctx = NamespaceContext(namespace_id=owner_ns, agent_id="owner-agent")

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    existing_scope_id = str(uuid.uuid4())
    existing_scopes = [
        {"resource_type": "namespace", "resource_id": existing_scope_id, "permissions": ["read"]}
    ]
    row = _grant_row_dict(
        grant_id=grant_id, owner_ns=owner_ns, scopes=existing_scopes, status="active"
    )

    new_scope_id = str(uuid.uuid4())
    new_scopes = [
        A2AScope(resource_type="namespace", resource_id=new_scope_id, permissions=["read"])
    ]

    # Mock fetchrow to return existing grant
    conn.fetchrow = AsyncMock(return_value=row)

    # 1. Replace mode
    with patch("nce.a2a.set_namespace_context", AsyncMock()):
        with patch("nce.event_log.append_event", AsyncMock()) as mock_append:
            res = await update_grant_scopes(conn, owner_ctx, grant_id, new_scopes, mode="replace")
            assert len(res["scopes"]) == 1
            assert res["scopes"][0]["resource_id"] == new_scope_id

            # Check UPDATE SQL execution
            assert conn.execute.called
            mock_append.assert_called_once()
            assert mock_append.call_args[1]["event_type"] == "a2a_grant_updated"
            assert mock_append.call_args[1]["params"]["mode"] == "replace"

    # 2. Append mode
    conn.execute.reset_mock()
    conn.fetchrow = AsyncMock(return_value=row)
    with patch("nce.a2a.set_namespace_context", AsyncMock()):
        with patch("nce.event_log.append_event", AsyncMock()) as mock_append:
            res = await update_grant_scopes(conn, owner_ctx, grant_id, new_scopes, mode="append")
            assert len(res["scopes"]) == 2
            # Verify both original and new exist
            ids = {s["resource_id"] for s in res["scopes"]}
            assert existing_scope_id in ids
            assert new_scope_id in ids

            assert conn.execute.called
            mock_append.assert_called_once()
            assert mock_append.call_args[1]["params"]["mode"] == "append"

    # 3. Invalid strategy
    with pytest.raises(ValueError, match="Invalid scope update mode"):
        await update_grant_scopes(conn, owner_ctx, grant_id, new_scopes, mode="invalid")

    # 4. Zero scopes validation
    with pytest.raises(ValueError, match="at least one active scope"):
        await update_grant_scopes(conn, owner_ctx, grant_id, [], mode="replace")


# ============================================================================
# 3. Domain Operations Tests (inspect_grant)
# ============================================================================


@pytest.mark.asyncio
async def test_inspect_grant_not_found_or_unauthorized() -> None:
    """inspect_grant raises error if grant does not exist or caller is not owner."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    owner_ctx = NamespaceContext(namespace_id=uuid.uuid4(), agent_id="owner-agent")

    with pytest.raises(A2AAuthorizationError, match="Grant not found or unauthorized."):
        await inspect_grant(conn, owner_ctx, uuid.uuid4())


@pytest.mark.asyncio
async def test_inspect_grant_success() -> None:
    """inspect_grant returns metadata securely without token hash."""
    conn = AsyncMock()
    grant_id = uuid.uuid4()
    owner_ns = uuid.uuid4()
    owner_ctx = NamespaceContext(namespace_id=owner_ns, agent_id="owner-agent")

    row = _grant_row_dict(grant_id=grant_id, owner_ns=owner_ns)
    conn.fetchrow = AsyncMock(return_value=row)

    res = await inspect_grant(conn, owner_ctx, grant_id)
    assert res["grant_id"] == str(grant_id)
    assert res["owner_namespace_id"] == str(owner_ns)
    assert "token_hash" not in res  # Ensures cryptographic safety


# ============================================================================
# 4. MCP Handlers Tests
# ============================================================================


@pytest.mark.asyncio
async def test_handle_a2a_verify_grant_status_mcp() -> None:
    """Verify MCP integration for handle_a2a_verify_grant_status."""
    engine = _engine_pool_context()
    caller_ns = uuid.uuid4()
    args = {
        "namespace_id": str(caller_ns),
        "agent_id": "caller-agent",
        "sharing_token": "bearer-token-abc",
    }

    mock_res = {"grant_id": str(uuid.uuid4()), "status": "active", "scopes": []}

    with patch(
        "nce.a2a_mcp_handlers.verify_grant_status", AsyncMock(return_value=mock_res)
    ) as mock_domain:
        res_str = await a2a_mcp_handlers.handle_a2a_verify_grant_status(engine, args)
        res = json.loads(res_str)
        assert res["status"] == "active"
        mock_domain.assert_called_once()
        # Verify context generation
        ctx = mock_domain.call_args[1]["ctx"]
        assert ctx.namespace_id == caller_ns
        assert ctx.agent_id == "caller-agent"


@pytest.mark.asyncio
async def test_handle_a2a_update_grant_scopes_mcp() -> None:
    """Verify MCP integration for handle_a2a_update_grant_scopes."""
    engine = _engine_pool_context()
    owner_ns = uuid.uuid4()
    grant_id = uuid.uuid4()
    args = {
        "namespace_id": str(owner_ns),
        "agent_id": "owner-agent",
        "grant_id": str(grant_id),
        "scopes": [
            {
                "resource_type": "namespace",
                "resource_id": str(uuid.uuid4()),
                "permissions": ["read"],
            }
        ],
        "mode": "append",
    }

    mock_res = {"grant_id": str(grant_id), "status": "updated", "scopes": []}

    with patch(
        "nce.a2a_mcp_handlers.update_grant_scopes", AsyncMock(return_value=mock_res)
    ) as mock_domain:
        res_str = await a2a_mcp_handlers.handle_a2a_update_grant_scopes(engine, args)
        res = json.loads(res_str)
        assert res["status"] == "updated"
        mock_domain.assert_called_once()
        assert mock_domain.call_args[1]["grant_id"] == grant_id
        assert mock_domain.call_args[1]["mode"] == "append"


@pytest.mark.asyncio
async def test_handle_a2a_inspect_grant_mcp() -> None:
    """Verify MCP integration for handle_a2a_inspect_grant."""
    engine = _engine_pool_context()
    owner_ns = uuid.uuid4()
    grant_id = uuid.uuid4()
    args = {
        "namespace_id": str(owner_ns),
        "agent_id": "owner-agent",
        "grant_id": str(grant_id),
    }

    mock_res = {"grant_id": str(grant_id), "owner_namespace_id": str(owner_ns), "status": "active"}

    with patch(
        "nce.a2a_mcp_handlers.inspect_grant", AsyncMock(return_value=mock_res)
    ) as mock_domain:
        res_str = await a2a_mcp_handlers.handle_a2a_inspect_grant(engine, args)
        res = json.loads(res_str)
        assert res["grant_id"] == str(grant_id)
        mock_domain.assert_called_once()
