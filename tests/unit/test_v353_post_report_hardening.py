"""Regression tests for v3.5.3 post-report hardening across Core services."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_ready_engine() -> MagicMock:
    pg_conn = MagicMock()
    pg_conn.fetchval = AsyncMock(return_value=1)

    class _AcquireCtx:
        async def __aenter__(self):
            return pg_conn

        async def __aexit__(self, *_args):
            return False

    pg_pool = MagicMock()
    pg_pool.acquire = MagicMock(return_value=_AcquireCtx())

    redis_client = MagicMock()
    redis_client.ping = AsyncMock(return_value=True)

    mongo_admin = MagicMock()
    mongo_admin.command = AsyncMock(return_value={"ok": 1.0})
    mongo_client = MagicMock()
    mongo_client.admin = mongo_admin

    engine = MagicMock()
    engine.pg_pool = pg_pool
    engine.redis_client = redis_client
    engine.mongo_client = mongo_client
    engine.check_health = AsyncMock(side_effect=AssertionError("heavy check_health called"))
    return engine


@pytest.mark.asyncio
async def test_healthz_fast_in_memory_and_zero_db_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/healthz across admin, a2a, and mcp-gateway responds quickly with zero DB I/O."""
    from nce import a2a_server, admin_app, admin_state, mcp_http_gateway

    engine = _make_ready_engine()
    monkeypatch.setattr(admin_state, "engine", engine)
    monkeypatch.setattr(a2a_server, "_engine", engine)

    mcp_app = mcp_http_gateway.create_mcp_gateway_app(engine=engine)
    apps = [
        ("admin", admin_app.app),
        ("a2a", a2a_server.app),
        ("mcp-gateway", mcp_app),
    ]

    for name, app in apps:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Warm route/middleware import path once, then time tight in-memory probe.
            warm = await client.get("/healthz")
            assert warm.status_code == 200, name

            t0 = time.perf_counter()
            for _ in range(10):
                resp = await client.get("/healthz")
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "ok"
                assert body["service"] == name
            elapsed_per_req_ms = ((time.perf_counter() - t0) / 10.0) * 1000.0
            assert elapsed_per_req_ms < 10.0, f"{name} /healthz averaged {elapsed_per_req_ms:.2f}ms"

    assert engine.pg_pool.acquire.call_count == 0
    assert engine.redis_client.ping.call_count == 0
    assert engine.mongo_client.admin.command.call_count == 0
    assert engine.check_health.call_count == 0


@pytest.mark.asyncio
async def test_readyz_singleflight_coalesces_concurrent_probes() -> None:
    """20 concurrent /readyz callers trigger only 1 datastore probe within 5s TTL."""
    from nce.health_probe import SingleflightReadinessCache

    cache = SingleflightReadinessCache(ttl_seconds=5.0)
    calls = 0

    async def _slow_probe():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return ({"status": "ok", "databases": {"postgres": "up", "redis": "up"}}, 200)

    results = await asyncio.gather(*(cache.get_or_probe(_slow_probe) for _ in range(20)))
    assert calls == 1
    assert all(code == 200 and payload["status"] == "ok" for payload, code in results)

    # Cached call hits memory immediately.
    payload, code = await cache.get_or_probe(_slow_probe)
    assert code == 200
    assert payload["status"] == "ok"
    assert calls == 1

    cache.invalidate()
    await cache.get_or_probe(_slow_probe)
    assert calls == 2


@pytest.mark.asyncio
async def test_readyz_routes_return_200_and_cache_across_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify /readyz on admin, a2a, and mcp-gateway reports ok and caches within TTL."""
    from nce import a2a_server, admin_app, admin_state, mcp_http_gateway

    engine = _make_ready_engine()
    monkeypatch.setattr(admin_state, "engine", engine)
    monkeypatch.setattr(a2a_server, "_engine", engine)
    admin_app._ADMIN_READINESS_CACHE.invalidate()
    a2a_server._A2A_READINESS_CACHE.invalidate()

    mcp_app = mcp_http_gateway.create_mcp_gateway_app(engine=engine)
    for name, app in (
        ("admin", admin_app.app),
        ("a2a", a2a_server.app),
        ("mcp-gateway", mcp_app),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r1 = await client.get("/readyz")
            r2 = await client.get("/readyz")
            assert r1.status_code == 200, f"{name}: {r1.text}"
            assert r2.status_code == 200
            assert r1.json()["status"] == "ok"

    assert engine.pg_pool.acquire.call_count == 3
    assert engine.redis_client.ping.call_count == 3


@pytest.mark.asyncio
async def test_cognitive_stub_fails_closed_in_prod_unless_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production startup rejects stub sidecars unless NCE_ALLOW_COGNITIVE_STUB=true."""
    from nce import embeddings
    from nce.config import cfg

    monkeypatch.setattr(cfg, "ENVIRONMENT", "prod")
    monkeypatch.setattr(cfg, "NCE_BACKEND", "")
    monkeypatch.setattr(cfg, "NCE_COGNITIVE_BASE_URL", "http://127.0.0.1:11435")
    monkeypatch.setattr(cfg, "NCE_ALLOW_COGNITIVE_STUB", False)

    stub_resp = SimpleNamespace(
        status_code=200,
        content=b'{"status":"ok","engine":"stub"}',
        json=lambda: {"status": "ok", "engine": "stub"},
    )
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=stub_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(RuntimeError, match="engine='stub'"):
            await embeddings.assert_cognitive_not_stub_in_prod()

    monkeypatch.setattr(cfg, "NCE_ALLOW_COGNITIVE_STUB", True)
    with patch("httpx.AsyncClient", return_value=mock_client):
        payload = await embeddings.assert_cognitive_not_stub_in_prod()
    assert payload == {"status": "ok", "engine": "stub"}

    # Ensure CognitiveRemoteBackend itself instantiates cleanly with _sync_embed_batch.
    with patch(
        "httpx.Client.get",
        return_value=SimpleNamespace(json=lambda: {"engine": "sentence-transformers"}),
    ):
        backend = embeddings.CognitiveRemoteBackend()
        assert callable(getattr(backend, "_sync_embed_batch", None))


