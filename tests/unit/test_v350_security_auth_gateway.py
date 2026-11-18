"""Contract tests for v3.5.0 security hardening across auth, gateway, bridges, and config."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nce import admin_plugins, bridge_mcp_handlers, bridge_repo
from nce._http_utils import UnsafeURLError, _resolve_safe_ip
from nce.admin_app import build_admin_middleware
from nce.auth import (
    MCP_ADMIN_TOOL_NAMES,
    BasicAuthMiddleware,
    HMACAuthMiddleware,
    NonceStore,
    bind_mcp_http_session,
)
from nce.config import _Config, cfg
from nce.mcp_errors import McpError
from nce.mcp_http_gateway import (
    _GATEWAY_SESSIONS,
    _IN_MEMORY_MCP_CLIENT_TOKENS,
    _MAX_GATEWAY_SESSIONS,
    _MAX_SESSIONS_PER_TOKEN,
    MCPGatewaySession,
    _store_gateway_session,
    create_mcp_gateway_app,
    register_test_mcp_client_token,
    verify_mcp_token,
)
from nce.mcp_stdio_tools import TOOLS
from nce.mtls import MTLSAuthMiddleware
from nce.outbound_webhooks.service import send_webhook_http_sync
from nce.settings_registry import REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_hmac_headers(
    key: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    ts: int | None = None,
    nonce: str | None = "nonce-000000000001",
) -> dict[str, str]:
    now_ts = int(time.time()) if ts is None else ts
    parts = [method.upper(), path, str(now_ts), nonce or ""]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    sig = hmac.new(
        key.encode("utf-8"), "\n".join(parts).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    hdrs = {
        "X-NCE-Timestamp": str(now_ts),
        "Authorization": f"HMAC-SHA256 {sig}",
    }
    if nonce is not None:
        hdrs["X-NCE-Nonce"] = nonce
    return hdrs


# ---------------------------------------------------------------------------
# S2: MTLSAuthMiddleware, BasicAuthMiddleware, admin_plugins
# ---------------------------------------------------------------------------


def test_mtls_middleware_protected_prefixes_and_admin_app_wiring() -> None:
    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/api/health", ok),
            Route("/admin/plugins", ok),
            Route("/admin/plugins/api", ok),
            Route("/admin/plugins/api/toggle", ok, methods=["POST"]),
            Route("/admin/plugins/apiX", ok),
        ],
        middleware=[
            Middleware(
                MTLSAuthMiddleware,
                protected_prefixes=("/api/", "/admin/plugins/api"),
                enabled=True,
                strict=True,
                allowed_sans=["spiffe://nce/admin"],
            )
        ],
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/health").status_code == 401
    assert client.get("/admin/plugins/api").status_code == 401
    assert client.post("/admin/plugins/api/toggle").status_code == 401
    assert client.get("/admin/plugins").status_code == 200
    assert client.get("/admin/plugins/apiX").status_code == 200

    mw_list = build_admin_middleware()
    mtls_mw = next(m for m in mw_list if m.cls is MTLSAuthMiddleware)
    assert mtls_mw.kwargs["protected_prefixes"] == ("/api/", "/admin/plugins/api")


def test_basic_auth_excluded_prefixes_boundary() -> None:
    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/admin/plugins/api", ok),
            Route("/admin/plugins/api/toggle", ok),
            Route("/admin/plugins/apiX", ok),
            Route("/api/ping", ok),
        ],
        middleware=[
            Middleware(
                BasicAuthMiddleware,
                protected_prefix="/",
                excluded_prefixes=("/api/", "/admin/plugins/api", "/healthz"),
                username="admin",
                password="secret",
            )
        ],
    )
    with patch.object(cfg, "NCE_ADMIN_OVERRIDE", False):
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/admin/plugins/api").status_code == 200
        assert client.get("/admin/plugins/api/toggle").status_code == 200
        assert client.get("/api/ping").status_code == 200
        assert client.get("/admin/plugins/apiX").status_code == 401


def test_admin_plugins_check_auth_none_headers_denies() -> None:
    req = SimpleNamespace(headers=None)
    resp = admin_plugins._check_admin_auth(req)  # type: ignore[arg-type]
    assert resp is not None
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_plugins_hmac_timestamp_and_nonce_replay() -> None:
    seen: set[str] = set()
    store = MagicMock(spec=NonceStore)
    store.ping = AsyncMock(return_value=True)

    async def _check_and_store(val: str) -> bool:
        if val in seen:
            return False
        seen.add(val)
        return True

    store.check_and_store = AsyncMock(side_effect=_check_and_store)

    def _make_req(headers: dict[str, str], body: bytes = b"") -> Request:
        async def _recv() -> dict[str, object]:
            return {"type": "http.request", "body": body}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/plugins/api",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
        }
        return Request(scope, _recv)

    with (
        patch.object(cfg, "NCE_API_KEY", "hmac-secret-key"),
        patch.object(cfg, "NCE_HMAC_NONCE_REQUIRED", True),
        patch("nce.admin_app._hmac_nonce_store", store),
    ):
        expired_hdrs = _make_hmac_headers(
            "hmac-secret-key", "GET", "/admin/plugins/api", ts=int(time.time()) - 400
        )
        r_exp = await admin_plugins._check_admin_auth_async(_make_req(expired_hdrs))
        assert r_exp is not None and r_exp.status_code == 401

        fresh_hdrs = _make_hmac_headers(
            "hmac-secret-key", "GET", "/admin/plugins/api", nonce="nonce-00000000000001"
        )
        r_ok = await admin_plugins._check_admin_auth_async(_make_req(fresh_hdrs))
        assert r_ok is None

        # Replay of same signature+nonce rejected
        r_rep = await admin_plugins._check_admin_auth_async(_make_req(fresh_hdrs))
        assert r_rep is not None and r_rep.status_code == 401

        # Replay of same nonce with new timestamp/sig rejected
        hdrs_same_nonce = _make_hmac_headers(
            "hmac-secret-key",
            "GET",
            "/admin/plugins/api",
            ts=int(time.time()) + 1,
            nonce="nonce-00000000000001",
        )
        r_rep2 = await admin_plugins._check_admin_auth_async(_make_req(hdrs_same_nonce))
        assert r_rep2 is not None and r_rep2.status_code == 401


@pytest.mark.asyncio
async def test_admin_plugins_mint_token_validation_and_db_rollback() -> None:
    def _make_req(payload: dict[str, object], pg_pool: object = None) -> Request:
        raw = json.dumps(payload).encode("utf-8")

        async def _recv() -> dict[str, object]:
            return {"type": "http.request", "body": raw}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/admin/plugins/api/mint-token",
            "headers": [(b"content-type", b"application/json")],
            "app": MagicMock(state=MagicMock(pg_pool=pg_pool)),
        }
        return Request(scope, _recv)

    with (
        patch("nce.admin_plugins.admin_state", MagicMock(engine=None)),
        patch("nce.admin_plugins._check_admin_auth", return_value=None),
    ):
        r_bad_ns = await admin_plugins.admin_plugins_mint_token(
            _make_req({"namespace_id": "not-a-uuid"})
        )
        assert r_bad_ns.status_code == 400

        for bad_rpm in (0, 10001, True, "fast"):
            r_rpm = await admin_plugins.admin_plugins_mint_token(
                _make_req({"rate_limit_rpm": bad_rpm})
            )
            assert r_rpm.status_code == 400

        for bad_days in (0, 366, False, "forever"):
            r_days = await admin_plugins.admin_plugins_mint_token(
                _make_req({"expires_in_days": bad_days})
            )
            assert r_days.status_code == 400

        # DB failure pops token from MINTED_MCP_TOKENS and returns 500
        before_keys = set(admin_plugins.MINTED_MCP_TOKENS.keys())
        failing_pool = MagicMock()
        failing_pool.acquire = MagicMock(side_effect=RuntimeError("db down"))
        r_db_err = await admin_plugins.admin_plugins_mint_token(
            _make_req(
                {"namespace_id": "11111111-2222-3333-4444-555555555555"}, pg_pool=failing_pool
            )
        )
        assert r_db_err.status_code == 500
        assert set(admin_plugins.MINTED_MCP_TOKENS.keys()) == before_keys


# ---------------------------------------------------------------------------
# S3: Bridge tenant scoping & Redis timeouts
# ---------------------------------------------------------------------------


def test_bridge_stdio_tool_schemas_have_namespace_id() -> None:
    by_name = {t.name: t for t in TOOLS}
    for name in ("list_bridges", "disconnect_bridge", "force_resync_bridge", "bridge_status"):
        assert "namespace_id" in by_name[name].inputSchema["properties"]


@pytest.mark.asyncio
async def test_bridge_handlers_require_namespace_and_use_scoped_session() -> None:
    ns_uuid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.pg_pool.acquire = MagicMock(return_value=acquire_cm)

    with pytest.raises(McpError):
        await bridge_mcp_handlers.list_bridges(engine, {"user_id": "u1"})

    with pytest.raises(McpError):
        await bridge_mcp_handlers.list_bridges(
            engine, {"user_id": "u1", "namespace_id": "bad-uuid"}
        )

    # HTTP session ContextVar fallback works when omitted in arguments
    sess = MCPGatewaySession(
        session_id="s-1",
        namespace_id=str(ns_uuid),
        principal_id="p-1",
        scopes=["tools:read"],
        allowed_engines=["*"],
        tool_exposure_mode="compact",
        expanded_tools=set(),
        rate_limit_rpm=60,
        event_queue=MagicMock(),
    )
    with (
        patch.object(bridge_repo, "list_for_user", new_callable=AsyncMock, return_value=[]) as lf,
        bind_mcp_http_session(sess),
    ):
        raw = await bridge_mcp_handlers.list_bridges(engine, {"user_id": "u1"})
        assert json.loads(raw) == {"bridges": []}
        assert lf.call_args.kwargs["namespace_id"] == ns_uuid
        assert any(
            len(c.args) >= 2
            and "set_config('nce.namespace_id', $1, true)" in str(c.args[0])
            and c.args[1] == str(ns_uuid)
            for c in conn.execute.await_args_list
        )


@pytest.mark.asyncio
async def test_bridge_repo_filters_by_namespace_id() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    ns_id = uuid.uuid4()
    bid = uuid.uuid4()

    await bridge_repo.get_by_id(conn, bid, namespace_id=ns_id)
    sql, *params = conn.fetchrow.call_args.args
    assert "namespace_id = $2::uuid" in sql
    assert params == [bid, ns_id]

    await bridge_repo.list_for_user(conn, "u1", namespace_id=ns_id)
    sql_l, *params_l = conn.fetch.call_args.args
    assert "namespace_id = $2::uuid" in sql_l
    assert params_l == ["u1", ns_id]


def test_bridge_redis_passes_socket_timeouts() -> None:
    with patch.object(bridge_mcp_handlers, "Redis") as mock_redis:
        bridge_mcp_handlers.bridge_redis()
        _, kwargs = mock_redis.from_url.call_args
        assert kwargs["socket_connect_timeout"] == cfg.REDIS_CONNECT_TIMEOUT_S
        assert kwargs["socket_timeout"] == cfg.REDIS_SOCKET_TIMEOUT_S


# ---------------------------------------------------------------------------
# S4: HMACAuthMiddleware rejects replayed signature even with fresh nonce
# ---------------------------------------------------------------------------


def test_hmac_middleware_nonce_bound_burst_and_tamper_protection() -> None:
    """Same-second concurrent identical requests succeed with distinct bound nonces;
    mutated nonces fail signature check; replayed (timestamp, nonce) fails replay check."""
    seen: set[str] = set()
    store = MagicMock(spec=NonceStore)
    store.ping = AsyncMock(return_value=True)

    async def _cas(val: str) -> bool:
        if val in seen:
            return False
        seen.add(val)
        return True

    store.check_and_store = AsyncMock(side_effect=_cas)

    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/api/ping", ok, methods=["GET"])],
        middleware=[
            Middleware(
                HMACAuthMiddleware,
                protected_prefix="/api/",
                api_key="test-key-s4",
                nonce_store=store,
                nonce_required=True,
            )
        ],
    )
    client = TestClient(app, raise_server_exceptions=False)
    ts = int(time.time())

    # 1. 10 identical GET /api/ping requests within the same integer second with distinct nonces -> all 200
    for idx in range(10):
        hdrs_i = _make_hmac_headers(
            "test-key-s4", "GET", "/api/ping", ts=ts, nonce=f"nonce-burst-{idx:06d}"
        )
        assert client.get("/api/ping", headers=hdrs_i).status_code == 200

    # 2. Mutating X-NCE-Nonce on captured signature fails cryptographically (invalid_signature)
    captured = _make_hmac_headers(
        "test-key-s4", "GET", "/api/ping", ts=ts, nonce="nonce-aaaaaaaaaaaa01"
    )
    tampered = {**captured, "X-NCE-Nonce": "nonce-bbbbbbbbbbbb02"}
    r_tampered = client.get("/api/ping", headers=tampered)
    assert r_tampered.status_code == 401
    assert r_tampered.json()["error"]["data"]["reason"] == "invalid_signature"

    # 3. Genuine request succeeds, exact replay of (ts, nonce, sig) is rejected (replay_nonce_conflict)
    assert client.get("/api/ping", headers=captured).status_code == 200
    r_replay = client.get("/api/ping", headers=captured)
    assert r_replay.status_code == 401
    assert r_replay.json()["error"]["data"]["reason"] == "replay_nonce_conflict"


# ---------------------------------------------------------------------------
# S5: MCP HTTP gateway session & auth hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_gateway_session_hardening_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _GATEWAY_SESSIONS.clear()
    _IN_MEMORY_MCP_CLIENT_TOKENS.clear()
    try:
        tok_sha = register_test_mcp_client_token(
            "nce_test_tok_s5",
            namespace_id="00000000-0000-4000-8000-000000000001",
            scopes=["tools:read", "tools:call"],
        )

        # Expired minted/memory token rejected
        register_test_mcp_client_token(
            "nce_test_tok_expired",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        with pytest.raises(ValueError, match="expired"):
            await verify_mcp_token("nce_test_tok_expired")

        redis_store: dict[str, str] = {}

        async def _hset(_k: str, field: str, val: str) -> int:
            redis_store[field] = val
            return 1

        async def _hget(_k: str, field: str) -> str | None:
            return redis_store.get(field)

        async def _hdel(_k: str, field: str) -> int:
            return 1 if redis_store.pop(field, None) is not None else 0

        mock_redis = MagicMock()
        mock_redis.hget = AsyncMock(side_effect=_hget)
        mock_redis.hset = AsyncMock(side_effect=_hset)
        mock_redis.hdel = AsyncMock(side_effect=_hdel)
        mock_redis.expire = AsyncMock(return_value=True)
        engine = MagicMock(redis_client=mock_redis, pg_pool=None)

        gw_app = create_mcp_gateway_app(engine=engine)
        client = TestClient(gw_app, raise_server_exceptions=False)

        from nce import __version__

        hz = client.get("/healthz")
        assert hz.status_code == 200
        assert hz.json()["version"] == __version__

        # Invalid bearer returns generic Unauthorized
        r_bad = client.post(
            "/mcp",
            headers={"Authorization": "Bearer invalid-secret-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert r_bad.status_code == 401
        assert r_bad.json() == {"error": "Unauthorized"}

        # Initialize returns current package version and binds session
        r_init = client.post(
            "/mcp",
            headers={"Authorization": "Bearer nce_test_tok_s5"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert r_init.status_code == 200
        assert r_init.json()["result"]["serverInfo"]["version"] == __version__
        sid = r_init.headers["mcp-session-id"]
        assert _GATEWAY_SESSIONS[sid].token_sha256 == tok_sha
        assert _GATEWAY_SESSIONS[sid].expires_at > time.time()
        mock_redis.expire.assert_awaited()

        # Query-param session_id fallback is rejected
        r_qp = client.post(
            f"/mcp?session_id={sid}",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        assert r_qp.status_code == 401
        assert client.delete(f"/mcp?session_id={sid}").status_code == 404

        # Header Mcp-Session-Id works while valid
        assert (
            client.post(
                "/mcp",
                headers={"Mcp-Session-Id": sid},
                json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
            ).status_code
            == 200
        )

        # Admin tool in MCP_ADMIN_TOOL_NAMES blocked for non-admin session even if spec.admin_only=False
        admin_name = next(iter(MCP_ADMIN_TOOL_NAMES))
        fake_spec = SimpleNamespace(
            engine=None,
            admin_only=False,
            migration=False,
            mutation=False,
            handler=AsyncMock(return_value="ok"),
        )
        with patch("nce.tool_registry.TOOL_REGISTRY", {admin_name: fake_spec}):
            r_adm = client.post(
                "/mcp",
                headers={"Mcp-Session-Id": sid},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": admin_name, "arguments": {}},
                },
            )
            assert r_adm.status_code == 200
            assert r_adm.json()["error"]["code"] == -32005

        # Revoking token invalidates active session on next lookup
        _IN_MEMORY_MCP_CLIENT_TOKENS[tok_sha]["revoked_at"] = time.time()
        r_rev = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": sid},
            json={"jsonrpc": "2.0", "id": 5, "method": "ping"},
        )
        assert r_rev.status_code == 401
        assert sid not in _GATEWAY_SESSIONS
    finally:
        _GATEWAY_SESSIONS.clear()
        _IN_MEMORY_MCP_CLIENT_TOKENS.clear()


def test_gateway_session_bounds_per_token_and_global() -> None:
    _GATEWAY_SESSIONS.clear()
    try:
        for i in range(_MAX_SESSIONS_PER_TOKEN + 5):
            _store_gateway_session(
                MCPGatewaySession(
                    session_id=f"tok-s-{i}",
                    namespace_id="ns",
                    principal_id="p",
                    scopes=[],
                    allowed_engines=["*"],
                    tool_exposure_mode="compact",
                    expanded_tools=set(),
                    rate_limit_rpm=60,
                    event_queue=MagicMock(),
                    token_sha256="same-token",
                    created_at=float(i),
                )
            )
        same_count = sum(1 for s in _GATEWAY_SESSIONS.values() if s.token_sha256 == "same-token")
        assert same_count == _MAX_SESSIONS_PER_TOKEN

        _GATEWAY_SESSIONS.clear()
        for i in range(_MAX_GATEWAY_SESSIONS + 10):
            _store_gateway_session(
                MCPGatewaySession(
                    session_id=f"glob-s-{i}",
                    namespace_id="ns",
                    principal_id="p",
                    scopes=[],
                    allowed_engines=["*"],
                    tool_exposure_mode="compact",
                    expanded_tools=set(),
                    rate_limit_rpm=60,
                    event_queue=MagicMock(),
                    token_sha256=f"tok-{i}",
                    created_at=float(i),
                )
            )
        assert len(_GATEWAY_SESSIONS) == _MAX_GATEWAY_SESSIONS
    finally:
        _GATEWAY_SESSIONS.clear()


# ---------------------------------------------------------------------------
# S7 & P4: Config defaults/prod validation, OIDC, registry, SSRF & webhooks
# ---------------------------------------------------------------------------


def test_config_v350_defaults_and_settings_registry() -> None:
    code = (
        "from nce.config import cfg; "
        "assert cfg.PG_MAX_POOL == 20; "
        "assert cfg.NCE_MCP_SESSION_TTL_S == 3600; "
        "assert cfg.NCE_OIDC_ADMIN_SCOPES_ALLOWLIST == ()"
    )
    env = os.environ.copy()
    env.pop("PG_MAX_POOL", None)
    env.pop("NCE_MCP_SESSION_TTL_S", None)
    env.pop("NCE_OIDC_ADMIN_SCOPES_ALLOWLIST", None)
    env["NCE_ENV"] = "dev"
    env.setdefault("NCE_MASTER_KEY", "x" * 32)
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=_REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr

    assert REGISTRY["NCE_COGNITIVE_BASE_URL"].prod_locked is True


def test_config_validate_rejects_default_minio_and_pg_password_in_prod() -> None:
    with (
        patch.object(_Config, "IS_PROD", True),
        patch.object(_Config, "PG_DSN", "postgresql://mcp_user:mcp_password@db.internal:5432/meta"),
        patch.object(_Config, "MONGO_URI", "mongodb://mongo.internal:27017"),
        patch.object(_Config, "REDIS_URL", "redis://redis.internal:6379/0"),
    ):
        with pytest.raises(RuntimeError, match="PG_DSN"):
            _Config.validate_datastore_config()

    with (
        patch.object(_Config, "IS_PROD", True),
        patch.object(_Config, "MINIO_ACCESS_KEY", "minio-ak"),
        patch.object(_Config, "MINIO_SECRET_KEY", "super_secure_minio_password"),
    ):
        with pytest.raises(ValueError, match="MINIO_SECRET_KEY"):
            _Config.validate_minio_credentials()


@pytest.mark.asyncio
async def test_oidc_jwt_requires_aud_iss_in_prod_and_strips_admin_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user-1",
            "namespace_id": "00000000-0000-4000-8000-000000000001",
            "scopes": ["tools:read", "admin"],
            "aud": "nce-mcp",
            "iss": "https://idp.example.com",
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
    )

    monkeypatch.setattr(cfg, "NCE_JWT_PUBLIC_KEY", pub_pem)
    monkeypatch.setattr(cfg, "NCE_JWT_AUDIENCE", "")
    monkeypatch.setattr(cfg, "NCE_JWT_ISSUER", "")
    monkeypatch.setattr(cfg, "IS_PROD", True)
    with pytest.raises(ValueError, match="NCE_JWT_AUDIENCE"):
        await verify_mcp_token(token)

    monkeypatch.setattr(cfg, "NCE_JWT_AUDIENCE", "nce-mcp")
    monkeypatch.setattr(cfg, "NCE_JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setattr(cfg, "NCE_OIDC_ADMIN_SCOPES_ALLOWLIST", ())
    verified = await verify_mcp_token(token)
    assert verified["scopes"] == ["tools:read"]

    monkeypatch.setattr(cfg, "NCE_OIDC_ADMIN_SCOPES_ALLOWLIST", ("admin",))
    verified_allowed = await verify_mcp_token(token)
    assert "admin" in verified_allowed["scopes"]


def test_resolve_safe_ip_fails_closed_on_gaierror(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_safe_ip.cache_clear()
    try:
        monkeypatch.setattr(
            socket, "getaddrinfo", MagicMock(side_effect=socket.gaierror("dns fail"))
        )
        with pytest.raises(UnsafeURLError, match="DNS resolution failed"):
            _resolve_safe_ip("unresolvable.example.invalid")
    finally:
        _resolve_safe_ip.cache_clear()


def test_outbound_webhook_pins_safe_ip_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_safe_ip.cache_clear()
    try:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured["client_kwargs"] = kwargs

            def __enter__(self) -> _FakeClient:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def post(
                self, url: str, headers: dict[str, str], content: bytes, **kwargs: object
            ) -> MagicMock:
                captured["post_url"] = url
                captured["post_headers"] = headers
                captured["post_kwargs"] = kwargs
                resp = MagicMock()
                resp.status_code = 200
                return resp

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        ok = send_webhook_http_sync(
            "https://hooks.example.com/events",
            "secret-hmac",
            {"hello": "world"},
        )
        assert ok is True
        assert captured["post_url"] == "https://93.184.216.34/events"
        assert captured["post_headers"]["Host"] == "hooks.example.com"  # type: ignore[index]
        t_obj = captured["client_kwargs"]["timeout"]  # type: ignore[index]
        assert isinstance(t_obj, httpx.Timeout)
        assert t_obj.connect == 5.0
        assert t_obj.read == 10.0
    finally:
        _resolve_safe_ip.cache_clear()
