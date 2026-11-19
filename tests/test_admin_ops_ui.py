"""Comprehensive tests for NCE Core v3.7.0 Day-2 Ops Control Plane, Pure-ASGI Dual-Auth & UI."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from nce import admin_ops, admin_plugins, admin_state
from nce.admin_app import build_admin_middleware, create_admin_app
from nce.admin_handlers import tools as admin_tools_mod
from nce.auth import HMACAuthMiddleware, NonceStore, _compute_signature
from nce.config import cfg
from nce.event_log import EXPECTED_TENANT_RLS_TABLES
from nce.mtls import MTLSAuthMiddleware

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeRedis:
    """In-memory async Redis stand-in supporting hashes, strings, sets, and info."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.zsets: dict[str, list[str]] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, val: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = str(val)
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.kv else 0

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                removed += 1
            if k in self.hashes:
                del self.hashes[k]
                removed += 1
        return removed

    async def incr(self, key: str) -> int:
        cur = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(cur)
        return cur

    async def expire(self, key: str, ttl: int) -> bool:
        return key in self.kv

    async def hset(self, key: str, field: str, val: str) -> int:
        h = self.hashes.setdefault(key, {})
        is_new = 0 if field in h else 1
        h[field] = str(val)
        return is_new

    async def hdel(self, key: str, field: str) -> int:
        h = self.hashes.get(key, {})
        if field in h:
            del h[field]
            return 1
        return 0

    async def hkeys(self, key: str) -> list[bytes]:
        return [k.encode("utf-8") for k in self.hashes.get(key, {}).keys()]

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, []))

    async def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    async def scan(self, cursor: int = 0, match: str | None = None, count: int = 100):
        keys = list(self.kv.keys()) + list(self.hashes.keys()) + list(self.lists.keys())
        if match and match.endswith("*"):
            prefix = match[:-1]
            keys = [k for k in keys if k.startswith(prefix)]
        return 0, [k.encode("utf-8") for k in sorted(set(keys))]

    async def info(self, section: str = "memory") -> dict[str, object]:
        return {"used_memory": 2048000, "used_memory_human": "1.95M"}

    def flush(self) -> None:
        self.kv.clear()
        self.hashes.clear()
        self.lists.clear()
        self.sets.clear()
        self.zsets.clear()


class _FakeConn:
    """In-memory asyncpg-like connection backing ops & governance tables."""

    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    async def fetchval(self, sql: str, *args: object) -> object:
        s = " ".join(sql.split()).lower()
        if s.startswith("select 1"):
            return 1
        if "relforcerowsecurity" in s:
            return len(EXPECTED_TENANT_RLS_TABLES)
        return None

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        s = " ".join(sql.split()).lower()
        if "from pg_stat_activity" in s:
            return {"active": 3, "idle_in_tx": 1, "waiting_locks": 0}
        if "update public.ops_quarantine" in s:
            qid = str(args[0])
            new_status = str(args[1])
            for row in self.state["quarantine"]:  # type: ignore[index]
                if str(row["quarantine_id"]) == qid:
                    row["status"] = new_status
                    return {
                        "quarantine_id": qid,
                        "status": new_status,
                        "updated_at": "2026-05-01T00:00:00Z",
                    }
            return None
        if "update mcp_client_tokens" in s:
            tid = str(args[0])
            for row in self.state["tokens"]:  # type: ignore[index]
                if str(row["token_id"]) == tid:
                    row["revoked_at"] = "2026-05-01T01:00:00Z"
                    return {
                        "token_id": tid,
                        "token_sha256": row["token_sha256"],
                        "revoked_at": row["revoked_at"],
                    }
            return None
        return None

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        s = " ".join(sql.split()).lower()
        if "from public.runtime_tool_governance" in s:
            gov: dict[tuple[str, str], dict[str, object]] = self.state["governance"]  # type: ignore[assignment]
            return [
                {"scope_key": k[0], "tool_name": k[1]}
                for k, v in gov.items()
                if not bool(v["enabled"])
            ]
        if "from public.backup_manifests" in s:
            return list(self.state["backups"])  # type: ignore[arg-type]
        if "from public.ops_incidents" in s:
            return list(self.state["incidents"])  # type: ignore[arg-type]
        if "from public.ops_probe_runs" in s:
            return list(self.state["probe_runs"])  # type: ignore[arg-type]
        if "from public.ops_quarantine" in s:
            return list(self.state["quarantine"])  # type: ignore[arg-type]
        if "from public.namespaces" in s or "from namespaces" in s:
            return [
                ns
                for ns in self.state["namespaces"]  # type: ignore[attr-defined]
                if str(ns["slug"]) != "_system"
            ]
        if "from resource_quotas" in s:
            return list(self.state["quotas"])  # type: ignore[arg-type]
        if "from mcp_client_tokens" in s:
            return list(self.state["tokens"])  # type: ignore[arg-type]
        return []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, sql: str, *args: object) -> str:
        s = " ".join(sql.split()).lower()
        if "insert into public.runtime_tool_governance" in s:
            scope_key = str(args[0])
            tool_name = str(args[1])
            enabled = bool(args[2])
            reason = args[3]
            updated_by = str(args[4])
            gov: dict[tuple[str, str], dict[str, object]] = self.state["governance"]  # type: ignore[assignment]
            gov[(scope_key, tool_name)] = {
                "scope_key": scope_key,
                "tool_name": tool_name,
                "enabled": enabled,
                "reason": reason,
                "updated_by": updated_by,
            }
            return "INSERT 0 1"
        if "insert into mcp_client_tokens" in s:
            tokens: list[dict[str, object]] = self.state["tokens"]  # type: ignore[assignment]
            tokens.append(
                {
                    "token_id": str(args[0]),
                    "token_sha256": str(args[1]),
                    "namespace_id": str(args[2]),
                    "principal_id": str(args[3]),
                    "scopes": json.loads(str(args[4])),
                    "allowed_engines": list(args[5]),  # type: ignore[arg-type]
                    "tool_exposure_mode": str(args[6]),
                    "rate_limit_rpm": int(args[7]),  # type: ignore[arg-type]
                    "expires_at": "2026-05-02T00:00:00Z",
                    "revoked_at": None,
                    "created_at": "2026-05-01T00:00:00Z",
                }
            )
            return "INSERT 0 1"
        return "OK"


