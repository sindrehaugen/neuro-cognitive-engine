"""Wave 4 Security Verification Suite for v3.3.0 Zero-Trust Upgrade.

Covers:
1. /mcp session tenant isolation & cross-tenant spoofing prevention
2. Compact discover_tools progressive routing & allowed_engines enforcement
3. DNS-rebinding Origin rejection on /mcp
4. Zero secrets in mcp_config.json.example, nce-mcp-proxy, and production stdio block
5. KEK/DEK envelope encryption, key rotation, cache eviction & GDPR crypto-shredding vs WORM JCS receipt
6. scoped_pg_session / set_namespace_context active SQL transaction (BEGIN) assertion
7. Quad-Store isolation (Redis nce:ns:*, MongoDB $where block, MinIO write-once SHA-256, salience buffer as_of/replay skip, Priority RQ)
8. replay_fork outbox & vertical connector fencing (is_replay_sandbox == true)
9. RS256 -> HS256 JWT algorithm confusion rejection & production HMAC nonce fail-closed
10. Secret-free parser subprocesses, zip-bomb budget, air-gapped embeddings, tiebreaker circuit breaker, and untrusted provenance @governed guard
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from nce.autonomy.governor import governed
from nce.contradictions import check_llm_tiebreaker_budget
from nce.crypto.envelope import (
    EnvelopeCryptoManager,
    InMemoryDEKStore,
    KEKRing,
    ShreddedKeyError,
    canonicalize_jcs,
    crypto_shred_subject,
)
from nce.db_utils import (
    ScopedMongoCollection,
    ScopedRedisClient,
    set_namespace_context,
    validate_redis_namespace_key,
)
from nce.embeddings import (
    EmbeddingDimensionMismatchError,
    verify_namespace_embedding_pin,
)
from nce.extractors.sandbox import (
    build_sanitized_subprocess_env,
    enforce_extraction_budget,
    run_sandboxed_extractor,
)
from nce.jwt_auth import JWTAlgorithmConfusionError, verify_jwt_isolated
from nce.mcp_client_proxy import validate_proxy_env
from nce.mcp_http_gateway import (
    create_mcp_gateway_app,
    is_engine_allowed_for_session,
    register_test_mcp_client_token,
    validate_origin,
)
from nce.outbox_relay import is_namespace_replay_sandbox
from nce.salience_buffer import buffer_salience_boost
from nce.storage import build_write_once_object_key, put_write_once_object

_VERT_ROOT = Path(__file__).resolve().parents[3] / "nce-vertical-engines"


def test_mcp_gateway_origin_rebinding_defense() -> None:
    """Verify strict Origin allowlist rejects DNS-rebinding origins with HTTP 403."""
    assert validate_origin("https://localhost:8443", {"https://localhost:8443"}) is True
    assert validate_origin("http://evil.attacker.com", {"https://localhost:8443"}) is False

    app = create_mcp_gateway_app()
    client = TestClient(app)
    token = "nce_mcp_test_origin_token"
    register_test_mcp_client_token(token)

    resp_bad = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://dns-rebind.evil.local",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp_bad.status_code == 403

    resp_ok = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://localhost:8443",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp_ok.status_code == 200
    assert "Mcp-Session-Id" in resp_ok.headers


def test_mcp_gateway_compact_discover_tools_and_tenant_isolation() -> None:
    """Verify compact progressive tool routing, engine scoping, and tenant isolation."""
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    token_a = "nce_mcp_tenant_a_scoped"
    register_test_mcp_client_token(
        token_a,
        namespace_id=tenant_a,
        principal_id="agent-a",
        allowed_engines=["type:erp-crm"],
        tool_exposure_mode="compact",
    )

    mock_engine = MagicMock()
    mock_engine.redis_client = None
    app = create_mcp_gateway_app(engine=mock_engine)
    client = TestClient(app)

    # 1. tools/list in compact mode includes discover_tools
    r_list = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r_list.status_code == 200
    session_id = r_list.headers["Mcp-Session-Id"]
    tools = r_list.json()["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert "discover_tools" in tool_names

    # 2. Call discover_tools with query/type_filter
    r_disc = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token_a}",
            "Mcp-Session-Id": session_id,
        },
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "discover_tools",
                "arguments": {"type_filter": "erp-crm"},
            },
        },
    )
    assert r_disc.status_code == 200
    disc_res = r_disc.json()["result"]
    assert disc_res.get("isError") is not True

    # 3. Verify dual-axis engine scope evaluation
    sample_descriptors = [
        {"slug": "odoo", "types": ["erp-crm"], "categories": ["commercial", "enterprise-erp"]},
        {"slug": "netbox", "types": ["infra-iot"], "categories": ["av-it-integrator"]},
    ]
    assert is_engine_allowed_for_session("odoo", ["type:erp-crm"], sample_descriptors) is True
    assert is_engine_allowed_for_session("netbox", ["type:erp-crm"], sample_descriptors) is False
    assert is_engine_allowed_for_session("odoo", ["cat:commercial"], sample_descriptors) is True

    # 4. Cross-tenant spoofing check: attempting to use session_id with a different tenant token fails
    token_b = "nce_mcp_tenant_b_scoped"
    register_test_mcp_client_token(token_b, namespace_id=tenant_b, principal_id="agent-b")
    r_spoof = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token_b}",
            "Mcp-Session-Id": session_id,
        },
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    assert r_spoof.status_code in (403, 404)

    # 5. DELETE /mcp revokes session
    r_del = client.delete(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token_a}",
            "Mcp-Session-Id": session_id,
        },
    )
    assert r_del.status_code in (200, 204)


def test_zero_secrets_in_mcp_config_and_proxy_and_prod_stdio_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mcp_config.json.example has zero DB/master secrets and server.py blocks prod stdio."""
    repo_root = Path(__file__).resolve().parents[2]
    cfg_text = (repo_root / "mcp_config.json.example").read_text(encoding="utf-8")
    parsed = json.loads(cfg_text)
    assert "mcpServers" in parsed
    for forbidden in ("NCE_MASTER_KEY", "PG_DSN", "DATABASE_URL", "MONGO_URI", "MINIO_SECRET_KEY"):
        assert forbidden not in cfg_text, (
            f"Forbidden secret {forbidden} found in mcp_config.json.example"
        )

    # Proxy env validator rejects backend secrets
    with pytest.raises(RuntimeError, match="NCE_MASTER_KEY"):
        validate_proxy_env(
            {"NCE_MCP_HTTP_URL": "https://localhost:8443/mcp", "NCE_MASTER_KEY": "secret"}
        )

    with pytest.raises(RuntimeError, match="PG_DSN"):
        validate_proxy_env(
            {"NCE_MCP_HTTP_URL": "https://localhost:8443/mcp", "PG_DSN": "postgresql://..."}
        )

    # Production local stdio guard in server.py
    import server

    monkeypatch.setenv("NCE_ENV", "production")
    monkeypatch.setenv("NCE_ALLOW_LOCAL_STDIO", "false")
    with pytest.raises(RuntimeError, match="disabled in production"):
        server.assert_local_stdio_allowed()