def test_generate_internal_mtls_bundle_creates_valid_ca_server_and_client(
    tmp_path: Path,
) -> None:
    """scripts/generate_internal_mtls.py builds valid CA + SAN-pinned server/client certs."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import generate_internal_mtls
    finally:
        sys.path.pop(0)

    out_dir = tmp_path / "mtls"
    paths = generate_internal_mtls.generate_internal_mtls_bundle(out_dir, validity_days=30)
    assert set(paths) == {"ca_cert", "admin_cert", "admin_key", "caddy_cert", "caddy_key"}
    for p in paths.values():
        assert p.is_file()

    ca_cert = x509.load_pem_x509_certificate(paths["ca_cert"].read_bytes())
    admin_cert = x509.load_pem_x509_certificate(paths["admin_cert"].read_bytes())
    caddy_cert = x509.load_pem_x509_certificate(paths["caddy_cert"].read_bytes())

    ca_pub = ca_cert.public_key()
    ca_pub.verify(
        admin_cert.signature,
        admin_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        admin_cert.signature_hash_algorithm,
    )  # type: ignore[arg-type]
    ca_pub.verify(
        caddy_cert.signature,
        caddy_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        caddy_cert.signature_hash_algorithm,
    )  # type: ignore[arg-type]

    admin_sans = {
        str(n.value)
        for n in admin_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    }
    assert {"admin", "localhost", "127.0.0.1"}.issubset(admin_sans)

    caddy_sans = [
        n.value
        for n in caddy_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ]
    assert caddy_sans == ["caddy.internal.nce"]


def test_worker_seccomp_profile_and_compose_planes_and_caddy_headers() -> None:
    """Verify worker OCI seccomp JSON, 3-plane Compose network segmentation, and Caddy XSS header."""
    seccomp = json.loads(
        (REPO_ROOT / "deploy" / "seccomp" / "worker.json").read_text(encoding="utf-8")
    )
    assert seccomp["defaultAction"] == "SCMP_ACT_ALLOW"
    blocked = {name for rule in seccomp["syscalls"] for name in rule["names"]}
    assert {"ptrace", "mount", "bpf", "unshare", "setns", "kexec_load"}.issubset(blocked)

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    nets = compose["networks"]
    assert nets["nce_data"]["internal"] is True
    assert nets["nce_sandbox"]["internal"] is True
    assert not nets["nce_edge"].get("internal", False)

    services = compose["services"]
    assert "seccomp:./deploy/seccomp/worker.json" in services["worker"]["security_opt"]
    assert set(services["worker"]["networks"]) == {"nce_data", "nce_sandbox"}
    assert set(services["caddy"]["networks"]) == {"nce_edge"}
    assert set(services["cognitive"]["networks"]) == {"nce_data"}
    assert services["cognitive"]["build"]["dockerfile"] == "deploy/cognitive-service/Dockerfile"

    ci_stub = yaml.safe_load((REPO_ROOT / "docker-compose.ci-stub.yml").read_text(encoding="utf-8"))
    assert ci_stub["services"]["cognitive"]["build"]["context"] == "./deploy/cognitive-stub"
    assert ci_stub["services"]["admin"]["environment"]["NCE_ALLOW_COGNITIVE_STUB"] == "true"

    mtls_compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.mtls.yml").read_text(encoding="utf-8")
    )
    assert mtls_compose["services"]["admin"]["environment"]["NCE_ADMIN_MTLS_ENABLED"] == "true"
    assert "--ssl-cert-reqs" in mtls_compose["services"]["admin"]["command"]

    caddyfile = (REPO_ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert 'X-XSS-Protection "0"' in caddyfile
    assert "X-XSS-Protection \"1; mode=block\"" not in caddyfile


@pytest.mark.asyncio
async def test_verify_rls_role_capability_flags_unforced_partition() -> None:
    """verify_rls_role_capability catches child partitions lacking FORCE RLS."""
    from nce.event_log import verify_rls_role_capability

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"role_name": "nce_app", "rolsuper": False, "rolbypassrls": False}
    )
    conn.fetch = AsyncMock(
        return_value=[
            {"tablename": "kg_nodes_0", "is_unforced": True, "role_matches": True},
        ]
    )
    findings = await verify_rls_role_capability(conn)
    assert any("kg_nodes_0" in f and "FORCE RLS" in f for f in findings)