class _FakePgPool:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None):
        yield _FakeConn(self.state)

    def get_size(self) -> int:
        return 8

    def get_idle_size(self) -> int:
        return 5


@pytest.fixture()
def ops_env(monkeypatch: pytest.MonkeyPatch):
    admin_ops.clear_admin_ops_caches()
    admin_plugins._IN_MEMORY_DISABLED_ENGINES.clear()
    admin_plugins.MINTED_MCP_TOKENS.clear()

    ns_uuid = str(uuid.uuid4())
    sys_uuid = str(uuid.uuid4())
    backup_uuid = str(uuid.uuid4())
    inc_uuid = str(uuid.uuid4())
    q_uuid = str(uuid.uuid4())
    existing_tok_uuid = str(uuid.uuid4())

    db_state: dict[str, object] = {
        "governance": {},
        "backups": [
            {
                "backup_id": backup_uuid,
                "t_cut": "2026-05-01T02:00:00Z",
                "t_blob_completed": "2026-05-01T02:01:00Z",
                "archive_path": "/var/backups/nce/b1.tar.zst",
                "archive_sha256": "a" * 64,
                "size_bytes": 10 * 1024 * 1024,
                "master_key_hmac_fp": "f" * 64,
                "schema_heads": {"alembic": "042"},
                "worm_heads": {"events": "100"},
                "tombstone_count": 3,
                "status": "verified",
                "last_drill_at": "2026-05-01T03:00:00Z",
                "last_drill_status": "passed",
                "last_drill_report": {"tables_ok": True},
                "created_at": "2026-05-01T02:01:05Z",
            }
        ],
        "incidents": [
            {
                "incident_id": inc_uuid,
                "fingerprint": "salience-lag",
                "severity": "warn",
                "status": "open",
                "title": "Salience flush buffer backlog",
                "deterministic_facts": {"salience_buffer_keys": 42, "p95_ms": 18.4},
                "elm_brief_json": {"summary": "Flush buffered keys", "confidence": 0.87},
                "elm_mode_used": "ops-elm-local",
                "recommended_playbook": "flush_salience_buffer",
                "occurrence_count": 2,
                "first_seen_at": "2026-05-01T01:00:00Z",
                "last_seen_at": "2026-05-01T01:30:00Z",
                "resolved_at": None,
            }
        ],
        "probe_runs": [
            {
                "run_id": str(uuid.uuid4()),
                "probe_name": "postgres_connectivity",
                "status": "ok",
                "metrics_json": {"active": 3, "latency_ms": 1.2},
                "created_at": "2026-05-01T01:30:00Z",
            }
        ],
        "quarantine": [
            {
                "quarantine_id": q_uuid,
                "job_fingerprint": "fp-job-42",
                "queue_name": "indexing",
                "func_name": "nce.indexer.process_file",
                "namespace_id": ns_uuid,
                "failure_count": 5,
                "exit_reason": "TimeoutError",
                "sanitized_traceback": "TimeoutError: exceeded deadline",
                "job_args_preview": {"file": "a.py"},
                "status": "quarantined",
                "created_at": "2026-05-01T01:15:00Z",
                "updated_at": "2026-05-01T01:15:00Z",
            }
        ],
        "namespaces": [
            {
                "id": sys_uuid,
                "slug": "_system",
                "parent_id": None,
                "created_at": "2026-05-01T00:00:00Z",
                "embedding_provider": "stub",
                "embedding_model": "stub-384",
                "embedding_dim": 384,
                "is_replay_sandbox": False,
            },
            {
                "id": ns_uuid,
                "slug": "acme-ops",
                "parent_id": None,
                "created_at": "2026-05-01T00:00:00Z",
                "embedding_provider": "stub",
                "embedding_model": "stub-384",
                "embedding_dim": 384,
                "is_replay_sandbox": False,
            },
        ],
        "quotas": [
            {
                "id": str(uuid.uuid4()),
                "namespace_id": ns_uuid,
                "agent_id": None,
                "resource_type": "memories",
                "limit_amount": 10000,
                "used_amount": 420,
                "reset_at": None,
                "updated_at": "2026-05-01T01:00:00Z",
            }
        ],
        "tokens": [
            {
                "token_id": existing_tok_uuid,
                "namespace_id": ns_uuid,
                "token_sha256": hashlib.sha256(b"super-secret-do-not-leak").hexdigest(),
                "principal_id": "ci-bot",
                "scopes": ["memory:read", "graph:read"],
                "allowed_engines": ["*"],
                "tool_exposure_mode": "compact",
                "rate_limit_rpm": 120,
                "expires_at": "2026-05-10T00:00:00Z",
                "revoked_at": None,
                "created_at": "2026-05-01T00:00:00Z",
            }
        ],
    }

    fake_redis = _FakeRedis()
    fake_pg = _FakePgPool(db_state)

    fake_mongo = SimpleNamespace(admin=SimpleNamespace(command=AsyncMock(return_value={"ok": 1})))
    fake_minio = SimpleNamespace(list_buckets=lambda: [SimpleNamespace(name="nce-blobs")])
    fake_cog = SimpleNamespace(
        model_name="stub-hash-384",
        provider_name="stub",
    )

    fake_engine = SimpleNamespace(
        pg_pool=fake_pg,
        redis_client=fake_redis,
        mongo_client=fake_mongo,
        minio_client=fake_minio,
        cognitive=fake_cog,
    )

    monkeypatch.setattr(admin_state, "engine", fake_engine)
    monkeypatch.setattr(cfg, "NCE_API_KEY", "test-ops-api-key-32-chars-minimum")
    monkeypatch.setattr(cfg, "NCE_MASTER_KEY", "test-master-key-for-admin-session")
    monkeypatch.setattr(cfg, "NCE_ADMIN_OVERRIDE", True)
    monkeypatch.setattr(cfg, "IS_PROD", False)
    monkeypatch.setattr(cfg, "NCE_BACKEND", "stub")
    monkeypatch.setenv("NCE_API_KEY", "test-ops-api-key-32-chars-minimum")
    monkeypatch.setenv("NCE_ADMIN_API_KEY", "test-ops-api-key-32-chars-minimum")
    monkeypatch.delenv("NCE_WORKER_DSN", raising=False)

    return SimpleNamespace(
        engine=fake_engine,
        redis=fake_redis,
        pg=fake_pg,
        db_state=db_state,
        ns_uuid=ns_uuid,
        backup_uuid=backup_uuid,
        inc_uuid=inc_uuid,
        q_uuid=q_uuid,
        existing_tok_uuid=existing_tok_uuid,
    )