@pytest.mark.asyncio
async def test_kek_dek_envelope_crypto_and_gdpr_shredding() -> None:
    """Verify KEK/DEK envelope encryption, rotation, and GDPR crypto-shredding with JCS receipt."""
    kek_ring = KEKRing(master_key=b"M" * 32, active_version="kek-v1")
    store = InMemoryDEKStore()
    mgr = EnvelopeCryptoManager(kek_ring=kek_ring)
    ns_id = str(uuid.uuid4())
    subject = "user:gdpr-subject-42@example.com"

    # Encrypt payload under subject DEK
    env = await mgr.encrypt_payload(
        store,
        ns_id,
        b"sensitive-pii-record",
        subject_ref=subject,
        aad=b"context-1",
    )
    assert env["v"] == 2
    assert env["alg"] == "AES-256-GCM"

    # Decrypt succeeds prior to shredding
    decrypted = await mgr.decrypt_payload(store, ns_id, env, aad=b"context-1")
    assert decrypted == b"sensitive-pii-record"

    # Rotate KEK and verify rewrap works
    kek_ring.rotate_kek("kek-v2", b"N" * 32)
    dek_rec = await mgr.get_or_create_dek(store, ns_id, subject_ref=subject)
    rewrapped = kek_ring.rewrap_dek(dek_rec.wrapped_key, "kek-v1", "kek-v2")
    assert len(rewrapped) > 0

    # GDPR crypto-shred subject
    receipt = await crypto_shred_subject(
        store,
        ns_id,
        subject,
        manager=mgr,
    )
    assert receipt["action"] == "gdpr_crypto_shred"
    assert receipt["dek_count"] == 1
    assert subject not in json.dumps(receipt), (
        "Raw PII subject_ref must never appear in WORM receipt"
    )
    # Verify RFC 8785 JCS canonicalization is deterministic
    jcs_bytes = canonicalize_jcs({"b": 2, "a": 1})
    assert jcs_bytes == b'{"a":1,"b":2}'

    # Subsequent decrypt raises ShreddedKeyError
    with pytest.raises(ShreddedKeyError):
        await mgr.decrypt_payload(store, ns_id, env, aad=b"context-1")


