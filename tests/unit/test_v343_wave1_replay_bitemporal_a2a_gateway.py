"""Regression tests for v3.4.3 Wave 1 remediation (Lane B: #2a-#2e, #5, #6, #7, #8, #14)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from starlette.testclient import TestClient

from nce import replay
from nce.a2a_mcp_handlers import (
    _build_grant_request,
    handle_a2a_create_grant,
    handle_a2a_inspect_grant,
    handle_a2a_list_grants,
    handle_a2a_query_shared,
    handle_a2a_revoke_grant,
    handle_a2a_update_grant_scopes,
    handle_a2a_verify_grant_status,
)
from nce.auth import (
    ScopeError,
    _validate_scope,
    bind_mcp_http_session,
    get_current_mcp_session,
)
from nce.embeddings import VECTOR_DIM
from nce.event_types import EventType
from nce.graph_query import GraphRAGTraverser
from nce.mcp_http_gateway import (
    MCPGatewaySession,
    bind_session_tenant_arguments,
    create_mcp_gateway_app,
    register_test_mcp_client_token,
)
from nce.models import A2AQuerySharedRequest, ReplayForkRequest
from nce.replay_mcp_handlers import handle_get_event_provenance, handle_replay_status
from nce.semantic_search import semantic_search
from nce.signing import canonical_json
from nce.snapshot_mcp_handlers import handle_import_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_mock_pool_and_conn() -> tuple[MagicMock, AsyncMock, MagicMock]:
    pool = MagicMock()
    conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = False
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn, tx


def _make_gateway_session(
    namespace_id: str,
    *,
    scopes: list[str] | None = None,
) -> MCPGatewaySession:
    return MCPGatewaySession(
        session_id=f"sess-{uuid.uuid4()}",
        namespace_id=namespace_id,
        principal_id="agent-lane-b",
        scopes=scopes if scopes is not None else ["tools:read", "tools:call"],
        allowed_engines=["*"],
        tool_exposure_mode="full",
        expanded_tools=set(),
        rate_limit_rpm=120,
        event_queue=asyncio.Queue(maxsize=16),
    )


# ---------------------------------------------------------------------------
# 1. Schema Sync (Task 1)
# ---------------------------------------------------------------------------


def test_schema_sync_namespaces_035_columns_and_split_halves() -> None:
    import sys

    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import check_schema_drift  # type: ignore[import-not-found]
    import split_schema  # type: ignore[import-not-found]

    cols = check_schema_drift.expected_columns(REPO_ROOT / "nce" / "schema.sql")
    assert {
        "embedding_provider",
        "embedding_model",
        "embedding_dim",
        "is_replay_sandbox",
    }.issubset(cols["namespaces"])

    boot, runtime, _counts = split_schema.build(REPO_ROOT / "nce" / "schema.sql")
    assert (REPO_ROOT / "nce" / "schema_bootstrap.sql").read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    ) == boot.replace("\r\n", "\n")
    assert (REPO_ROOT / "nce" / "schema_runtime.sql").read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    ) == runtime.replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# 2. Replay Overhaul (#2a - #2e)
# ---------------------------------------------------------------------------


def test_2a_build_event_query_and_record_include_signing_columns() -> None:
    ns_id = uuid.uuid4()
    sql, args = replay._build_event_query(
        source_namespace_id=ns_id,
        start_seq=1,
        end_seq=10,
        agent_id_filter=None,
    )
    assert "namespace_id" in sql
    assert "signature_version" in sql
    assert "chain_hash" in sql
    assert args[0] == ns_id

    now = datetime.now(timezone.utc)
    rec: dict[str, Any] = {
        "id": uuid.uuid4(),
        "namespace_id": ns_id,
        "event_seq": 3,
        "event_type": "store_memory",
        "occurred_at": now,
        "agent_id": "agent-1",
        "params": '{"memory_id": "11111111-1111-4111-8111-111111111111"}',
        "result_summary": None,
        "parent_event_id": None,
        "llm_payload_uri": None,
        "llm_payload_hash": None,
        "signature_version": 2,
        "chain_hash": b"\xab" * 32,
    }
    ev = replay._record_to_event_row(rec)  # type: ignore[arg-type]
    assert ev.namespace_id == ns_id
    assert ev.signature_version == 2
    assert ev.chain_hash == b"\xab" * 32
    assert ev.params == {"memory_id": "11111111-1111-4111-8111-111111111111"}
    assert ev.result_summary is None


@pytest.mark.asyncio
async def test_2b_parse_jsonb_dict_and_get_run_status_string_jsonb() -> None:
    assert replay._parse_jsonb_dict(None) == {}
    assert replay._parse_jsonb_dict("") == {}
    assert replay._parse_jsonb_dict('{"k": 1}') == {"k": 1}
    assert replay._parse_jsonb_dict("[1, 2, 3]") == {}
    assert replay._parse_jsonb_dict({"a": "b"}) == {"a": "b"}

    pool, conn, tx = _make_mock_pool_and_conn()
    run_id = uuid.uuid4()
    src_ns = uuid.uuid4()
    now = datetime.now(timezone.utc)
    conn.fetchrow = AsyncMock(
        return_value={
            "id": run_id,
            "source_namespace_id": src_ns,
            "target_namespace_id": None,
            "mode": "observational",
            "replay_mode": "deterministic",
            "start_seq": 1,
            "end_seq": 5,
            "divergence_seq": None,
            "config_overrides": '{"temperature": 0.1}',
            "status": "success",
            "events_applied": 5,
            "error": None,
            "started_at": now,
            "finished_at": now,
            "source_state_digest": None,
            "target_state_digest": None,
            "digest_match": None,
        }
    )

    session = _make_gateway_session(str(src_ns))
    with bind_mcp_http_session(session):
        status = await replay.get_run_status(pool, run_id)

    assert status["config_overrides"] == {"temperature": 0.1}
    assert tx.__aenter__.await_count == 1
    conn.execute.assert_awaited_once_with(
        "SELECT set_config('nce.namespace_id', $1, true)", str(src_ns)
    )


@pytest.mark.asyncio
async def test_2c_is_raw_uuid_initialized_before_try_no_unbound_local() -> None:
    _, conn, _ = _make_mock_pool_and_conn()
    bad_store_ev = replay._EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="store_memory",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={"memory_id": "not-a-valid-uuid"},
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )
    with pytest.raises(ValueError):
        await replay._handle_store_memory(conn, bad_store_ev, uuid.uuid4(), None, None)

    bad_cons_ev = replay._EventRow(
        event_id=uuid.uuid4(),
        event_seq=2,
        event_type="consolidation_run",
        occurred_at=datetime.now(timezone.utc),
        agent_id="agent-1",
        params={
            "payload_ref": str(ObjectId()),
            "source_namespace_id": "not-a-valid-uuid",
        },
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )
    with pytest.raises(ValueError):
        await replay._handle_consolidation_run(
            conn,
            bad_cons_ev,
            uuid.uuid4(),
            {"response": {"abstraction": "valid summary", "confidence": 0.9}},
            None,
        )


@pytest.mark.asyncio
async def test_2d_dispatch_dual_namespace_rls_wrapped_dek_and_memory_id_remap() -> None:
    _, write_conn, _ = _make_mock_pool_and_conn()
    src_ns = uuid.uuid4()
    tgt_ns = uuid.uuid4()
    src_mem_id = uuid.uuid4()
    run_id = uuid.uuid4()
    payload_oid = str(ObjectId())
    now = datetime.now(timezone.utc)

    write_conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "embedding": [0.25, 0.5, 0.75],
                "assertion_type": "fact",
                "memory_type": "episodic",
                "metadata": '{"tag": "alpha"}',
                "valid_from": now,
                "created_at": now,
                "wrapped_dek": b"wrapped-secret-dek",
                "dek_key_id": "kek-v1",
            },
            {"salience_score": 0.9},
        ]
    )

    ctx = replay.ReplayContext(tgt_ns, source_namespace_id=src_ns)
    ctx.copy_mongo_doc = AsyncMock(return_value=str(ObjectId()))  # type: ignore[method-assign]

    ev = replay._EventRow(
        event_id=uuid.uuid4(),
        event_seq=1,
        event_type="store_memory",
        occurred_at=now,
        agent_id="agent-1",
        params={"memory_id": str(src_mem_id), "payload_ref": payload_oid},
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
        namespace_id=src_ns,
    )

    fake_fork_ev = MagicMock()
    fake_fork_ev.event_id = uuid.uuid4()
    fake_fork_ev.event_seq = 1

    with patch(
        "nce.replay.append_event", new_callable=AsyncMock, return_value=fake_fork_ev
    ) as mock_append:
        summary, _ = await replay._dispatch_and_apply_event(
            write_conn,
            src=ev,
            ctx=ctx,
            target_namespace_id=tgt_ns,
            llm_payload=None,
            config_overrides=None,
            run_id=run_id,
            source_namespace_id=src_ns,
        )

    remapped_id = ctx.remap(src_mem_id)
    assert summary["new_memory_id"] == str(remapped_id)

    # Verify RLS switching: target -> source (read) -> target (write) -> source (salience read) -> target (salience write + append_event)
    rls_calls = [
        c.args[1]
        for c in write_conn.execute.await_args_list
        if "set_config('nce.namespace_id'" in c.args[0]
    ]
    assert str(src_ns) in rls_calls
    assert rls_calls[-1] == str(tgt_ns)

    # Verify INSERT INTO memories preserved wrapped_dek, dek_key_id, JSON metadata, and vector string
    insert_mem_call = next(
        c for c in write_conn.execute.await_args_list if "INSERT INTO memories" in c.args[0]
    )
    assert insert_mem_call.args[4] == "[0.25, 0.5, 0.75]"
    assert json.loads(insert_mem_call.args[8])["source_memory_id"] == str(src_mem_id)
    assert insert_mem_call.args[11] == b"wrapped-secret-dek"
    assert insert_mem_call.args[12] == "kek-v1"

    # Verify enriched_params remapped memory_id while keeping source_memory_id
    append_kwargs = mock_append.await_args.kwargs
    assert append_kwargs["params"]["memory_id"] == str(remapped_id)
    assert append_kwargs["params"]["source_memory_id"] == str(src_mem_id)
    assert append_kwargs["params"]["source_namespace_id"] == str(src_ns)


@pytest.mark.asyncio
async def test_2d_replay_handlers_pass_session_namespace_to_status_and_provenance() -> None:
    src_ns = str(uuid.uuid4())
    run_id = uuid.uuid4()
    mem_id = uuid.uuid4()
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    session = _make_gateway_session(src_ns)

    with (
        bind_mcp_http_session(session),
        patch(
            "nce.replay.get_run_status",
            new_callable=AsyncMock,
            return_value={"run_id": str(run_id), "status": "success"},
        ) as mock_status,
        patch(
            "nce.replay.get_event_provenance",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_prov,
    ):
        await handle_replay_status(engine, {"replay_run_id": str(run_id)})
        await handle_get_event_provenance(engine, {"memory_id": str(mem_id)})

    assert str(mock_status.await_args.kwargs["namespace_id"]) == src_ns
    assert str(mock_prov.await_args.kwargs["namespace_id"]) == src_ns


@pytest.mark.asyncio
async def test_2e_create_run_sandbox_flag_and_legacy_column_savepoint_fallback() -> None:
    import asyncpg

    _, conn, _ = _make_mock_pool_and_conn()
    src_ns = uuid.uuid4()
    tgt_ns = uuid.uuid4()

    async def exec_side_effect(sql: str, *args: Any) -> str:
        if "SET is_replay_sandbox = TRUE" in sql:
            raise asyncpg.UndefinedColumnError('column "is_replay_sandbox" does not exist')
        return "UPDATE 1"

    conn.execute = AsyncMock(side_effect=exec_side_effect)
    run_id = await replay._create_run(
        conn,
        source_namespace_id=src_ns,
        target_namespace_id=tgt_ns,
        mode="forked",
        replay_mode="deterministic",
        start_seq=1,
        end_seq=5,
        divergence_seq=5,
        config_overrides={"alpha": 0.5},
    )
    assert isinstance(run_id, uuid.UUID)
    executed_sql = [c.args[0] for c in conn.execute.await_args_list]
    assert any("SET is_replay_sandbox = TRUE" in s for s in executed_sql)
    assert any(
        "SET metadata = COALESCE(metadata" in s and "is_replay_sandbox = TRUE" not in s
        for s in executed_sql
    )


@pytest.mark.asyncio
async def test_2e_consolidation_run_falls_back_to_params_and_serializes_vector() -> None:
    _, conn, _ = _make_mock_pool_and_conn()
    src_ns = uuid.uuid4()
    tgt_ns = uuid.uuid4()
    ep_id = uuid.uuid4()
    ctx = replay.ReplayContext(tgt_ns, source_namespace_id=src_ns)
    ctx.copy_mongo_doc = AsyncMock(return_value=str(ObjectId()))  # type: ignore[method-assign]

    ev = replay._EventRow(
        event_id=uuid.uuid4(),
        event_seq=4,
        event_type="consolidation_run",
        occurred_at=datetime.now(timezone.utc),
        agent_id="consolidator",
        params={
            "source_namespace_id": str(src_ns),
            "payload_ref": str(ObjectId()),
            "abstraction": "Synthesized topology rule",
            "confidence": 0.85,
            "supporting_memory_ids": [str(ep_id)],
        },
        result_summary=None,
        parent_event_id=None,
        llm_payload_uri=None,
        llm_payload_hash=None,
    )
    with patch("nce.embeddings.embed", new_callable=AsyncMock, return_value=[0.1, 0.2, 0.3]):
        res = await replay._handle_consolidation_run(conn, ev, ctx, None, None)
    assert res["abstraction"] == "Synthesized topology rule"
    assert res["confidence"] == 0.85
    mem_insert = next(
        c for c in conn.execute.await_args_list if "INSERT INTO memories" in c.args[0]
    )
    assert mem_insert.args[4] == "[0.1, 0.2, 0.3]"
    assert isinstance(mem_insert.args[6], str)  # JSON-serialized metadata


# ---------------------------------------------------------------------------
# 3. Bi-Temporal Graph Query (#5) & Semantic Search (#6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5_graph_query_all_4_time_travel_ctes_qualified_lateral_and_shredded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, conn, _ = _make_mock_pool_and_conn()
    mongo_client = MagicMock()
    ns_id = str(uuid.uuid4())
    as_of = datetime(2026, 2, 1, tzinfo=timezone.utc)
    ev_id = str(uuid.uuid4())

    captured_queries: list[str] = []

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        captured_queries.append(sql)
        if "SELECT * FROM event_log WHERE id = ANY" in sql:
            return [
                {
                    "id": ev_id,
                    "namespace_id": ns_id,
                    "signature": b"\x01" * 32,
                    "signature_key_id": "sk-1",
                }
            ]
        if "WITH RECURSIVE traversal" in sql:
            return [{"label": "RouterA", "depth": 0}, {"label": "SwitchB", "depth": 1}]
        if "historical_edges" in sql:
            return [
                {
                    "subject_label": "RouterA",
                    "predicate": "uplinks_to",
                    "object_label": "SwitchB",
                    "payload_ref": None,
                    "decayed_confidence": 0.95,
                    "event_id": ev_id,
                }
            ]
        return [
            {
                "label": "RouterA",
                "entity_type": "DEVICE",
                "payload_ref": None,
                "distance": 0.05,
                "event_id": ev_id,
            }
        ]

    conn.fetch = AsyncMock(side_effect=fake_fetch)
    monkeypatch.setattr("nce.event_log.verify_event_signature", AsyncMock(return_value=None))

    async def embed(_q: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    traverser = GraphRAGTraverser(pg_pool=pool, mongo_client=mongo_client, embedding_fn=embed)
    anchors = await traverser._find_anchor("router", namespace_id=ns_id, top_k=1, as_of=as_of)
    visited, edges = await traverser._bfs("RouterA", max_depth=1, namespace_id=ns_id, as_of=as_of)
    nodes = await traverser._fetch_nodes_metadata(
        conn, list(visited), namespace_id=ns_id, as_of=as_of, anchor=anchors[0]
    )

    assert len(anchors) == 1
    assert len(edges) == 1
    assert len(nodes) == 1

    cte_queries = [q for q in captured_queries if "memory_events AS" in q]
    assert len(cte_queries) == 4
    for q in cte_queries:
        assert "event_log.id AS event_id" in q
        assert "event_log.namespace_id" in q
        assert "event_log.occurred_at" in q
        assert "event_log.event_seq" in q
        assert "'memory_shredded'" in q
        assert "CROSS JOIN LATERAL jsonb_array_elements(" in q

    anchor_cte = cte_queries[0]
    assert "WHERE m.embedding IS NOT NULL" in anchor_cte


@pytest.mark.asyncio
async def test_5_hydrate_sources_decrypts_wrapped_dek_inside_rls_transaction() -> None:
    pool, conn, tx = _make_mock_pool_and_conn()
    ns_id = str(uuid.uuid4())
    oid = ObjectId()
    ref_str = str(oid)

    conn.fetch = AsyncMock(
        return_value=[{"payload_ref": ref_str, "wrapped_dek": b"wrapped-dek-bytes"}]
    )

    class _Cursor:
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self._items = list(items)

        def __aiter__(self) -> _Cursor:
            return self

        async def __anext__(self) -> dict[str, Any]:
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    scoped_db = MagicMock()
    scoped_db.episodes.find = MagicMock(
        return_value=_Cursor([{"_id": oid, "raw_data": "ENC:blob", "type": "episodic"}])
    )
    scoped_db.code_files.find = MagicMock(return_value=_Cursor([]))

    @asynccontextmanager
    async def fake_scoped_mongo(_client: Any, _ns: Any):
        yield scoped_db

    traverser = GraphRAGTraverser(
        pg_pool=pool,
        mongo_client=MagicMock(),
        embedding_fn=AsyncMock(return_value=[0.1, 0.2, 0.3]),
    )
    with (
        patch("nce.db_utils.scoped_mongo_session", fake_scoped_mongo),
        patch(
            "nce.envelope.maybe_decrypt_raw_data",
            return_value="decrypted episode payload",
        ) as mock_dec,
    ):
        sources = await traverser._hydrate_sources({ref_str}, namespace_id=ns_id)

    assert tx.__aenter__.await_count == 1
    conn.execute.assert_awaited_once_with("SELECT set_config('nce.namespace_id', $1, true)", ns_id)
    mock_dec.assert_called_once_with("ENC:blob", b"wrapped-dek-bytes")
    assert sources[0]["excerpt"] == "decrypted episode payload"


@pytest.mark.asyncio
async def test_6_semantic_search_as_of_identifiers_and_offset_slicing() -> None:
    ns_id = str(uuid.uuid4())
    as_of_str = "2025-06-15T12:00:00Z"
    as_of_dt = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    created_dt = as_of_dt - timedelta(days=2)

    oids = [str(ObjectId()) for _ in range(4)]
    mids = [uuid.UUID(f"00000000-0000-4000-8000-00000000000{i}") for i in range(1, 5)]
    pg_rows = [
        {
            "payload_ref": oids[i],
            "memory_id": mids[i],
            "final_score": 0.9 - (i * 0.1),
            "raw_salience": 1.0,
            "last_updated": created_dt,
        }
        for i in range(4)
    ]

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"metadata": '{"temporal_retention_days": 30}'})
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(side_effect=[pg_rows, [], []])

    @asynccontextmanager
    async def fake_scoped_pg(_pool: Any, _ns: Any):
        yield conn

    @asynccontextmanager
    async def fake_scoped_mongo(_client: Any, _ns: Any):
        db = MagicMock()

        async def _find(*_a: Any, **_kw: Any):
            for i, ref in enumerate(oids):
                yield {"_id": ObjectId(ref), "raw_data": f"doc-{i} SKU-998877"}

        db.episodes.find = MagicMock(side_effect=_find)
        yield db

    async def embed(_q: str) -> list[float]:
        return [0.05] * VECTOR_DIM

    with (
        patch("nce.semantic_search.scoped_pg_session", fake_scoped_pg),
        patch("nce.db_utils.scoped_mongo_session", fake_scoped_mongo),
    ):
        results = await semantic_search(
            pg_pool=MagicMock(),
            mongo_client=MagicMock(),
            embedding_fn=embed,
            query="find device SKU-998877",
            namespace_id=ns_id,
            agent_id="agent-1",
            as_of=as_of_str,
            limit=2,
            offset=1,
        )

    sql, *params = conn.fetch.await_args_list[0].args
    assert as_of_dt in params
    assert (
        "LEAST(COALESCE(s.salience_score, 1.0), COALESCE(s_sys.salience_score, 1.0)) > 0.0"
        not in sql
    )
    assert sql.count("(m.valid_to IS NULL OR m.valid_to >") == 3
    assert "NOW()" not in sql
    assert "+ CASE WHEN (" in sql

    # Offset=1, Limit=2 slices items [1, 2] from the 4 ranked results, and historical 2-day-old memory is NOT stale
    assert [r["memory_id"] for r in results] == [mids[1], mids[2]]
    assert all(r["stale"] is False for r in results)


# ---------------------------------------------------------------------------
# 4. A2A Transaction, RLS & one_time Grant Plumbing (#7 & #14)
# ---------------------------------------------------------------------------


def test_7_a2a_grant_updated_in_event_types_and_replay_registry() -> None:
    assert "a2a_grant_updated" in get_args(EventType)
    assert "a2a_grant_updated" in replay._HANDLER_REGISTRY
    assert "a2a_grant_updated" in replay._additional_fork_provenance_types


def test_14_build_grant_request_plumbs_one_time_and_handles_false_strings() -> None:
    ns_id = str(uuid.uuid4())
    req_true = _build_grant_request(
        {
            "scopes": [{"resource_type": "namespace", "resource_id": ns_id}],
            "one_time": True,
            "expires_in_seconds": None,
        }
    )
    assert req_true.one_time is True
    assert req_true.expires_in_seconds == 3600

    req_false_str = _build_grant_request(
        {
            "scopes": [{"resource_type": "namespace", "resource_id": ns_id}],
            "one_time": "false",
            "can_delegate": "0",
        }
    )
    assert req_false_str.one_time is False
    assert req_false_str.can_delegate is False


@pytest.mark.asyncio
async def test_7_a2a_handlers_and_query_shared_use_rls_transactions() -> None:
    pool, conn, tx = _make_mock_pool_and_conn()
    engine = MagicMock()
    engine.pg_pool = pool
    owner_ns = uuid.uuid4()
    consumer_ns = uuid.uuid4()
    mem_id = uuid.uuid4()
    grant_id = uuid.uuid4()

    engine.semantic_search = AsyncMock(
        return_value=[{"memory_id": mem_id, "payload_ref": "ref1", "score": 0.9}]
    )
    conn.fetch = AsyncMock(
        return_value=[{"id": mem_id, "signature": b"\xcd" * 32, "signature_key_id": "sk-owner"}]
    )

    fake_verified = MagicMock()
    fake_verified.grant_id = grant_id
    fake_verified.owner_namespace_id = owner_ns
    fake_verified.owner_agent_id = "owner-agent"
    fake_verified.scopes = [
        MagicMock(resource_type="namespace", resource_id=str(owner_ns), permissions=["read"])
    ]

    with (
        patch(
            "nce.a2a_mcp_handlers.verify_token", new_callable=AsyncMock, return_value=fake_verified
        ),
        patch("nce.a2a_mcp_handlers.enforce_scope"),
        patch("nce.a2a._append_a2a_event", new_callable=AsyncMock),
    ):
        raw_out = await handle_a2a_query_shared(
            engine,
            {
                "sharing_token": "tok-123",
                "consumer_namespace_id": str(consumer_ns),
                "consumer_agent_id": "consumer-agent",
                "query": "shared topology",
            },
        )

    parsed = json.loads(raw_out)
    assert parsed["results"][0]["signature"] == ("cd" * 32)
    # 2 transactions: 1st for verify_token + _append_a2a_event, 2nd for memories signature hydration
    assert tx.__aenter__.await_count == 2
    rls_ns_args = [
        c.args[1]
        for c in conn.execute.await_args_list
        if "set_config('nce.namespace_id'" in c.args[0]
    ]
    assert rls_ns_args == [str(consumer_ns), str(owner_ns)]

    # Verify remaining 6 A2A handlers each open a transaction + set_namespace_context
    tx.__aenter__.reset_mock()
    with (
        patch(
            "nce.a2a_mcp_handlers.create_grant",
            new_callable=AsyncMock,
            return_value=MagicMock(
                grant_id=grant_id,
                sharing_token="tok",
                expires_at=datetime.now(timezone.utc),
            ),
        ),
        patch("nce.a2a_mcp_handlers.revoke_grant", new_callable=AsyncMock, return_value=True),
        patch("nce.a2a_mcp_handlers.list_grants", new_callable=AsyncMock, return_value=[]),
        patch(
            "nce.a2a_mcp_handlers.verify_grant_status",
            new_callable=AsyncMock,
            return_value={"valid": True},
        ),
        patch(
            "nce.a2a_mcp_handlers.update_grant_scopes",
            new_callable=AsyncMock,
            return_value={"updated": True},
        ),
        patch(
            "nce.a2a_mcp_handlers.inspect_grant",
            new_callable=AsyncMock,
            return_value={"grant_id": str(grant_id)},
        ),
    ):
        await handle_a2a_create_grant(
            engine,
            {
                "namespace_id": str(owner_ns),
                "scopes": [{"resource_type": "namespace", "resource_id": str(owner_ns)}],
                "one_time": True,
            },
        )
        await handle_a2a_revoke_grant(
            engine, {"namespace_id": str(owner_ns), "grant_id": str(grant_id)}
        )
        await handle_a2a_list_grants(engine, {"namespace_id": str(owner_ns)})
        await handle_a2a_verify_grant_status(
            engine, {"namespace_id": str(owner_ns), "grant_id": str(grant_id)}
        )
        await handle_a2a_update_grant_scopes(
            engine,
            {
                "namespace_id": str(owner_ns),
                "grant_id": str(grant_id),
                "scopes": [{"resource_type": "namespace", "resource_id": str(owner_ns)}],
            },
        )
        await handle_a2a_inspect_grant(
            engine, {"namespace_id": str(owner_ns), "grant_id": str(grant_id)}
        )

    assert tx.__aenter__.await_count == 6


# ---------------------------------------------------------------------------
# 5. Schema-Aware Tenant Binding on /mcp (#8)
# ---------------------------------------------------------------------------


def test_8_schema_aware_binding_a2a_query_shared_and_replay_fork_extra_forbid() -> None:
    sess_ns = str(uuid.uuid4())
    other_ns = str(uuid.uuid4())
    session = _make_gateway_session(sess_ns)

    # 1. a2a_query_shared: injects consumer_namespace_id, NOT namespace_id (passes extra="forbid")
    a2a_args: dict[str, Any] = {"sharing_token": "tok-abc", "query": "test"}
    err = bind_session_tenant_arguments("a2a_query_shared", a2a_args, session, req_id=1)
    assert err is None
    assert "namespace_id" not in a2a_args
    assert a2a_args["consumer_namespace_id"] == sess_ns
    parsed_a2a = A2AQuerySharedRequest(**a2a_args)
    assert str(parsed_a2a.consumer_namespace_id) == sess_ns

    # Cross-tenant consumer_namespace_id rejected with -32005
    bad_a2a = {"sharing_token": "tok-abc", "query": "test", "consumer_namespace_id": other_ns}
    err_cross = bind_session_tenant_arguments("a2a_query_shared", bad_a2a, session, req_id=2)
    assert err_cross is not None
    assert err_cross["error"]["code"] == -32005

    # 2. replay_fork: injects source_namespace_id, NOT namespace_id (passes extra="forbid")
    tgt_ns = str(uuid.uuid4())
    payload = {
        "source_namespace_id": sess_ns,
        "target_namespace_id": tgt_ns,
        "fork_seq": 5,
        "start_seq": 1,
        "replay_mode": "deterministic",
        "config_overrides": None,
        "agent_id_filter": None,
    }
    checksum = hashlib.sha256(canonical_json(payload)).hexdigest()
    fork_args: dict[str, Any] = {
        "target_namespace_id": tgt_ns,
        "fork_seq": 5,
        "expected_sha256": checksum,
    }
    err_fork = bind_session_tenant_arguments("replay_fork", fork_args, session, req_id=3)
    assert err_fork is None
    assert "namespace_id" not in fork_args
    assert fork_args["source_namespace_id"] == sess_ns
    parsed_fork = ReplayForkRequest(**fork_args)
    assert str(parsed_fork.source_namespace_id) == sess_ns

    # Cross-tenant source_namespace_id on replay_fork & replay_reconstruct rejected with -32005
    # (including when the tenant session carries "admin" scope to call replay_fork!)
    admin_scoped_tenant = _make_gateway_session(
        sess_ns, scopes=["mcp:tools:discover", "mcp:tools:call", "admin"]
    )
    for tool in ("replay_fork", "replay_reconstruct"):
        for s in (session, admin_scoped_tenant):
            cross_err = bind_session_tenant_arguments(
                tool, {"source_namespace_id": other_ns}, s, req_id=4
            )
            assert cross_err is not None
            assert cross_err["error"]["code"] == -32005


@pytest.mark.asyncio
async def test_8_import_snapshot_cross_tenant_rejected_in_gateway_and_handler() -> None:
    sess_ns = str(uuid.uuid4())
    other_ns = str(uuid.uuid4())
    session = _make_gateway_session(sess_ns)

    snap_args: dict[str, Any] = {"archive": {"dummy": True}}
    assert bind_session_tenant_arguments("import_snapshot", snap_args, session, req_id=1) is None
    assert "namespace_id" not in snap_args
    assert snap_args["target_namespace_id"] == sess_ns

    cross_err = bind_session_tenant_arguments(
        "import_snapshot",
        {"archive": {}, "target_namespace_id": other_ns},
        session,
        req_id=2,
    )
    assert cross_err is not None
    assert cross_err["error"]["code"] == -32005

    with bind_mcp_http_session(session):
        with pytest.raises(ScopeError, match="Cross-tenant target_namespace_id forbidden"):
            await handle_import_snapshot(
                MagicMock(), {"snapshot_data": "", "target_namespace_id": other_ns}
            )


def test_8_gateway_http_replay_status_contextvar_and_admin_scope_authorization() -> None:
    sess_ns = str(uuid.uuid4())
    other_ns = str(uuid.uuid4())

    # Admin scope validation via active HTTP MCP session without admin_api_key
    tenant_session = _make_gateway_session(sess_ns, scopes=["tools:read", "tools:call"])
    admin_session = _make_gateway_session(sess_ns, scopes=["tools:read", "tools:call", "admin"])

    with bind_mcp_http_session(tenant_session):
        with pytest.raises(ScopeError):
            _validate_scope("admin", {"command": "list"})

    with bind_mcp_http_session(admin_session):
        _validate_scope("admin", {"command": "list"})

    # Admin session preserves explicit cross-tenant namespace_id, defaults when omitted
    admin_explicit = {"command": "get", "namespace_id": other_ns}
    assert bind_session_tenant_arguments("manage_namespace", admin_explicit, admin_session) is None
    assert admin_explicit["namespace_id"] == other_ns

    admin_default = {"command": "list"}
    assert bind_session_tenant_arguments("manage_namespace", admin_default, admin_session) is None
    assert admin_default["namespace_id"] == sess_ns

    # End-to-end HTTP /mcp tools/call on replay_status: no namespace_id injected, ContextVar bound
    raw_token = f"tok-{uuid.uuid4()}"
    register_test_mcp_client_token(
        raw_token,
        namespace_id=sess_ns,
        principal_id="tenant-admin",
        scopes=["tools:read", "tools:call", "admin"],
    )

    seen_args: dict[str, Any] = {}
    seen_session_ns: list[str | None] = []

    async def fake_replay_status_handler(_engine: Any, arguments: dict[str, Any]) -> str:
        seen_args.update(arguments)
        cur = get_current_mcp_session()
        seen_session_ns.append(getattr(cur, "namespace_id", None))
        return json.dumps({"status": "success", "run_id": arguments["run_id"]})

    run_id = str(uuid.uuid4())
    app = create_mcp_gateway_app(engine=MagicMock(redis_client=None, pg_pool=None))
    from nce.tool_registry import TOOL_REGISTRY

    orig_spec = TOOL_REGISTRY["replay_status"]
    patched_spec = replace(orig_spec, handler=fake_replay_status_handler)
    caller_arguments = {"run_id": run_id}
    with (
        patch.dict(TOOL_REGISTRY, {"replay_status": patched_spec}),
        patch(
            "nce.tool_governance.GOVERNANCE",
            MagicMock(is_disabled=AsyncMock(return_value=False)),
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {raw_token}"},
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "replay_status",
                    "arguments": caller_arguments,
                },
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    assert body["result"]["isError"] is False
    assert "namespace_id" not in seen_args
    assert seen_args == {"run_id": run_id}
    assert caller_arguments == {"run_id": run_id}
    assert seen_session_ns == [sess_ns]