def _hmac_headers(method: str, path: str, body: bytes, api_key: str, nonce: str) -> dict[str, str]:
    ts = int(time.time())
    sig = _compute_signature(api_key, method, path, ts, body, nonce=nonce)
    return {
        "X-NCE-Timestamp": str(ts),
        "X-NCE-Nonce": nonce,
        "Authorization": f"HMAC-SHA256 {sig}",
    }


def test_middleware_is_pure_asgi_and_preserves_mtls_and_healthz_latency(ops_env):
    """Zero BaseHTTPMiddleware in stack; mTLS prefixes preserved; /healthz < 3.0 ms p50."""
    mws = build_admin_middleware()
    for mw in mws:
        assert not issubclass(mw.cls, BaseHTTPMiddleware)

    mtls_mw = next(m for m in mws if m.cls is MTLSAuthMiddleware)
    assert mtls_mw.kwargs["protected_prefixes"] == ("/api/", "/admin/plugins/api")

    hmac_mw = next(m for m in mws if issubclass(m.cls, HMACAuthMiddleware))
    assert issubclass(hmac_mw.cls, admin_ops.AdminDualAuthMiddleware)

    app = create_admin_app()

    # Pure ASGI fast-path timing (< 3.0 ms p50) with zero DB/Redis activity
    import asyncio

    async def _run_probes() -> list[float]:
        latencies_ms: list[float] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        async def _recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(_msg):
            return None

        for _ in range(40):
            t0 = time.perf_counter()
            await app(scope, _recv, _send)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        return sorted(latencies_ms)

    samples = asyncio.run(_run_probes())
    p50 = samples[len(samples) // 2]
    assert p50 < 3.0, f"/healthz ASGI p50={p50:.3f}ms exceeded 3.0ms"

    client = TestClient(app)
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_ops_api_fail_closed_even_with_admin_override(ops_env, monkeypatch: pytest.MonkeyPatch):
    """Unauthenticated GET /admin/ops/api/overview MUST return 401 regardless of NCE_ADMIN_OVERRIDE."""
    monkeypatch.setattr(cfg, "NCE_ADMIN_OVERRIDE", True)
    app = create_admin_app()
    client = TestClient(app)

    res = client.get("/admin/ops/api/overview")
    assert res.status_code == 401


def test_session_login_logout_rate_limit_entra_and_csrf(ops_env, monkeypatch: pytest.MonkeyPatch):
    """Browser session login/status/logout, HTTPS Secure flag, Redis rate-limit, Entra JWT & CSRF."""
    app = create_admin_app()

    # 1. Invalid key + Redis login rate limit (nce:admin:login_rl:{ip})
    monkeypatch.setenv("NCE_ADMIN_LOGIN_RATE_LIMIT", "2")
    client = TestClient(app)
    assert client.post("/admin/session/login", json={"api_key": "wrong-key"}).status_code == 401
    assert client.post("/admin/session/login", json={"api_key": "wrong-key"}).status_code == 401
    assert client.post("/admin/session/login", json={"api_key": "wrong-key"}).status_code == 429

    # Clear RL for valid login tests
    ops_env.redis.flush()

    # 2. Valid API-key login over HTTPS -> 8h HttpOnly; SameSite=Strict; Path=/admin; Secure
    https_client = TestClient(app, base_url="https://testserver")
    login_res = https_client.post(
        "/admin/session/login",
        json={"api_key": "test-ops-api-key-32-chars-minimum"},
    )
    assert login_res.status_code == 200
    login_data = login_res.json()
    assert login_data["authenticated"] is True
    assert login_data["auth_mode"] == "api_key"
    csrf = login_data["csrf_token"]
    assert csrf

    set_cookies = login_res.headers.get_list("set-cookie")
    sess_hdr = next(h for h in set_cookies if h.startswith("nce_admin_session="))
    csrf_hdr = next(h for h in set_cookies if h.startswith("nce_admin_csrf="))
    assert "HttpOnly" in sess_hdr
    assert "SameSite=Strict" in sess_hdr
    assert "Path=/admin" in sess_hdr
    assert "Secure" in sess_hdr
    assert "HttpOnly" not in csrf_hdr
    assert "SameSite=Strict" in csrf_hdr

    # 3. Session status
    st = https_client.get("/admin/session/status")
    assert st.status_code == 200
    assert st.json()["authenticated"] is True
    assert st.json()["csrf_token"] == csrf

    # 4. Cookie GET works on /admin/ops/api/*
    ov = https_client.get("/admin/ops/api/overview")
    assert ov.status_code == 200

    # 5. CSRF enforcement on POST/PUT/DELETE via session cookie
    assert (
        https_client.post(
            "/admin/ops/api/tokens/mint", json={"scopes": ["memory:read"]}
        ).status_code
        == 403
    )
    assert (
        https_client.post(
            "/admin/ops/api/tokens/mint",
            json={"scopes": ["memory:read"]},
            headers={"X-NCE-CSRF-Token": "mismatch-csrf"},
        ).status_code
        == 403
    )
    assert (
        https_client.post(
            "/admin/plugins/api/toggle",
            json={"slug": "compliance", "enabled": True},
        ).status_code
        == 403
    )

    # Cookie on /api/admin/* requires CSRF too
    saved_cookie = https_client.cookies.get("nce_admin_session", "", path="/admin")
    api_client = TestClient(app, base_url="https://testserver")
    api_client.cookies.set("nce_admin_session", saved_cookie, path="/")
    api_client.cookies.set("nce_admin_csrf", csrf, path="/")
    assert (
        api_client.post(
            "/api/admin/tools/toggle",
            json={"tool_name": "store_memory", "tool_type": "mcp", "enabled": True},
        ).status_code
        == 403
    )
    ok_tool = api_client.post(
        "/api/admin/tools/toggle",
        json={"tool_name": "store_memory", "tool_type": "mcp", "enabled": True},
        headers={"X-NCE-CSRF-Token": csrf},
    )
    assert ok_tool.status_code == 200

    # 6. Logout revokes session JTI & rejects reuse
    logout_res = https_client.post("/admin/session/logout")
    assert logout_res.status_code == 200
    assert logout_res.json()["authenticated"] is False

    reuse_client = TestClient(app)
    reuse_client.cookies.set("nce_admin_session", saved_cookie, path="/admin")
    assert reuse_client.get("/admin/ops/api/overview").status_code == 401

    # 7. Entra OIDC Bearer JWT login path when verifier is configured
    fake_verifier = SimpleNamespace(
        verify_token=AsyncMock(
            return_value=SimpleNamespace(
                upn="ops-lead@example.internal",
                oid="oid-1",
                sub="sub-1",
                roles=("NCE.Admin",),
                scopes=(),
            )
        )
    )
    monkeypatch.setattr(admin_ops, "_ENTRA_VERIFIER", fake_verifier)
    monkeypatch.setenv("NCE_ENTRA_ENFORCE_MODE", "strict")
    entra_res = client.post(
        "/admin/session/login",
        headers={"Authorization": "Bearer hdr.payload.sig"},
    )
    assert entra_res.status_code == 200
    assert entra_res.json()["auth_mode"] == "entra_oidc"
    assert entra_res.json()["principal"] == "ops-lead@example.internal"


def test_hmac_three_header_and_replay_rejection_across_surfaces(
    ops_env, monkeypatch: pytest.MonkeyPatch
):
    """3-header HMAC (Authorization & X-NCE-Signature) + Redis/memory nonce replay returns 401."""
    from nce import admin_app as admin_app_mod

    nonce_store = NonceStore(ops_env.redis)
    monkeypatch.setattr(admin_app_mod, "_hmac_nonce_store", nonce_store)
    app = create_admin_app()
    client = TestClient(app)
    api_key = "test-ops-api-key-32-chars-minimum"

    # A. Authorization: HMAC-SHA256 on /admin/ops/api/overview + replay 401
    hdrs1 = _hmac_headers("GET", "/admin/ops/api/overview", b"", api_key, "nonce-ops-00000001")
    assert client.get("/admin/ops/api/overview", headers=hdrs1).status_code == 200
    assert client.get("/admin/ops/api/overview", headers=hdrs1).status_code == 401

    # B. X-NCE-Signature header on /admin/ops/api/overview + replay 401
    ts = int(time.time())
    sig = _compute_signature(
        api_key, "GET", "/admin/ops/api/overview", ts, b"", nonce="nonce-xsig-0000002"
    )
    xhdrs = {
        "X-NCE-Timestamp": str(ts),
        "X-NCE-Nonce": "nonce-xsig-0000002",
        "X-NCE-Signature": sig,
    }
    assert client.get("/admin/ops/api/overview", headers=xhdrs).status_code == 200
    assert client.get("/admin/ops/api/overview", headers=xhdrs).status_code == 401

    # C. HMAC on /admin/plugins/api + replay 401
    phdrs = _hmac_headers("GET", "/admin/plugins/api", b"", api_key, "nonce-plug-0000003")
    assert client.get("/admin/plugins/api", headers=phdrs).status_code == 200
    assert client.get("/admin/plugins/api", headers=phdrs).status_code == 401

    # D. HMAC on /api/admin/tools + replay 401
    thdrs = _hmac_headers("GET", "/api/admin/tools", b"", api_key, "nonce-api-00000004")
    assert client.get("/api/admin/tools", headers=thdrs).status_code == 200
    assert client.get("/api/admin/tools", headers=thdrs).status_code == 401

    # E. In-memory replay guard even when nonce_store is None in non-prod
    monkeypatch.setattr(admin_app_mod, "_hmac_nonce_store", None)
    mem_hdrs = _hmac_headers("GET", "/admin/ops/api/overview", b"", api_key, "nonce-mem-00000005")
    assert client.get("/admin/ops/api/overview", headers=mem_hdrs).status_code == 200
    assert client.get("/admin/ops/api/overview", headers=mem_hdrs).status_code == 401


def test_day2_ops_rest_endpoints_end_to_end(ops_env, monkeypatch: pytest.MonkeyPatch):
    """Overview (stub warning, RLS, 4-stores), Backups/Drills, Incidents/Playbooks/Quarantine, Tenants/Tokens."""
    app = create_admin_app()
    client = TestClient(app)

    login = client.post(
        "/admin/session/login", json={"api_key": "test-ops-api-key-32-chars-minimum"}
    )
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]
    mut_hdrs = {"X-NCE-CSRF-Token": csrf}

    # 1. Overview
    ov = client.get("/admin/ops/api/overview").json()
    assert set(ov["stores"].keys()) == {"postgres", "mongodb", "redis", "minio"}
    assert ov["stores"]["postgres"]["status"] == "up"
    assert ov["rls"]["expected_forced_tables"] == len(EXPECTED_TENANT_RLS_TABLES)
    assert ov["rls"]["verified_forced_tables"] == len(EXPECTED_TENANT_RLS_TABLES)
    assert ov["cognitive"]["embedding_provider"] == "stub"
    assert ov["cognitive"]["stub_warning"] is True
    assert "ops_elm" in ov
    assert ov["disk"]["free_gib"] >= 0

    # 2. Backups list + trigger + ephemeral drill
    bk = client.get("/admin/ops/api/backups").json()
    assert len(bk["manifests"]) == 1
    assert bk["latest_tombstone_count"] == 3
    assert "preflight_ok" in bk["disk_preflight"]

    monkeypatch.setattr(
        admin_ops,
        "create_watermark_backup",
        AsyncMock(return_value={"backup_id": ops_env.backup_uuid, "status": "verified"}),
    )
    trig = client.post("/admin/ops/api/backups/trigger", json={}, headers=mut_hdrs)
    assert trig.status_code == 200
    assert trig.json()["manifest"]["status"] == "verified"

    monkeypatch.setattr(
        admin_ops,
        "run_ephemeral_restore_drill",
        AsyncMock(return_value={"backup_id": ops_env.backup_uuid, "status": "passed"}),
    )
    drill = client.post(
        f"/admin/ops/api/backups/{ops_env.backup_uuid}/drill",
        json={},
        headers=mut_hdrs,
    )
    assert drill.status_code == 200
    assert drill.json()["drill_report"]["status"] == "passed"

    # 3. Incidents + Quarantine + 2-Step Playbook Preview -> Execute
    inc = client.get("/admin/ops/api/incidents").json()
    assert inc["incidents"][0]["deterministic_facts"]["salience_buffer_keys"] == 42
    assert inc["incidents"][0]["elm_brief_json"]["confidence"] == 0.87
    assert inc["incidents"][0]["elm_mode_used"] == "ops-elm-local"
    assert len(inc["playbooks"]) == 5
    assert len(inc["probe_runs"]) == 1

    q = client.get("/admin/ops/api/quarantine").json()
    assert len(q["items"]) == 1
    req = client.post(
        f"/admin/ops/api/quarantine/{ops_env.q_uuid}/requeue",
        json={},
        headers=mut_hdrs,
    )
    assert req.status_code == 200
    assert req.json()["new_status"] == "requeued"
    dis = client.post(
        f"/admin/ops/api/quarantine/{ops_env.q_uuid}/dismiss",
        json={},
        headers=mut_hdrs,
    )
    assert dis.status_code == 200
    assert dis.json()["new_status"] == "dismissed"

    monkeypatch.setattr(
        admin_ops,
        "preview_playbook",
        AsyncMock(
            return_value={
                "playbook_id": "flush_salience_buffer",
                "dry_run": True,
                "confirmation_token": "tok-confirm-123",
                "estimated_rows": 42,
            }
        ),
    )
    monkeypatch.setattr(
        admin_ops,
        "execute_playbook",
        AsyncMock(return_value={"playbook_id": "flush_salience_buffer", "status": "completed"}),
    )

    prev = client.post(
        "/admin/ops/api/playbooks/flush_salience_buffer/preview",
        json={"incident_id": ops_env.inc_uuid},
        headers=mut_hdrs,
    )
    assert prev.status_code == 200
    tok = prev.json()["confirmation_token"]
    assert tok == "tok-confirm-123"

    # Execute without confirmation_token -> 400
    bad_exec = client.post(
        "/admin/ops/api/playbooks/flush_salience_buffer/execute",
        json={},
        headers=mut_hdrs,
    )
    assert bad_exec.status_code == 400

    ok_exec = client.post(
        "/admin/ops/api/playbooks/flush_salience_buffer/execute",
        json={"confirmation_token": tok, "reason": "verified drain"},
        headers=mut_hdrs,
    )
    assert ok_exec.status_code == 200
    assert ok_exec.json()["execution"]["status"] == "completed"

    # 4. Tenants (excludes _system & hides token hashes) + Mint (4 scopes + compact/full) + Revoke
    ten = client.get("/admin/ops/api/tenants").json()
    slugs = [n["slug"] for n in ten["namespaces"]]
    assert "_system" not in slugs
    assert "acme-ops" in slugs
    for t in ten["tokens"]:
        assert "token_sha256" not in t
        assert t["token_prefix"].startswith("nce_mcp_")

    mint_res = client.post(
        "/admin/ops/api/tokens/mint",
        json={
            "namespace_id": ops_env.ns_uuid,
            "principal_id": "ops-agent",
            "scopes": ["memory:read", "memory:write", "graph:read", "admin"],
            "tool_exposure_mode": "full",
            "ttl_hours": 12,
        },
        headers=mut_hdrs,
    )
    assert mint_res.status_code == 201
    minted = mint_res.json()
    assert minted["tool_exposure_mode"] == "full"
    assert set(minted["scopes"]) == {"memory:read", "memory:write", "graph:read", "admin"}
    assert minted["mcp_url"] == "https://localhost:8443/mcp"
    assert ":8443/mcp" in minted["mcp_config_json"]
    minted_id = minted["token_id"]

    rev_ok = client.post(
        f"/admin/ops/api/tokens/{minted_id}/revoke",
        json={"namespace_id": ops_env.ns_uuid},
        headers=mut_hdrs,
    )
    assert rev_ok.status_code == 200
    assert rev_ok.json()["revoked"] is True

    rev_missing = client.post(
        f"/admin/ops/api/tokens/{uuid.uuid4()}/revoke",
        json={},
        headers=mut_hdrs,
    )
    assert rev_missing.status_code == 404


