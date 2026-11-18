"""Unit regression suite for Core hardening and efficiency refinements.

Covers security boundary enforcement, cryptographic fast-path wrapping,
hot-path query consolidation, worker safety, OOXML bomb guards, and
wheel packaging exclusions introduced across Waves 1-4.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import io
import json
import sys
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. MCP Gateway Session Binding, Batch Bounds & Principal Tiers
# ---------------------------------------------------------------------------


def test_mcp_gateway_bearer_session_mismatch_and_batch_limits() -> None:
    from nce.mcp_http_gateway import (
        create_mcp_gateway_app,
        register_test_mcp_client_token,
    )

    ns_id = str(uuid.uuid4())
    tok_a = f"tok-a-{uuid.uuid4()}"
    tok_b = f"tok-b-{uuid.uuid4()}"
    register_test_mcp_client_token(
        tok_a,
        namespace_id=ns_id,
        principal_id="alice",
        scopes=["tools:read", "tools:call"],
    )
    register_test_mcp_client_token(
        tok_b,
        namespace_id=ns_id,
        principal_id="bob",
        scopes=["tools:read", "tools:call"],
    )

    app = create_mcp_gateway_app(engine=MagicMock(redis_client=None, pg_pool=None))
    client = TestClient(app)

    init_resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tok_a}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert init_resp.status_code == 200
    sess_id = init_resp.headers["mcp-session-id"]

    # Supplying a different Bearer token alongside an existing Mcp-Session-Id -> 403
    mismatch = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tok_b}", "Mcp-Session-Id": sess_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert mismatch.status_code == 403

    # Empty JSON-RPC batch -> -32600 Invalid Request
    empty_batch = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tok_a}"},
        json=[],
    )
    assert empty_batch.status_code == 400
    assert empty_batch.json()["error"]["code"] == -32600

    # Oversized JSON-RPC batch (>50 items) -> -32600 Invalid Request
    big_batch = [
        {"jsonrpc": "2.0", "id": idx, "method": "tools/list", "params": {}} for idx in range(51)
    ]
    too_big = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {tok_a}"},
        json=big_batch,
    )
    assert too_big.status_code == 400
    assert too_big.json()["error"]["code"] == -32600


def test_mcp_gateway_mutation_requires_tools_call_and_prod_scrubs_internal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nce.config import cfg
    from nce.mcp_http_gateway import (
        create_mcp_gateway_app,
        register_test_mcp_client_token,
    )
    from nce.tool_registry import TOOL_REGISTRY

    ns_id = str(uuid.uuid4())
    read_only_tok = f"tok-ro-{uuid.uuid4()}"
    call_tok = f"tok-call-{uuid.uuid4()}"
    register_test_mcp_client_token(
        read_only_tok,
        namespace_id=ns_id,
        principal_id="reader",
        scopes=["tools:read"],
    )
    register_test_mcp_client_token(
        call_tok,
        namespace_id=ns_id,
        principal_id="caller",
        scopes=["tools:read", "tools:call"],
    )

    app = create_mcp_gateway_app(engine=MagicMock(redis_client=None, pg_pool=None))
    client = TestClient(app)

    # Calling a mutation tool without tools:call must fail closed with -32003
    denied = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {read_only_tok}"},
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "store_memory",
                "arguments": {"namespace_id": ns_id, "agent_id": "a1", "content": "hi"},
            },
        },
    )
    assert denied.status_code == 200
    assert denied.json()["error"]["code"] == -32003

    # In production, unexpected exceptions inside tools/call must not leak raw messages
    orig_spec = TOOL_REGISTRY["semantic_search"]

    async def _boom(_engine, _args):
        raise RuntimeError("sensitive internal connection string leaked")

    from dataclasses import replace

    monkeypatch.setitem(TOOL_REGISTRY, "semantic_search", replace(orig_spec, handler=_boom))
    monkeypatch.setattr(cfg, "IS_PROD", True)
    with patch(
        "nce.tool_governance.GOVERNANCE", MagicMock(is_disabled=AsyncMock(return_value=False))
    ):
        err_resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {call_tok}"},
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "semantic_search",
                    "arguments": {"namespace_id": ns_id, "query": "hello"},
                },
            },
        )
    assert err_resp.status_code == 200
    body = err_resp.json()
    assert body["error"]["code"] == -32603
    assert body["error"]["data"] == "Internal server error"
    assert "connection string" not in json.dumps(body)


@pytest.mark.asyncio
async def test_put_me_context_tier_boundaries() -> None:
    from nce.auth import NamespaceContext
    from nce.me_app import put_me_context

    ns_id = uuid.uuid4()

    def _make_req(ctx: NamespaceContext, body_obj: object) -> MagicMock:
        req = MagicMock()
        req.state = SimpleNamespace(namespace_ctx=ctx)
        req.app = SimpleNamespace(state=SimpleNamespace(engine=None))
        req.json = AsyncMock(return_value=body_obj)
        return req

    # Unverified principal cannot self-bind
    unverified_ctx = NamespaceContext(
        namespace_id=ns_id,
        agent_id="u1",
        principal_id="u1",
        principal_kind="unverified",
    )
    res_unv = await put_me_context(_make_req(unverified_ctx, {"roles": ["viewer"]}))
    assert res_unv.status_code == 403
    assert json.loads(bytes(res_unv.body))["error"]["data"]["reason"] == "unverified_principal_tier"

    # Contractor cannot escalate into employee_id or admin role
    ext_scope = uuid.uuid4()
    contractor_ctx = NamespaceContext(
        namespace_id=ns_id,
        agent_id="c1",
        principal_id="c1",
        principal_kind="contractor",
        external_scope_id=ext_scope,
    )
    res_emp_id = await put_me_context(_make_req(contractor_ctx, {"employee_id": "E-1"}))
    assert res_emp_id.status_code == 403

    res_role = await put_me_context(_make_req(contractor_ctx, {"roles": ["admin"]}))
    assert res_role.status_code == 403

    # Employee self-binding succeeds cleanly
    emp_ctx = NamespaceContext(
        namespace_id=ns_id,
        agent_id="e1",
        principal_id="e1",
        principal_kind="employee",
    )
    res_ok = await put_me_context(
        _make_req(emp_ctx, {"employee_id": "E-100", "roles": ["engineer"]})
    )
    assert res_ok.status_code == 200
    assert json.loads(bytes(res_ok.body))["tier"] == "employee"


def test_jwt_unknown_principal_kind_and_mtls_prod_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nce.auth import extract_caller_identity
    from nce.config import cfg
    from nce.jwt_auth import decode_agent_token

    ns_id = str(uuid.uuid4())
    import jwt as pyjwt

    jwt_secret = "unit-test-jwt-secret-0123456789abcdef0123456789"
    monkeypatch.setattr(cfg, "NCE_JWT_SECRET", jwt_secret)
    monkeypatch.setattr(cfg, "NCE_JWT_ISSUER", None)
    monkeypatch.setattr(cfg, "NCE_JWT_AUDIENCE", None)
    token = pyjwt.encode(
        {
            "sub": "svc-user",
            "namespace_id": ns_id,
            "principal_kind": "partner-unknown-tier",
            "exp": int(datetime.now(timezone.utc).timestamp()) + 300,
            "nbf": int(datetime.now(timezone.utc).timestamp()) - 10,
        },
        jwt_secret,
        algorithm="HS256",
    )
    ctx = decode_agent_token(token)
    assert ctx.principal_kind == "unverified"

    # In production with mTLS disabled, spoofed client-cert forwarding headers are ignored
    monkeypatch.setattr(cfg, "IS_PROD", True)
    monkeypatch.setattr(cfg, "NCE_ADMIN_MTLS_ENABLED", False)
    req = SimpleNamespace(
        state=SimpleNamespace(),
        headers={
            "x-forwarded-client-cert": 'By=spiffe://cluster.local/ns/default/sa/api;Subject="CN=spoof"'
        },
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert extract_caller_identity(req) == "client:127.0.0.1"


# ---------------------------------------------------------------------------
# 2. SSRF (RFC 6598 + IDNA SNI), Sanitize, Body Limits & Webhooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssrf_cgnat_idna_sni_and_sanitize_order() -> None:
    import ipaddress

    import httpx

    from nce._http_utils import SafeAsyncClient, UnsafeURLError, _is_unsafe_ip
    from nce.sanitize import _MAX_PAYLOAD_LEN, sanitize_llm_payload

    assert _is_unsafe_ip(ipaddress.ip_address("100.64.0.1")) is True
    assert _is_unsafe_ip(ipaddress.ip_address("100.127.255.254")) is True

    with patch(
        "nce._http_utils.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("100.64.1.5", 443))]
    ):
        async with SafeAsyncClient() as cgnat_client:
            with pytest.raises(UnsafeURLError):
                await cgnat_client.get("https://internal-cgnat.example/api")

    captured: dict[str, object] = {}

    async def _fake_transport_send(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["host"] = request.headers.get("Host")
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="ok")

    with patch(
        "nce._http_utils.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]
    ):
        async with SafeAsyncClient(
            transport=httpx.MockTransport(_fake_transport_send)
        ) as safe_client:
            safe_client._skip_ip_pinning = False
            resp = await safe_client.get("https://b\u00fccher.example/path")
            assert resp.status_code == 200

    assert isinstance(captured["sni"], str)
    assert captured["sni"] == "xn--bcher-kva.example"
    assert captured["host"] == "xn--bcher-kva.example"

    # HTML entity-encoded zero-width chars are decoded before stripping,
    # and truncation never splits '{{' / '}}' brace escapes
    raw = "&#x200b;<b>hi</b> " + ("{" * (_MAX_PAYLOAD_LEN + 50))
    cleaned = sanitize_llm_payload(raw)
    assert "\u200b" not in cleaned
    assert "<b>" not in cleaned
    assert cleaned.startswith("hi ")
    assert cleaned.count("{") % 2 == 0


@pytest.mark.asyncio
async def test_bounded_body_middleware_handles_base_exception_group() -> None:
    from nce.http_body_limit import BoundedBodyMiddleware

    async def streaming_app(scope, receive, send):
        async def _consume():
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break

        task = asyncio.create_task(_consume())
        await task

    chunks = [
        {"type": "http.request", "body": b"0123456789abcdef0", "more_body": False},
    ]

    async def fake_receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    sent: list[dict] = []

    async def fake_send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-length", b"10")],  # lied smaller than actual body
    }

    base_eg_cls = getattr(builtins, "BaseExceptionGroup", None)
    if base_eg_cls is None:
        pytest.skip("BaseExceptionGroup requires Python >= 3.11")

    async def grouped_app(s, r, snd):
        try:
            await streaming_app(s, r, snd)
        except Exception as exc:
            raise base_eg_cls("eg", [exc]) from exc

    mw_eg = BoundedBodyMiddleware(grouped_app, max_bytes=16)
    await mw_eg(scope, fake_receive, fake_send)
    assert sent[0]["status"] == 413


# ---------------------------------------------------------------------------
# 3. Fast HKDF Data-Key Wrapping (TCHK\x01), Ring Rewrap & Rotation Fallback
# ---------------------------------------------------------------------------


def test_tchk01_hkdf_wrap_unwrap_and_rewrap() -> None:
    from nce.envelope import (
        clear_unwrapped_dek_cache,
        decrypt_with_dek,
        encrypt_with_dek,
        generate_dek,
        unwrap_dek,
        wrap_dek,
    )
    from nce.master_key_ring import blob_is_on_primary, master_key_ring, rewrap
    from nce.signing import (
        _ENCRYPTED_KEY_BLOB_HKDF,
        _ENCRYPTED_KEY_BLOB_V5,
        MasterKey,
        decrypt_signing_key,
        encrypt_fast_data_key,
        encrypt_signing_key,
        require_master_key,
    )

    mk = require_master_key()
    dek = generate_dek()
    wrapped = wrap_dek(dek, mk)
    assert wrapped.startswith(_ENCRYPTED_KEY_BLOB_V5)
    assert _ENCRYPTED_KEY_BLOB_HKDF in wrapped[:48]
    clear_unwrapped_dek_cache()
    assert unwrap_dek(wrapped, mk) == dek

    ct = encrypt_with_dek(b"sensitive memory body", dek)
    assert decrypt_with_dek(ct, dek) == b"sensitive memory body"

    # Ring rewrap preserves TCHK\x01 for fast data keys and re-wraps legacy blobs
    old_mk_str = "Old_Master_Key_With_High_Entropy_0123456789!"
    new_mk_str = "New_Master_Key_With_High_Entropy_9876543210!"
    old_mk = MasterKey(old_mk_str.encode("utf-8"))
    new_mk = MasterKey(new_mk_str.encode("utf-8"))
    fast_old = encrypt_fast_data_key(dek, old_mk)
    assert _ENCRYPTED_KEY_BLOB_HKDF in fast_old[:48]
    with patch.dict(
        "os.environ",
        {"NCE_MASTER_KEY": new_mk_str, "NCE_MASTER_KEY_PREVIOUS": old_mk_str},
    ):
        ring = master_key_ring()
        assert blob_is_on_primary(fast_old, new_mk) is False
        rewrapped = rewrap(fast_old, new_mk, ring)
        assert rewrapped is not None
        assert _ENCRYPTED_KEY_BLOB_HKDF in rewrapped[:48]
        assert decrypt_signing_key(rewrapped, new_mk) == dek
        assert blob_is_on_primary(rewrapped, new_mk) is True

    # PBKDF2 / Argon2id signing key wrapping continues to interoperate
    s_blob = encrypt_signing_key(dek, old_mk)
    assert _ENCRYPTED_KEY_BLOB_HKDF not in s_blob[:48]
    assert decrypt_signing_key(s_blob, old_mk) == dek


# ---------------------------------------------------------------------------
# 4. Hot-Path Memory Orchestrator, Event Log, Vector Casts & Working Memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_orchestrator_skips_dup_pii_and_runs_mongo_embed_in_parallel() -> None:
    from nce.models import AssertionType, MemoryType, StoreMemoryRequest
    from nce.orchestrators.memory import MemoryOrchestrator

    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _fake_scoped(_pool, _ns):
        yield mock_conn

    orch = MemoryOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        redis_client=MagicMock(),
    )
    payload = StoreMemoryRequest(
        namespace_id=uuid.uuid4(),
        agent_id="agent-1",
        content="Identical text",
        summary="Identical text",
        heavy_payload="Identical text",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
    )

    with (
        patch("nce.orchestrators.memory.scoped_pg_session", _fake_scoped),
        patch("nce.pii.process", new_callable=AsyncMock) as mock_pii,
        patch("nce.graph_extractor.extract_async", new_callable=AsyncMock, return_value=([], [])),
    ):
        mock_pii.return_value = SimpleNamespace(
            sanitized_text="Identical text", redacted=False, entities_found=[], vault_entries=[]
        )
        _res, sum_txt, heavy_txt, _ents, _trips = await orch._apply_pii_pipeline(payload)
        # heavy_payload == summary skips the duplicate pii_process call
        assert mock_pii.await_count == 1
        assert sum_txt == "Identical text"
        assert heavy_txt == "Identical text"

    # Parallel gather in _run_store_memory_saga invokes both before returning
    orch._saga_log_start = AsyncMock(return_value=str(uuid.uuid4()))  # type: ignore[method-assign]
    orch._apply_pii_pipeline = AsyncMock(  # type: ignore[method-assign]
        return_value=(SimpleNamespace(redacted=False), "sum", "sum", [], [])
    )
    orch._store_episodic_mongodb = AsyncMock(  # type: ignore[method-assign]
        return_value=(str(ObjectId()), MagicMock(), b"dek", "k1")
    )
    orch._store_semantic_graph_pg = AsyncMock(return_value=uuid.uuid4())  # type: ignore[method-assign]
    orch._saga_log_transition = AsyncMock()  # type: ignore[method-assign]
    orch._cache_working_memory_redis = AsyncMock()  # type: ignore[method-assign]
    orch._detect_contradictions_sync = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch(
        "nce.orchestrators.memory._embeddings.embed_batch",
        new_callable=AsyncMock,
        return_value=[[0.1] * 8],
    ):
        res = await orch._run_store_memory_saga(payload)
    assert res["quarantined"] is False
    orch._store_episodic_mongodb.assert_awaited_once()
    orch._store_semantic_graph_pg.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_log_single_build_signing_fields_and_merkle_end_seq_pushdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nce import event_log as ev_mod

    class _FakePgConn:
        def is_in_transaction(self) -> bool:
            return True

        async def execute(self, *_args, **_kwargs) -> str:
            return "SELECT 1"

    mock_conn = MagicMock(spec=_FakePgConn)
    mock_conn.is_in_transaction.return_value = True
    mock_conn.execute = AsyncMock(return_value="SELECT 1")
    monkeypatch.setattr(ev_mod.asyncpg, "Connection", _FakePgConn)

    ns_id = uuid.uuid4()
    ev_id = uuid.uuid4()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            {"seq": 1, "db_clock": now, "prev_hash": None},
            {
                "id": ev_id,
                "namespace_id": ns_id,
                "agent_id": "agent-1",
                "event_type": "store_memory",
                "event_seq": 1,
                "occurred_at": now,
                "signature_key_id": "key-1",
                "chain_hash": b"\xaa" * 32,
            },
        ]
    )

    build_calls = 0
    orig_build = ev_mod._build_signing_fields

    def _counting_build(**kwargs):
        nonlocal build_calls
        build_calls += 1
        return orig_build(**kwargs)

    with (
        patch.object(ev_mod, "_build_signing_fields", side_effect=_counting_build),
        patch("nce.event_log.get_active_key", AsyncMock(return_value=("key-1", b"k" * 32))),
    ):
        await ev_mod.append_event(
            conn=mock_conn,
            namespace_id=ns_id,
            agent_id="agent-1",
            event_type="store_memory",
            params={
                "saga_id": str(uuid.uuid4()),
                "memory_id": str(uuid.uuid4()),
                "payload_ref": "a" * 24,
                "assertion_type": "fact",
                "entities": [],
                "triplets": [],
            },
        )
    assert build_calls == 1
    mock_conn.execute.assert_awaited_once()

    # verify_merkle_chain pushes end_seq <= $3 into SQL when conn is asyncpg.Connection
    merkle_conn = MagicMock(spec=_FakePgConn)
    merkle_conn.fetch = AsyncMock(return_value=[])
    await ev_mod.verify_merkle_chain(merkle_conn, namespace_id=ns_id, start_seq=1, end_seq=10)
    sql_arg = merkle_conn.fetch.await_args.args[0]
    assert "event_seq <= $3" in sql_arg
    assert merkle_conn.fetch.await_args.args[3] == 10


def test_vector_halfvec_casts_present_in_source() -> None:
    graph_src = (REPO_ROOT / "nce/orchestrators/graph.py").read_text(encoding="utf-8")
    contra_src = (REPO_ROOT / "nce/contradictions.py").read_text(encoding="utf-8")
    sem_src = (REPO_ROOT / "nce/semantic_search.py").read_text(encoding="utf-8")

    assert "$1::halfvec(768)" in graph_src
    assert "$1::vector" not in graph_src
    assert "1 - (embedding <=> $1::halfvec(768))" in contra_src
    assert "1 - (embedding <=> $1::vector)" not in contra_src
    assert "me.embedding <=> {p_vector}::vector" in sem_src
    assert "m.embedding <=> {p_vector}::halfvec(768)" in sem_src


@pytest.mark.asyncio
async def test_working_memory_binary_trim_enforces_budget() -> None:
    from nce.working_memory import compile_working_memory

    ns_id = uuid.uuid4()
    big_hits = [
        {"memory_id": str(uuid.uuid4()), "payload_ref": "r" * 24, "raw_data": "word " * 200}
        for _ in range(15)
    ]

    async def _fake_search(**_kwargs):
        return list(big_hits)

    engine = SimpleNamespace(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        embedding_fn=AsyncMock(),
        semantic_search=_fake_search,
    )
    with (
        patch("nce.working_memory._fetch_kg_neighborhood", AsyncMock(return_value=[{"s": "a"}])),
        patch("nce.working_memory._fetch_heuristics", AsyncMock(return_value=[])),
        patch("nce.working_memory._fetch_contradictions", AsyncMock(return_value=[])),
    ):
        res = await compile_working_memory(
            engine,
            goal="check token budget trimming",
            namespace_id=ns_id,
            max_tokens=300,
        )
    assert res["token_count"] <= 300


# ---------------------------------------------------------------------------
# 5. Cold-Start Lazy Registry, Resource Guard, Workers, Extractors & Packaging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_registry_lazy_import_and_patch_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nce.tool_registry as tr

    mock_handler = AsyncMock(return_value="ok-lazy")
    with patch("nce.memory_mcp_handlers.handle_get_recent_context", mock_handler):
        out = await tr.TOOL_REGISTRY["get_recent_context"].handler(MagicMock(), {"limit": 5})
    assert out == "ok-lazy"
    assert tr.infer_tool_engine(tr.TOOL_REGISTRY["pricing_resolve"]) == "pricing"
    assert tr.infer_tool_engine(tr.TOOL_REGISTRY["resolve"]) == "entity_resolution"


def test_resource_surface_respects_disabled_engines_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nce.resource_surface as rs

    monkeypatch.setenv("NCE_ENABLED_ENGINES", "none")
    monkeypatch.setattr(rs, "_LAST_LOADED_ENGINES_ENV", None)
    with patch.object(importlib, "import_module") as mock_imp:
        rs.load_all_engine_resources()
    mock_imp.assert_not_called()


@pytest.mark.asyncio
async def test_outbox_relay_concurrent_sync_drain_preserves_m0w20d() -> None:
    from nce import outbox_relay

    calls: list[int] = []

    def _make_act(idx: int):
        def _act():
            calls.append(idx)

        return _act

    actions = [_make_act(1), _make_act(2), _make_act(3)]
    fake_conn = MagicMock()
    fake_tx = AsyncMock()
    fake_conn.transaction.return_value = fake_tx
    fake_pool = MagicMock()
    fake_pool.acquire.return_value = AsyncMock(
        __aenter__=AsyncMock(return_value=fake_conn), __aexit__=AsyncMock(return_value=False)
    )

    events = [
        {
            "id": uuid.uuid4(),
            "namespace_id": uuid.uuid4(),
            "event_type": "memory.stored",
            "attempt_count": 0,
            "payload": {},
        }
    ]
    with (
        patch.object(outbox_relay, "poll_outbox", AsyncMock(return_value=events)),
        patch.object(outbox_relay, "deliver_one", AsyncMock(return_value=actions)),
        patch.object(outbox_relay, "mark_published", AsyncMock()),
    ):
        res = await outbox_relay.run_outbox_relay_once(fake_pool)
    assert int(res) == 1
    assert sorted(calls) == [1, 2, 3]


@pytest.mark.asyncio
async def test_salience_buffer_prefers_scan_iter_and_reembedder_advances_empty_batch() -> None:
    import redis.asyncio as aioredis

    from nce.re_embedder import run_re_embedding_worker
    from nce.salience_buffer import flush_salience_buffer

    ns_id = uuid.uuid4()
    mem_id = uuid.uuid4()
    key = f"nce:ns:{ns_id}:salience_buffer"

    class _FakeAsyncRedis(aioredis.Redis):
        def __init__(self) -> None:
            self.eval_calls: list[tuple] = []

        async def scan_iter(self, match=None, count=None):
            yield key.encode("utf-8")

        async def hgetall(self, k):
            return {f"{mem_id}:agent1".encode(): b"0.25"}

        async def eval(self, script, numkeys, *args):
            self.eval_calls.append((numkeys, args))
            return 1

    fake_redis = _FakeAsyncRedis()
    fake_conn = MagicMock()

    @asynccontextmanager
    async def _fake_scoped(_pool, _ns):
        yield fake_conn

    with (
        patch("nce.salience_buffer.scoped_pg_session", _fake_scoped),
        patch("nce.salience_buffer.reinforce", AsyncMock()),
    ):
        flushed = await flush_salience_buffer(fake_redis, MagicMock())
    assert flushed == 1
    assert len(fake_redis.eval_calls) == 1
    assert fake_redis.eval_calls[0][1][1] == f"{mem_id}:agent1"
    assert fake_redis.eval_calls[0][1][2] == "0.25"

    # re_embedder: when all fetched memories have unreadable/invalid payload_ref,
    # cursor still advances and sleeps instead of spinning endlessly on row 0
    mig_id = uuid.uuid4()
    target_model = uuid.uuid4()
    last_mem = uuid.uuid4()
    re_conn = MagicMock()
    re_conn.transaction.return_value = AsyncMock()
    re_conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": mig_id,
                "target_model_id": target_model,
                "last_memory_id": None,
                "last_node_id": None,
                "model_name": "m",
            },
            asyncio.CancelledError(),
        ]
    )
    re_conn.fetch = AsyncMock(
        return_value=[{"id": last_mem, "payload_ref": "not-an-oid", "namespace_id": ns_id}]
    )
    re_conn.execute = AsyncMock()
    re_pool = MagicMock()
    re_pool.acquire.return_value = AsyncMock(
        __aenter__=AsyncMock(return_value=re_conn), __aexit__=AsyncMock(return_value=False)
    )

    with (
        patch("nce.re_embedder.asyncio.sleep", AsyncMock()) as mock_sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_re_embedding_worker(re_pool, MagicMock())

    re_conn.execute.assert_any_await(
        "UPDATE embedding_migrations SET last_memory_id = $1 WHERE id = $2",
        last_mem,
        mig_id,
    )
    assert any(c.args == (0.05,) for c in mock_sleep.await_args_list)


@pytest.mark.asyncio
async def test_ooxml_xlsx_and_pptx_reject_xml_entity_bomb() -> None:
    from nce.extractors.office_excel import extract_xlsx
    from nce.extractors.office_pptx import extract_pptx

    buf = io.BytesIO()
    entities = "".join(f"<!ENTITY e{i} 'x'>" for i in range(25))
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f"<!DOCTYPE a [{entities}]><workbook/>")
    bomb = buf.getvalue()

    xlsx_res = await extract_xlsx(bomb)
    assert xlsx_res.skip_reason is not None and "xml_entity_bomb" in xlsx_res.skip_reason

    pptx_res = await extract_pptx(bomb)
    assert pptx_res.skip_reason is not None and "xml_entity_bomb" in pptx_res.skip_reason


def test_pyproject_excludes_verticals_and_non_package_dirs() -> None:
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib as _toml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

    if _toml is not None:
        cfg_data = _toml.loads(pyproject_text)
        assert cfg_data["project"]["version"] == "3.5.3"
        find_cfg = cfg_data["tool"]["setuptools"]["packages"]["find"]
        assert find_cfg["include"] == ["nce", "nce.*"]
        assert "nce_verticals*" in find_cfg["exclude"]
        assert "tests*" in find_cfg["exclude"]
    else:
        assert 'version = "3.5.3"' in pyproject_text
        assert 'include = ["nce", "nce.*"]' in pyproject_text
        assert '"nce_verticals*"' in pyproject_text
        assert '"tests*"' in pyproject_text
    assert sys.version_info >= (3, 10)


def test_webhook_client_ip_selects_rightmost_non_internal_hop() -> None:
    from nce.webhook_receiver.main import _client_ip

    def _make_req(xff: str, peer: str = "10.0.0.5") -> MagicMock:
        req = MagicMock()
        req.headers = {"x-forwarded-for": xff}
        req.client = MagicMock(host=peer)
        return req

    with patch("nce.webhook_receiver.main.cfg") as mock_cfg:
        mock_cfg.NCE_WEBHOOK_TRUST_PROXY = True
        assert _client_ip(_make_req("203.0.113.50, 10.0.0.1")) == "203.0.113.50"
        assert _client_ip(_make_req("198.51.100.99, 203.0.113.50, 10.0.0.1")) == "203.0.113.50"
        assert _client_ip(_make_req("not-an-ip, <bad>", peer="10.0.0.5")) == "10.0.0.5"


@pytest.mark.asyncio
async def test_state_digest_imports_motor_timeout_kwargs_and_preserves_replayed_kg_edges(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from nce.state_digest import compute_namespace_state_digest

    ns = uuid.uuid4()
    mem_id = uuid.uuid4()
    ep_oid = ObjectId()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fetched_sqls: list[str] = []

    class _FakeCursor:
        def __init__(self, docs: list[dict]) -> None:
            self._docs = docs

        def __aiter__(self):
            async def _gen():
                for doc in self._docs:
                    yield doc

            return _gen()

    fake_db = SimpleNamespace(
        episodes=MagicMock(
            find=MagicMock(return_value=_FakeCursor([{"_id": ep_oid, "raw_data": "hello"}]))
        ),
        code_files=MagicMock(find=MagicMock(return_value=_FakeCursor([]))),
    )

    @asynccontextmanager
    async def _fake_scoped_mongo(_client, _ns):
        yield fake_db

    fake_mongo = MagicMock()

    async def _fake_fetch(sql: str, *args):
        fetched_sqls.append(sql)
        if "FROM memories" in sql:
            return [
                {
                    "id": mem_id,
                    "agent_id": "a1",
                    "created_at": now,
                    "memory_type": "episodic",
                    "assertion_type": "fact",
                    "valid_from": now,
                    "valid_to": None,
                    "derived_from": [],
                    "metadata": {},
                    "payload_ref": str(ep_oid),
                    "wrapped_dek": None,
                }
            ]
        return [
            {
                "subject_label": "TargetEntity",
                "predicate": "linked_to",
                "object_label": "AnotherEntity",
                "confidence": 0.9,
            }
        ]

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fake_fetch)

    with (
        caplog.at_level("WARNING", logger="nce.state_digest"),
        patch("nce.state_digest.AsyncIOMotorClient", return_value=fake_mongo),
        patch("nce.db_utils.scoped_mongo_session", _fake_scoped_mongo),
    ):
        digest = await compute_namespace_state_digest(conn, ns, as_of=now)

    assert len(digest) == 64
    assert "Failed to connect to MongoDB or fetch payloads for digest" not in caplog.text
    fake_mongo.close.assert_called_once()
    kg_sql = next(s for s in fetched_sqls if "FROM kg_edges" in s)
    assert "created_at <=" not in kg_sql
    assert "WHERE namespace_id = $1" in kg_sql