@pytest.mark.asyncio
async def test_scoped_pg_session_transaction_guard_and_quad_store_isolation() -> None:
    """Verify set_namespace_context requires BEGIN transaction and Quad-Store enforces tenant boundaries."""
    ns_a = str(uuid.uuid4())
    ns_b = str(uuid.uuid4())

    # 1. Postgres autocommit connection (is_in_transaction() is False) is rejected
    unscoped_conn = MagicMock()
    unscoped_conn.is_in_transaction.return_value = False
    unscoped_conn.execute = AsyncMock()
    with pytest.raises(RuntimeError, match="requires an active SQL transaction"):
        await set_namespace_context(unscoped_conn, ns_a)

    # 2. Redis key validation enforces nce:ns:{namespace_id}:* and blocks cross-tenant access
    valid_key = validate_redis_namespace_key(ns_a, "cache:item1")
    assert valid_key == f"nce:ns:{ns_a}:cache:item1"
    with pytest.raises(ValueError, match="Cross-tenant"):
        validate_redis_namespace_key(ns_a, f"nce:ns:{ns_b}:secret")
    with pytest.raises(ValueError, match="wildcard"):
        validate_redis_namespace_key(ns_a, "nce:ns:*")

    mock_redis = AsyncMock()
    scoped_redis = ScopedRedisClient(mock_redis, ns_a)
    await scoped_redis.get("mykey")
    mock_redis.get.assert_awaited_once_with(f"nce:ns:{ns_a}:mykey")

    # 3. MongoDB ScopedMongoCollection blocks $where and cross-tenant filters
    raw_coll = MagicMock()
    scoped_mongo = ScopedMongoCollection(raw_coll, ns_a)
    with pytest.raises(ValueError, match=r"\$where"):
        scoped_mongo._inject_filter({"$where": "this.a == 1"})

    # 4. MinIO write-once content-addressed key & idempotent put
    obj_key = build_write_once_object_key(ns_a, "evidence", b"immutable-payload")
    assert obj_key.startswith(f"{ns_a}/evidence/")
    with pytest.raises(ValueError):
        build_write_once_object_key(ns_a, "../traversal", b"bad")

    minio_mock = MagicMock()
    minio_mock.stat_object.return_value = MagicMock()  # already exists
    returned_key = put_write_once_object(
        minio_mock, "nce-bucket", ns_a, "evidence", b"immutable-payload"
    )
    assert returned_key == obj_key
    minio_mock.put_object.assert_not_called()

    # 5. Salience buffer skips as_of and is_replay reads
    redis_salience = AsyncMock()
    assert (
        await buffer_salience_boost(
            redis_salience, ns_a, uuid.uuid4(), as_of="2026-01-01T00:00:00Z"
        )
        is False
    )
    assert await buffer_salience_boost(redis_salience, ns_a, uuid.uuid4(), is_replay=True) is False
    redis_salience.hincrbyfloat.assert_not_called()

    # 6. Priority RQ queues in start_worker
    import start_worker

    assert start_worker.PRIORITY_QUEUES[:3] == ["nce-critical", "nce-default", "nce-bulk"]


