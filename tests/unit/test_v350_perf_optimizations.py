"""Unit tests for v3.5.0 performance & hot-path optimizations."""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. Migration 037 + schema.sql / split schema contract
# ---------------------------------------------------------------------------


def test_migration_037_indexes_and_no_concurrently() -> None:
    mig = (_REPO_ROOT / "nce" / "migrations" / "037_perf_hot_path_indexes.sql").read_text(
        encoding="utf-8"
    )
    lines = mig.splitlines()
    assert lines[0].strip() == "-- 037_perf_hot_path_indexes.sql"
    assert "BEGIN;" in mig and "COMMIT;" in mig
    mig_no_comments = re.sub(r"--[^\n]*", "", mig)
    assert "CONCURRENTLY" not in mig_no_comments.upper()

    expected_indexes = [
        "idx_kg_nodes_ns_payload_ref",
        "idx_kg_edges_ns_payload_ref",
        "idx_kg_nodes_label_trgm",
        "idx_kg_edges_subj_trgm",
        "idx_kg_edges_obj_trgm",
        "idx_kg_nodes_ns_type_label",
        "idx_memories_derived_from_gin",
        "idx_memories_ns_agent_recent",
        "idx_contradictions_ns_mem_a_open",
        "idx_contradictions_ns_mem_b_open",
        "idx_event_log_time_travel",
    ]
    schema_sql = (_REPO_ROOT / "nce" / "schema.sql").read_text(encoding="utf-8")
    bootstrap_sql = (_REPO_ROOT / "nce" / "schema_bootstrap.sql").read_text(encoding="utf-8")
    for idx_name in expected_indexes:
        assert idx_name in mig, f"Missing {idx_name} in 037 migration"
        assert idx_name in schema_sql, f"Missing {idx_name} in schema.sql"
        assert idx_name in bootstrap_sql, f"Missing {idx_name} in schema_bootstrap.sql"

    assert "DROP INDEX IF EXISTS idx_event_log_ns_seq;" in mig
    assert "idx_event_log_ns_seq" not in schema_sql
    assert "'memory_shredded'" in schema_sql
    assert "'memory_shredded'" in mig


# ---------------------------------------------------------------------------
# 2. P1: tasks.py & bridge_runtime.py lightweight engine + timeouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_caches_real_engine_per_loop_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nce.tasks as tasks
    from nce.orchestrator import NCEEngine

    tasks._worker_engines.clear()
    connect_calls: list[bool | None] = []
    disconnect_calls = 0

    async def fake_connect(self: NCEEngine, *, run_startup_checks: bool = True) -> None:
        connect_calls.append(run_startup_checks)
        self.pg_pool = MagicMock()
        self.mongo_client = MagicMock()
        self.redis_client = MagicMock()

    async def fake_disconnect(self: NCEEngine) -> None:
        nonlocal disconnect_calls
        disconnect_calls += 1

    monkeypatch.setattr(NCEEngine, "connect", fake_connect)
    monkeypatch.setattr(NCEEngine, "disconnect", fake_disconnect)

    try:
        e1, cached1 = await tasks._acquire_worker_engine()
        e2, cached2 = await tasks._acquire_worker_engine()
        assert cached1 is True and cached2 is True
        assert e1 is e2
        assert connect_calls == [False]

        await tasks.close_cached_worker_engines()
        assert disconnect_calls == 1
        assert len(tasks._worker_engines) == 0
    finally:
        tasks._worker_engines.clear()