def test_durable_governance_pg_write_through_and_redis_flush_hydration(
    ops_env, monkeypatch: pytest.MonkeyPatch
):
    """Engine and Core tool toggles persist to PG runtime_tool_governance and re-hydrate on Redis flush."""
    dummy_plugin = SimpleNamespace(
        plugin_id="ops_test_engines",
        get_engine_descriptors=lambda: [
            {
                "slug": "compliance",
                "id": "compliance",
                "name": "Compliance Engine",
                "description": "Audit and compliance rule evaluator.",
                "version": "1.0.0",
                "types": ["engine"],
                "categories": ["governance"],
                "installed": True,
                "enabled_by_env": True,
                "enabled": True,
                "maturity": "beta",
            }
        ],
    )
    monkeypatch.setattr("nce.plugins.load_plugins", lambda: {"ops_test_engines": dummy_plugin})

    app = create_admin_app()
    client = TestClient(app)

    login = client.post(
        "/admin/session/login", json={"api_key": "test-ops-api-key-32-chars-minimum"}
    )
    csrf = login.json()["csrf_token"]
    hdrs = {"X-NCE-CSRF-Token": csrf}

    # Toggle engine off -> persists to PG + Redis
    r_eng = client.post(
        "/admin/plugins/api/toggle",
        json={"slug": "compliance", "enabled": False},
        headers=hdrs,
    )
    assert r_eng.status_code == 200
    assert ops_env.db_state["governance"][("engine", "compliance")]["enabled"] is False
    assert ops_env.redis.kv.get(admin_ops.GOVERNANCE_CACHE_HYDRATED_KEY) == "1"

    # Toggle core MCP tool off -> persists to PG + Redis
    r_tool = client.post(
        "/admin/ops/api/tools/toggle",
        json={"tool_name": "store_memory", "tool_type": "mcp", "enabled": False},
        headers=hdrs,
    )
    assert r_tool.status_code == 200
    assert ops_env.db_state["governance"][("tool", "store_memory")]["enabled"] is False

    # Simulate Redis flush (wipe hashes & sentinel)
    ops_env.redis.flush()
    assert ops_env.redis.hashes == {}

    # Querying plugins API & tools API MUST auto-hydrate Redis from PG!
    plug_after = client.get("/admin/plugins/api").json()
    comp_entry = next(e for e in plug_after["engines"] if e["slug"] == "compliance")
    assert comp_entry["runtime_disabled"] is True
    assert ops_env.redis.kv.get(admin_ops.GOVERNANCE_CACHE_HYDRATED_KEY) == "1"

    tools_after = client.get("/admin/ops/api/tools").json()
    store_mem = next(t for t in tools_after["mcp_tools"] if t["name"] == "store_memory")
    assert store_mem["enabled"] is False

    # Re-test module import compatibility for existing tools module
    assert callable(admin_tools_mod.api_admin_tools_toggle)


