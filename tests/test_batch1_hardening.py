"""
tests/test_batch1_hardening.py

Unit tests for batch 1 security hardening:
1. Special character handling in passwords via secure ALTER ROLE implementation in _init_pg_schema.
2. Tenant authentication block when an admin key is fed into a tenant tool under enforce_mcp_tool_auth.
3. Uniformity verification of mTLS error code response -32015.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from nce.a2a import A2AMTLSError
from nce.auth import ScopeError, enforce_mcp_tool_auth
from nce.mtls import DEFAULT_MTLS_ERROR_CODE, MTLSAuthMiddleware
from nce.orchestrator import NCEEngine

# ---------------------------------------------------------------------------
# 1. Special character handling in passwords via secure ALTER ROLE
# ---------------------------------------------------------------------------


class MockTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockConnection:
    def __init__(self):
        self.execute = AsyncMock()
        # Q-4 step 2: _init_pg_schema now probes the migration ledger to decide whether the
        # bootstrap half runs, so a connection stand-in needs fetchval. Returning None means
        # "applied_migrations does not exist" -> treated as a fresh database, which is the
        # right default for a mock and keeps this test exercising the path it is about (the
        # nce_app password DO block), not the ledger branch.
        self.fetchval = AsyncMock(return_value=None)
        self.tx = MockTransaction()

    def transaction(self):
        return self.tx

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self, timeout=None):
        return self.conn


@pytest.mark.asyncio
async def test_init_pg_schema_special_character_password():
    # Setup the mock connection and pool
    mock_conn = MockConnection()
    mock_pool = MockPool(mock_conn)

    engine = NCEEngine()
    engine.pg_pool = mock_pool

    special_password = r"my$'special\'password''$$with_backslashes\\"

    # We patch NCE_APP_PASSWORD in the config and mock the path to schema.sql
    with (
        patch("nce.config.cfg.NCE_APP_PASSWORD", special_password),
        patch("pathlib.Path.read_text", return_value="-- mock schema ddl"),
    ):
        await engine._init_pg_schema()

    # execute() calls, in order:
    #   1. pg_advisory_xact_lock   -- schema batch
    #   2. execute(schema_bootstrap.sql)  -- Q-4 step 2; see below
    #   3. execute(schema_runtime.sql)
    #   4. pg_advisory_xact_lock   -- role refresh, same lock. The schema itself
    #      ALTER ROLEs nce_app, so an unlocked refresh races it on the pg_authid
    #      tuple; see tests/test_schema_ddl_lock.py.
    #   5. set_config
    #   6. DO block
    #
    # 5 -> 6 because Q-4 step 2 split schema.sql in two. `MockConnection.fetchval`
    # returns None, i.e. `applied_migrations` does not exist, i.e. a fresh database --
    # so BOTH halves run here. On an existing database only the runtime half would,
    # and this count would be 5 again.
    #
    # The count stays exact rather than becoming a >= : this test is about the
    # special-character password reaching the DO block as a *parameter*, but an exact
    # count is also what would catch DDL being executed twice by accident, which is
    # worth keeping now that there are two files to execute.
    calls = mock_conn.execute.call_args_list
    assert len(calls) == 6, [c[0][0] for c in calls]

    locks = [c for c in calls if "pg_advisory_xact_lock" in c[0][0]]
    assert len(locks) == 2, "both DDL transactions must take the schema advisory lock"

    (set_config,) = [c for c in calls if "set_config" in c[0][0]]
    assert set_config[0][0] == "SELECT set_config('nce.temp_password', $1, true)"
    assert set_config[0][1] == special_password

    # Verify the DO block executes safely using current_setting
    (do_block,) = [c for c in calls if "DO $$" in c[0][0]]
    assert "ALTER ROLE nce_app WITH LOGIN PASSWORD %L" in do_block[0][0]
    assert "current_setting('nce.temp_password')" in do_block[0][0]


# ---------------------------------------------------------------------------
# 2. Tenant authentication block when an admin key is fed into a tenant tool
# ---------------------------------------------------------------------------


def test_tenant_tool_rejects_admin_api_key():
    # Mock NCE_MCP_API_KEY / _mcp_server_api_key
    with patch("nce.auth._mcp_server_api_key", return_value="mcp-secret-key"):
        # Passing admin_api_key to a tenant tool (e.g., store_memory) without mcp_api_key should raise ScopeError
        with pytest.raises(ScopeError) as exc_info:
            enforce_mcp_tool_auth("store_memory", {"admin_api_key": "admin-secret-key"})
        assert exc_info.value.required_scope == "tenant"
        assert "missing mcp_api_key" in exc_info.value.reason

        # Passing admin_api_key to a tenant tool with an invalid mcp_api_key should also raise ScopeError
        with pytest.raises(ScopeError) as exc_info:
            enforce_mcp_tool_auth(
                "store_memory",
                {"admin_api_key": "admin-secret-key", "mcp_api_key": "invalid-mcp-key"},
            )
        assert exc_info.value.required_scope == "tenant"
        assert "invalid mcp_api_key" in exc_info.value.reason


# ---------------------------------------------------------------------------
# 3. Uniformity verification of mTLS error code response -32015
# ---------------------------------------------------------------------------


def _make_scope(path: str = "/api/v1") -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
    }


async def _collect_response(middleware: MTLSAuthMiddleware, scope: dict) -> dict:
    received: dict = {}

    async def receive():
        return {}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            raw_headers = message.get("headers", [])
            received["headers"] = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers
            }
        elif message["type"] == "http.response.body":
            received["body"] = message.get("body", b"")

    await middleware(scope, receive, send)
    return received


@pytest.mark.asyncio
async def test_mtls_middleware_default_error_code():
    # Instantiate the middleware with enabled=True and standard DNS SANS to trust
    downstream = AsyncMock()
    mw = MTLSAuthMiddleware(
        downstream,
        enabled=True,
        protected_prefix="/api",
        allowed_sans=["trusted.internal"],
    )

    # Assert constructor default matches -32015
    assert mw.error_code == -32015
    assert mw.error_code == DEFAULT_MTLS_ERROR_CODE

    # Force a certificate validation failure
    with patch("nce.mtls.mtls_enforce", side_effect=A2AMTLSError("Cert is untrusted")):
        result = await _collect_response(mw, _make_scope(path="/api/data"))

    # Assert HTTP status and response body error code
    assert result["status"] == 401
    body = json.loads(result["body"])
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32015
    assert "mTLS client certificate validation failed" in body["error"]["message"]