def test_tasks_redis_and_pool_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    import nce.tasks as tasks
    from nce.config import cfg

    orig = tasks._redis_client
    tasks._redis_client = None
    try:
        with patch("nce.tasks.Redis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            tasks._get_redis()
            mock_from_url.assert_called_once_with(
                cfg.REDIS_URL,
                socket_connect_timeout=cfg.REDIS_CONNECT_TIMEOUT_S,
                socket_timeout=cfg.REDIS_SOCKET_TIMEOUT_S,
            )
    finally:
        tasks._redis_client = orig

    src = inspect.getsource(tasks._store_dlq_async)
    assert "command_timeout=30" in src and "timeout=5" in src
    d365_src = inspect.getsource(tasks._dispatch_d365_event)
    assert "command_timeout=30" in d365_src and "timeout=5" in d365_src
    assert "socket_connect_timeout=cfg.REDIS_CONNECT_TIMEOUT_S" in d365_src
    assert "socket_timeout=cfg.REDIS_SOCKET_TIMEOUT_S" in d365_src


@pytest.mark.asyncio
async def test_bridge_runtime_calls_lightweight_connect_when_supported() -> None:
    from nce.bridge_runtime import resolve_stored_oauth_access_token_async

    mock_conn = object()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_conn
    mock_cm.__aexit__.return_value = False
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_cm

    connect_kwargs: list[dict] = []

    class FakeEngine:
        def __init__(self) -> None:
            self.pg_pool = mock_pool

        async def connect(self, *, run_startup_checks: bool = True) -> None:
            connect_kwargs.append({"run_startup_checks": run_startup_checks})

        async def disconnect(self) -> None:
            pass

    with (
        patch("nce.bridge_runtime.NCEEngine", FakeEngine),
        patch(
            "nce.bridge_runtime.bridge_repo.fetch_active_subscription",
            new=AsyncMock(return_value={"id": "sub-1", "provider": "sharepoint"}),
        ),
        patch(
            "nce.bridge_runtime.ensure_fresh_oauth_token",
            new=AsyncMock(return_value="tok-123"),
        ),
    ):
        tok = await resolve_stored_oauth_access_token_async(
            "sharepoint",
            client_state="cs-1",
            pg_pool=None,
        )

    assert tok == "tok-123"
    assert connect_kwargs == [{"run_startup_checks": False}]


# ---------------------------------------------------------------------------
# 3. Graph query CTE hoisting, ATMS + cascade dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_query_hoists_materialized_ctes_ahead_of_traversal() -> None:
    from datetime import datetime, timezone

    from nce.graph_query import GraphRAGTraverser

    captured: list[str] = []
    pool = MagicMock()
    conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)
    acq = AsyncMock()
    acq.__aenter__.return_value = conn
    acq.__aexit__.return_value = None
    pool.acquire = MagicMock(return_value=acq)

    async def fake_fetch(sql: str, *args: object) -> list[dict]:
        captured.append(sql)
        if "WITH RECURSIVE traversal" in sql:
            return [{"label": "A"}]
        return []

    conn.fetch = AsyncMock(side_effect=fake_fetch)

    async def _embed(_q: str) -> list[float]:
        return [0.0] * 3

    traverser = GraphRAGTraverser(pg_pool=pool, mongo_client=MagicMock(), embedding_fn=_embed)
    await traverser._bfs(
        "A",
        max_depth=2,
        namespace_id=str(uuid.uuid4()),
        as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(captured) == 2
    bfs_sql = captured[0]
    assert "memory_events AS MATERIALIZED" in bfs_sql
    assert "historical_edges AS MATERIALIZED" in bfs_sql
    # Outer CTEs must appear BEFORE WITH RECURSIVE traversal (not inside JOIN LATERAL)
    assert bfs_sql.index("memory_events AS MATERIALIZED") < bfs_sql.index(
        "WITH RECURSIVE traversal"
    )
    assert bfs_sql.index("historical_edges AS MATERIALIZED") < bfs_sql.index(
        "WITH RECURSIVE traversal"
    )
    lateral_part = bfs_sql.split("JOIN LATERAL", 1)[1]
    assert "memory_events" not in lateral_part
    assert "WITH ns" not in lateral_part
    assert "memory_events AS MATERIALIZED" in traverser._temporal_cte_prefix()


@pytest.mark.asyncio
async def test_atms_fetch_transitive_and_seed_node_ids() -> None:
    from nce.atms import evaluate_atms_intervention, fetch_transitive_derived_memories
    from nce.causal.correlation import CausalGraph

    ns = uuid.uuid4()
    mem_id = str(uuid.uuid4())
    dep_id = str(uuid.uuid4())

    conn = AsyncMock()
    conn.fetch.return_value = [{"id": uuid.UUID(dep_id)}]

    out = await fetch_transitive_derived_memories(conn, ns, mem_id)
    assert out == {dep_id}
    sql = conn.fetch.call_args[0][0]
    assert "unnest($2::uuid[])" in sql
    assert "dc.id::text" in sql and "dc.id::uuid" in sql
    assert "m.valid_to IS NULL" in sql

    empty = await evaluate_atms_intervention(conn, ns, [])
    assert empty == set()

    with patch(
        "nce.causal.correlation.CausalGraph.load_from_db",
        new=AsyncMock(return_value=CausalGraph()),
    ) as mock_load:
        res = await evaluate_atms_intervention(conn, ns, [mem_id])
    assert dep_id in res
    mock_load.assert_awaited_once_with(conn, ns, seed_node_ids=[mem_id])


def test_cognitive_and_me_app_removed_n_plus_1_loops() -> None:
    import nce.me_app as me_app
    import nce.orchestrators.cognitive as cognitive

    cog_src = inspect.getsource(cognitive)
    me_src = inspect.getsource(me_app)
    assert "while todo and len(visited)" not in cog_src
    assert "while todo and len(visited)" not in me_src


# ---------------------------------------------------------------------------
# 4. Salience batch, tracked task + semaphore, pooled httpx clients
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_salience_reinforce_batch_and_semaphore() -> None:
    import nce.salience as salience
    import nce.semantic_search as ss

    conn = AsyncMock()
    m1 = str(uuid.uuid4())
    m2 = str(uuid.uuid4())
    ns = str(uuid.uuid4())

    await salience.reinforce_batch(conn, [m1, m2, m1], "agent-1", ns, delta=0.05)
    conn.execute.assert_awaited_once()
    sql, ids_arg, agent_arg, ns_arg, delta_arg = conn.execute.call_args[0]
    assert "UNNEST($1::uuid[])" in sql
    assert ids_arg == [m1, m2]
    assert agent_arg == "agent-1"
    assert ns_arg == ns
    assert delta_arg == 0.05

    # _fire_reinforcement uses reinforce_batch (1 execute for N results) and bounds concurrency
    conn2 = AsyncMock()

    @asynccontextmanager
    async def fake_scoped(_pool, _ns):
        yield conn2

    with patch("nce.semantic_search.scoped_pg_session", fake_scoped):
        await ss._fire_reinforcement(
            MagicMock(),
            [{"memory_id": m1}, {"memory_id": m2}],
            "agent-1",
            ns,
            0.05,
        )
    conn2.execute.assert_awaited_once()
    assert ss._REINFORCE_SEMAPHORE._value == 4


@pytest.mark.asyncio
async def test_nli_async_client_is_pooled_with_connect_timeout() -> None:
    import nce.semantic_search as ss

    ss._nli_async_clients.clear()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"score": 0.2})

    transport = httpx.MockTransport(handler)
    pooled = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0, connect=5.0))
    loop = asyncio.get_running_loop()
    ss._nli_async_clients[loop] = pooled

    try:
        with patch(
            "nce.embeddings.validated_cognitive_base_url", return_value="http://sidecar.local"
        ):
            scores = await ss.check_nli_relevance_batch([("p1", "h1"), ("p2", "h2"), ("p3", "h3")])
        assert len(scores) == 3
        assert all(abs(s - 0.8) < 1e-6 for s in scores)
        assert len(calls) == 3
    finally:
        await pooled.aclose()
        ss._nli_async_clients.clear()