@pytest.mark.asyncio
async def test_jwt_algorithm_confusion_and_sandbox_and_replay_fencing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify RS256->HS256 confusion rejection, secret-free parser sandbox, and replay outbox fencing."""
    import base64
    import hashlib
    import hmac

    # 1. Generate RSA keypair and attempt HS256 algorithm confusion with public key PEM
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    hdr_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode("ascii")
    pay_b64 = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "attacker", "namespace_id": str(uuid.uuid4())}).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signing_input = f"{hdr_b64}.{pay_b64}".encode("ascii")
    sig_b64 = (
        base64.urlsafe_b64encode(hmac.new(pub_pem, signing_input, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    forged_hs256 = f"{hdr_b64}.{pay_b64}.{sig_b64}"
    with pytest.raises(JWTAlgorithmConfusionError):
        verify_jwt_isolated(forged_hs256, key=pub_pem, asymmetric_only=True)

    # 2. Secret-free subprocess environment for document parsers
    monkeypatch.setenv("NCE_MASTER_KEY", "top-secret-master-key-do-not-leak")
    monkeypatch.setenv("PG_DSN", "postgresql://secret:secret@db/prod")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-key")
    clean_env = build_sanitized_subprocess_env()
    assert "NCE_MASTER_KEY" not in clean_env
    assert "PG_DSN" not in clean_env
    assert "OPENAI_API_KEY" not in clean_env

    proc = run_sandboxed_extractor(["/usr/bin/env"])
    stdout_str = (
        proc.stdout.decode("utf-8", errors="replace")
        if isinstance(proc.stdout, bytes)
        else str(proc.stdout)
    )
    assert "top-secret-master-key-do-not-leak" not in stdout_str
    assert "postgresql://secret" not in stdout_str

    # 3. Zip-bomb extraction budget guard
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.txt", b"0" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="expansion ratio"):
        enforce_extraction_budget(buf.getvalue(), max_expansion_ratio=5.0)

    # 4. Per-namespace embedding dimension pinning
    with pytest.raises(EmbeddingDimensionMismatchError):
        verify_namespace_embedding_pin(
            uuid.uuid4(),
            provider="local",
            model="bge-m3",
            dim=1024,
            pinned_provider="local",
            pinned_model="bge-base",
            pinned_dim=768,
        )

    # 5. Per-tenant LLM contradiction tiebreaker circuit breaker
    ns_burst = str(uuid.uuid4())
    assert check_llm_tiebreaker_budget(ns_burst, max_calls_per_minute=2) is True
    assert check_llm_tiebreaker_budget(ns_burst, max_calls_per_minute=2) is True
    assert check_llm_tiebreaker_budget(ns_burst, max_calls_per_minute=2) is False

    # 6. Untrusted provenance guard on @governed
    @governed(action_type="external_mutation")
    async def _mut_tool(
        conn: object,
        namespace_id: uuid.UUID,
        arguments: dict,
        *,
        idempotency_key: str = "",
        confirm: bool = False,
    ) -> str:
        return "executed"

    gov_conn = MagicMock()
    gov_conn.is_in_transaction = MagicMock(return_value=True)
    gov_conn.fetchrow = AsyncMock(side_effect=[None, {"id": uuid.uuid4()}])

    res_obj = await _mut_tool(
        gov_conn,
        uuid.uuid4(),
        {
            "confirm": True,
            "idempotency_key": "idem-1",
            "provenance_tier": "external_untrusted",
        },
        idempotency_key="idem-1",
        confirm=True,
    )
    assert res_obj["status"] == "pending_approval"

    # 7. Replay sandbox outbox & vertical connector fencing
    monkeypatch.setenv("NCE_REPLAY_SANDBOX", "true")
    assert await is_namespace_replay_sandbox(None, uuid.uuid4()) is True

    import os
    import sys

    vert_root = _VERT_ROOT
    added_path = False
    if await asyncio.to_thread(os.path.isdir, str(vert_root)) and str(vert_root) not in sys.path:
        sys.path.insert(0, str(vert_root))
        added_path = True
    try:
        from nce_verticals.replay_fence import ReplaySandboxFenceError, assert_not_replay_sandbox

        with pytest.raises(ReplaySandboxFenceError):
            await assert_not_replay_sandbox(is_replay_sandbox=True)
    except ImportError:
        pass
    finally:
        if added_path and str(vert_root) in sys.path:
            sys.path.remove(str(vert_root))
        for mod_name in list(sys.modules):
            if mod_name == "nce_verticals" or mod_name.startswith("nce_verticals."):
                sys.modules.pop(mod_name, None)


def test_admin_plugins_mint_token_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify POST /admin/plugins/api/mint-token mints a scoped token usable on /mcp."""
    from starlette.applications import Starlette

    from nce.admin_plugins import register_admin_plugin_routes

    monkeypatch.setenv("NCE_ADMIN_API_KEY", "test-admin-key-v33")
    admin_app = Starlette(routes=register_admin_plugin_routes())
    admin_client = TestClient(admin_app)

    mint_resp = admin_client.post(
        "/admin/plugins/api/mint-token",
        headers={"X-Admin-Api-Key": "test-admin-key-v33"},
        json={
            "namespace_id": "00000000-0000-4000-8000-000000000099",
            "principal_id": "ci-verify",
            "types": ["erp-crm"],
            "categories": ["commercial"],
            "tool_exposure_mode": "compact",
        },
    )
    assert mint_resp.status_code == 201
    mint_data = mint_resp.json()
    assert mint_data["status"] == "ok"
    assert mint_data["token"].startswith("nce_mcp_")
    assert "type:erp-crm" in mint_data["allowed_engines"]
    assert "cat:commercial" in mint_data["allowed_engines"]
    assert "NCE_MASTER_KEY" not in mint_data["mcp_config_json"]

    # Verify the minted token authenticates against the Streamable HTTP /mcp gateway
    mcp_app = create_mcp_gateway_app()
    mcp_client = TestClient(mcp_app)
    mcp_resp = mcp_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {mint_data['token']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert mcp_resp.status_code == 200
    assert "Mcp-Session-Id" in mcp_resp.headers