def test_unified_console_routes_airgap_dom_xss_and_brand_wall(ops_env):
    """Verify /admin & /admin/plugins UI shells, content negotiation, air-gap, DOM-XSS safety & brand wall."""
    app = create_admin_app()
    client = TestClient(app)

    # /admin serves HTML without BasicAuth challenge (prompts login modal cleanly)
    r_admin = client.get("/admin")
    assert r_admin.status_code == 200
    assert 'data-initial-tab="overview"' in r_admin.text
    assert 'id="login-modal"' in r_admin.text
    assert 'data-tab="ops"' in r_admin.text
    assert 'data-tab="tenants"' in r_admin.text

    assert client.get("/admin/console.js").status_code == 200
    assert client.get("/admin/console.css").status_code == 200

    # /admin/plugins stays 401 when unauthenticated
    assert client.get("/admin/plugins").status_code == 401

    # Authenticate via session cookie -> /admin/plugins loads Tab 5 console; Accept JSON returns JSON
    client.post("/admin/session/login", json={"api_key": "test-ops-api-key-32-chars-minimum"})
    r_plug_html = client.get("/admin/plugins")
    assert r_plug_html.status_code == 200
    assert 'data-initial-tab="plugins"' in r_plug_html.text
    assert 'id="maturity-segmented"' in r_plug_html.text
    assert 'data-maturity="beta"' in r_plug_html.text
    assert ".badge-maturity-beta" in r_plug_html.text

    r_plug_json = client.get("/admin/plugins", headers={"Accept": "application/json"})
    assert r_plug_json.status_code == 200
    assert "engines" in r_plug_json.json()

    # Air-gap + DOM XSS checks across entire admin/ directory
    admin_dir = REPO_ROOT / "admin"
    url_re = re.compile(r"https?://", re.IGNORECASE)
    xss_re = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b")

    for asset in admin_dir.iterdir():
        if not asset.is_file():
            continue
        content = asset.read_text(encoding="utf-8", errors="ignore")
        assert not url_re.search(content), f"External URL found in {asset.name}"
        assert not xss_re.search(content), f"Unsafe DOM sink found in {asset.name}"

    # Brand-wall + local file-URI hygiene across all Worker-3 owned files
    forbidden_terms = [
        "".join(["Glam", "ox"]),
        "".join(["Ver", "tex"]),
        "".join(["E", "2D"]),
    ]
    brand_re = re.compile("|".join(forbidden_terms), re.IGNORECASE)
    owned_files = [
        REPO_ROOT / "admin_server.py",
        REPO_ROOT / "nce" / "admin_app.py",
        REPO_ROOT / "nce" / "admin_plugins.py",
        REPO_ROOT / "nce" / "admin_ops.py",
        REPO_ROOT / "nce" / "admin_handlers" / "tools.py",
        REPO_ROOT / "tests" / "test_admin_ops_ui.py",
        *[p for p in admin_dir.iterdir() if p.is_file()],
    ]
    for fp in owned_files:
        txt = fp.read_text(encoding="utf-8", errors="ignore")
        assert not brand_re.search(txt), f"Brand-wall term found in {fp}"
        assert ("file:" + "///") not in txt, f"Local file URI found in {fp}"