def test_sync_http_clients_pooled_in_embeddings_and_graph_extractor() -> None:
    import nce.embeddings as emb
    import nce.graph_extractor as ge

    c1 = emb._get_sync_http_client()
    c2 = emb._get_sync_http_client()
    assert c1 is c2
    assert c1.timeout.connect == 5.0

    g1 = ge._get_spacy_http_client()
    g2 = ge._get_spacy_http_client()
    assert g1 is g2
    assert g1.timeout.connect == 5.0


# ---------------------------------------------------------------------------
# 5. Replay MinIO cache & SQL fixes, REST UUID lookup, single-pass OCR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_minio_cache_and_sql_queries() -> None:
    import nce.replay as replay

    replay._MINIO_CLIENT = None
    with patch("nce.replay.Minio") as mock_minio:
        mock_minio.return_value = MagicMock()
        a = replay._make_minio()
        b = replay._make_minio()
        assert a is b
        mock_minio.assert_called_once()
        http_client = mock_minio.call_args.kwargs["http_client"]
        assert http_client.connection_pool_kw.get("maxsize") == 16
        timeout_obj = http_client.connection_pool_kw.get("timeout")
        assert timeout_obj.connect_timeout == 2.0
        assert timeout_obj.read_timeout == 5.0
    replay._MINIO_CLIENT = None

    edge_src = inspect.getsource(replay._handle_edge_confidence_floored)
    assert "(params->>'memory_id')::uuid = ANY($2::uuid[])" in edge_src

    fork_src = inspect.getsource(replay.ForkedReplay)
    recon_src = inspect.getsource(replay.ReconstructiveReplay)
    assert "params @> jsonb_build_object('replay_run_id', $2::text)" in fork_src
    assert "params @> jsonb_build_object('replay_run_id', $2::text)" in recon_src


@pytest.mark.asyncio
async def test_rest_resolve_canonical_avoids_id_text_cast() -> None:
    from types import SimpleNamespace

    from nce.resource_surface.rest import resolve_canonical_identifier

    spec = SimpleNamespace(
        tenant_scope="graph",
        node_type="SYSTEM",
        secondary_tables=(),
        table_name=None,
        id_field="id",
    )
    ns = uuid.uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {"label": "node-alpha"}

    # Non-UUID candidate -> queries label = $3 only (no id::text)
    lbl = await resolve_canonical_identifier(conn, spec, ns, "node-alpha", False)
    assert lbl == "node-alpha"
    sql_non_uuid = conn.fetchrow.call_args[0][0]
    assert "id::text" not in sql_non_uuid
    assert "label = $3" in sql_non_uuid

    # UUID candidate -> queries label = $3 OR id = $4::uuid (no id::text)
    conn.fetchrow.reset_mock()
    uid = str(uuid.uuid4())
    lbl2 = await resolve_canonical_identifier(conn, spec, ns, uid, False)
    assert lbl2 == "node-alpha"
    sql_uuid = conn.fetchrow.call_args[0][0]
    assert "id::text" not in sql_uuid
    assert "id = $4::uuid" in sql_uuid


@pytest.mark.asyncio
async def test_ocr_single_pass_reconstruction() -> None:
    import sys
    from types import SimpleNamespace

    from nce.extractors.ocr import ocr_pil_image

    fake_data = {
        "text": ["", "Hello", "world", "", "Next", "line", "New", "para"],
        "conf": ["-1", "95", "90", "-1", "85", "88", "92", "94"],
        "block_num": [1, 1, 1, 1, 1, 1, 2, 2],
        "par_num": [1, 1, 1, 1, 1, 1, 1, 1],
        "line_num": [1, 1, 1, 2, 2, 2, 1, 1],
    }
    calls = {"data": 0, "string": 0}

    def fake_image_to_data(*_args, **_kwargs):
        calls["data"] += 1
        return fake_data

    def fake_image_to_string(*_args, **_kwargs):
        calls["string"] += 1
        return "SHOULD NOT BE CALLED"

    fake_pytesseract = SimpleNamespace(
        Output=SimpleNamespace(DICT="dict"),
        image_to_data=fake_image_to_data,
        image_to_string=fake_image_to_string,
    )

    with patch.dict(sys.modules, {"pytesseract": fake_pytesseract}):
        text, warnings = await ocr_pil_image(object())

    assert calls["data"] == 1
    assert calls["string"] == 0
    assert warnings == []
    assert text == "Hello world\nNext line\n\nNew para"
