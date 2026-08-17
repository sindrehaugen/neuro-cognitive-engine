import asyncio
import os
import time
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from nce import MemoryPayload, NCEEngine

# Tests require live DB containers (MongoDB, Redis, PostgreSQL).
# Skip gracefully when containers are not available.


def _check_container(env_var: str, host: str, port: int, label: str) -> bool:
    """Return True if the container at host:port is reachable."""
    import socket
    from urllib.parse import urlparse

    url = os.getenv(env_var)
    if url:
        try:
            if "://" in url:
                parsed = urlparse(url)
                host = parsed.hostname or host
                port = parsed.port or port
            else:
                parts = url.split(":")
                host = parts[0]
                if len(parts) > 1:
                    try:
                        port = int(parts[1].split("/")[0])
                    except ValueError:
                        pass
        except Exception:
            pass
    try:
        sock = socket.create_connection((host, port), timeout=1)
        sock.close()
        return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


_MONGO_OK = _check_container("MONGO_URI", "127.0.0.1", 27017, "MongoDB")
_PG_OK = _check_container("PG_DSN", "127.0.0.1", 5432, "PostgreSQL")
_REDIS_OK = _check_container("REDIS_URL", "127.0.0.1", 6379, "Redis")
_ALL_CONTAINERS = _MONGO_OK and _PG_OK and _REDIS_OK


# ---------------------------------------------------------------------------
# Unit tests — no live containers required
# ---------------------------------------------------------------------------


class TestEnsureUuid:
    """Regression suite for NCEEngine._ensure_uuid (Phase 2 Issue #1).

    Verifies that string UUIDs are correctly parsed to UUID objects, UUID
    objects are returned as-is, and None inputs return None.  The critical
    regression case is that a string input must NEVER silently produce the
    string "None" that would corrupt RLS context.
    """

    def setup_method(self):
        self.engine = NCEEngine()

    def test_none_returns_none(self):
        """None input must return None, not UUID('None')."""
        result = self.engine._ensure_uuid(None)
        assert result is None

    def test_uuid_object_returned_unchanged(self):
        """UUID objects must pass through without wrapping."""
        uid = uuid4()
        result = self.engine._ensure_uuid(uid)
        assert result is uid

    def test_string_uuid_is_parsed_to_uuid_object(self):
        """String UUIDs must be converted — NOT silently passed as strings."""
        uid = uuid4()
        uid_str = str(uid)
        result = self.engine._ensure_uuid(uid_str)
        assert isinstance(result, UUID), (
            f"Expected UUID object, got {type(result).__name__!r}. "
            "This means _ensure_uuid is NOT converting strings — "
            "RLS context would receive a bare string, not a UUID."
        )
        assert result == uid

    def test_string_uuid_never_produces_string_none(self):
        """The historical bug: result was str('None') when val was a string.
        This test is the primary regression guard for Phase 2 Issue #1.
        """
        uid = uuid4()
        result = self.engine._ensure_uuid(str(uid))
        assert str(result) != "None", (
            "RLS namespace context would be set to 'None' — "
            "this is the exact P0 regression this test guards against."
        )

    def test_invalid_string_raises_value_error(self):
        """Non-UUID strings must raise ValueError immediately, not silently pass."""
        with pytest.raises(ValueError):
            self.engine._ensure_uuid("not-a-uuid")


class TestSagaMetricsOnFailure:
    """Regression suite for SagaMetrics.on_saga_failure (Phase 2 Issue #2).

    Verifies that the callback safely handles missing kwargs so the metric
    is always emitted — omitting step_name must not raise KeyError.
    """

    def test_on_saga_failure_empty_kwargs_does_not_raise(self):
        """Calling on_saga_failure with no kwargs must never raise KeyError."""
        from nce.observability import SagaMetrics

        exc = RuntimeError("saga exploded")
        # Must not raise — this was the bug
        SagaMetrics.on_saga_failure(exc)

    def test_on_saga_failure_missing_step_name_uses_default(self, monkeypatch):
        """When step_name is absent the metric stage defaults to 'unknown'."""
        from nce.config import cfg
        from nce.observability import SAGA_FAILURES, SagaMetrics

        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)

        def _fake_inc():
            # We need to capture the labels tuple; monkeypatch the .inc() method
            pass

        # Capture what stage label gets set
        stages: list[str] = []
        original_labels = SAGA_FAILURES.labels

        def _capture_labels(**kw):
            stages.append(kw.get("stage", "<none>"))
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_FAILURES, "labels", _capture_labels)

        exc = ValueError("oops")
        SagaMetrics.on_saga_failure(exc)  # no step_name kwarg

        # The stage must be the safe default "unknown", not a KeyError crash
        assert stages, "SAGA_FAILURES.labels was never called — metric not emitted"
        assert stages[0] == "unknown", (
            f"Expected stage='unknown' when step_name omitted, got {stages[0]!r}"
        )

    def test_on_saga_failure_with_step_name(self, monkeypatch):
        """When step_name is provided it is forwarded as the metric stage."""
        from nce.config import cfg
        from nce.observability import SAGA_FAILURES, SagaMetrics

        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)

        stages: list[str] = []
        original_labels = SAGA_FAILURES.labels

        def _capture_labels(**kw):
            stages.append(kw.get("stage", "<none>"))
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_FAILURES, "labels", _capture_labels)

        SagaMetrics.on_saga_failure(RuntimeError("x"), step_name="pg_insert")
        assert stages == ["pg_insert"], f"Unexpected stage: {stages}"

    def test_saga_metrics_context_fires_on_failure_callback(self):
        """SagaMetrics.__exit__ invokes the on_failure callback on exception."""
        from nce.observability import SagaMetrics

        fired: list[BaseException] = []

        def _cb(exc):
            fired.append(exc)

        with pytest.raises(RuntimeError):
            with SagaMetrics("test_op", on_failure=_cb):
                raise RuntimeError("deliberately broken")

        assert len(fired) == 1, "on_failure callback was not called"
        assert isinstance(fired[0], RuntimeError)

    def test_saga_metrics_context_does_not_fire_on_success(self):
        """on_failure must NOT be called when the block completes normally."""
        from nce.observability import SagaMetrics

        fired: list[BaseException] = []

        def _cb(exc):
            fired.append(exc)

        with SagaMetrics("test_op", on_failure=_cb):
            pass  # no exception

        assert fired == [], "on_failure fired on a successful saga block"


class TestSagaRollbackMocked:
    """Mocked unit tests for Saga rollbacks (e.g. Postgres timeout)."""

    @pytest.mark.asyncio
    async def test_postgres_timeout_triggers_mongo_and_pg_rollback(self, monkeypatch):
        """Simulate a PG timeout or query cancel exception during store_memory transactional PG write.

        Assert that:
        1. Mongo delete_one is called to remove the orphaned document.
        2. PG safety cleanup is triggered.
        3. The original PG timeout exception is propagated (not masked).
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

        from nce.models import AssertionType, MemoryType, StoreMemoryRequest
        from nce.orchestrator import NCEEngine

        # Setup engine and database mocks
        engine = NCEEngine()

        # Mock Mongo episodes collection
        collection = AsyncMock()
        fake_inserted_id = "507f1f77bcf86cd799439011"

        class FakeInsertResult:
            def __init__(self, inserted_id):
                self.inserted_id = inserted_id

        collection.insert_one = AsyncMock(return_value=FakeInsertResult(fake_inserted_id))
        collection.delete_one = AsyncMock()

        db = MagicMock()
        type(db).episodes = PropertyMock(return_value=collection)
        engine.mongo_client = MagicMock()
        type(engine.mongo_client).memory_archive = PropertyMock(return_value=db)

        # Mock PG Pool and connection
        class FakeConn:
            def __init__(self):
                self.fetchrow = AsyncMock(return_value={"id": "saga-123", "metadata": "{}"})
                self.fetch = AsyncMock(return_value=[{"id": "model-1"}])
                self.fetchval = AsyncMock()
                self.execute = AsyncMock()
                self.executemany = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def transaction(self):
                return self

        conn = FakeConn()
        # Mock PG timeout / query cancellation on PG write (e.g. during _embed_and_insert_vectors inside pg session)
        conn.fetchval.side_effect = asyncio.TimeoutError("Postgres timeout during vector insert")

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=conn)
        engine.pg_pool = pool
        engine.redis_client = AsyncMock()

        # Mock scoped_pg_session
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _scoped(_pool, _ns):
            yield conn

        monkeypatch.setattr("nce.orchestrators.memory.scoped_pg_session", _scoped)

        # Setup request payload
        payload = StoreMemoryRequest(
            namespace_id="00000000-0000-4000-8000-000000000001",
            agent_id="test-agent",
            content="Test content for mocked rollback",
            summary="Test summary",
            heavy_payload="Heavy payload content",
            memory_type=MemoryType.episodic,
            assertion_type=AssertionType.fact,
            metadata={"user_id": "user-1", "session_id": "sess-1"},
            check_contradictions=False,
        )

        # Patch expensive imports / pipelines
        _P_EMBED = "nce.orchestrator._embeddings.embed_batch"
        _P_GRAPH = "nce.graph_extractor.extract"
        _P_PII = "nce.pii.process"

        class FakePiiResult:
            def __init__(self):
                self.sanitized_text = "sanitized text"
                self.redacted = False
                self.entities_found = 0
                self.vault_entries = []

        with patch(_P_EMBED, return_value=[[0.1] * 768]):
            with patch(_P_GRAPH, return_value=([], [])):
                with patch(_P_PII, return_value=FakePiiResult()):
                    with pytest.raises(
                        asyncio.TimeoutError, match="Postgres timeout during vector insert"
                    ):
                        await engine.store_memory(payload)

        # Verify MongoDB delete was called to clean up the episode
        collection.delete_one.assert_called_once_with({"_id": fake_inserted_id})

        # Verify PG safety cleanup was attempted (since pg_committed is False, it goes to the elif inserted_mongo_id block)
        # It should delete from kg_edges, kg_nodes, and update memories
        execute_calls = [c[0][0] for c in conn.execute.call_args_list]
        pg_sql = " ".join(execute_calls)
        assert "DELETE FROM kg_edges" in pg_sql
        assert "DELETE FROM kg_nodes" in pg_sql
        assert "UPDATE memories" in pg_sql


class TestA2AScopeViolationFailingPaths:
    """Mocked unit tests covering A2A scope violations in failing path MCP tool executions.

    Verifies that when a handler raises an A2AScopeViolationError, it is properly mapped
    to the JSON-RPC error code -32011 (MCP_A2A_SCOPE_VIOLATION).
    """

    @pytest.mark.asyncio
    async def test_a2a_scope_violation_returns_jsonrpc_error(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from nce.a2a import A2AScopeViolationError
        from nce.mcp_errors import mcp_handler
        from nce.mcp_stdio_dispatch import execute_call_tool
        from nce.orchestrator import NCEEngine
        from nce.tool_registry import TOOL_REGISTRY

        # Setup engine mock
        engine = MagicMock(spec=NCEEngine)
        engine.redis_client = AsyncMock()
        engine.redis_client.get = AsyncMock(return_value=None)
        engine.redis_client.hexists = AsyncMock(return_value=False)
        engine.pg_pool = MagicMock()

        # Mock target tool handler (e.g. semantic_search) to raise A2AScopeViolationError
        @mcp_handler
        async def mock_handler(eng, args):
            raise A2AScopeViolationError("Access denied: target namespace not shared.")

        # Temporarily mock the handler for semantic_search in TOOL_REGISTRY
        original_spec = TOOL_REGISTRY.get("semantic_search")
        assert original_spec is not None

        # Create a modified spec with the mocked handler
        from dataclasses import replace

        mocked_spec = replace(original_spec, handler=mock_handler)

        monkeypatch.setitem(TOOL_REGISTRY, "semantic_search", mocked_spec)

        # Disable quota checks to simplify the test path
        monkeypatch.setattr("nce.mcp_stdio_rpc._consume_quota_for_mcp_tool", AsyncMock())

        args = {
            "namespace_id": "00000000-0000-4000-8000-000000000001",
            "agent_id": "test-agent",
            "query": "hello",
            "limit": 5,
        }

        # Execute call tool
        results = await execute_call_tool(engine, "semantic_search", args)

        assert len(results) == 1
        response_text = results[0].text

        import json

        response_data = json.loads(response_text)
        assert "error" in response_data
        assert response_data["error"]["code"] == -32011  # MCP_A2A_SCOPE_VIOLATION
        assert "Scope violation" in response_data["error"]["message"]
        assert "Access denied" in response_data["error"]["data"]["reason"]


# ---------------------------------------------------------------------------
# Integration tests — require live MongoDB + PostgreSQL + Redis containers
# ---------------------------------------------------------------------------

_skip_no_containers = pytest.mark.skipif(
    not _ALL_CONTAINERS,
    reason="Integration tests require live MongoDB, PostgreSQL, and Redis containers",
)


@pytest_asyncio.fixture
async def engine():
    eng = NCEEngine()
    await eng.connect()
    yield eng
    await eng.disconnect()


@_skip_no_containers
@pytest.mark.asyncio
async def test_store_and_recall(engine, namespace_id):
    """store_memory → get_recent_context (Redis hit)"""
    test_id = str(uuid4())
    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="test-agent",
        content="NCE uses Redis as working memory for sub-millisecond recall.",
        summary="NCE uses Redis as working memory for sub-millisecond recall.",
        heavy_payload="Full conversation transcript placeholder for test T1.",
        metadata={"user_id": test_id, "session_id": test_id},
    )
    res = await engine.store_memory(payload)
    mongo_id = res.get("payload_ref")
    assert mongo_id, "No mongo_id returned"

    cached = await engine.recall_memory(str(namespace_id), test_id, test_id)
    assert cached == payload.summary, f"Cache mismatch: {cached!r}"


@_skip_no_containers
@pytest.mark.asyncio
async def test_semantic_search(engine, namespace_id):
    """semantic_search returns stored document"""
    test_id = str(uuid4())
    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="test-agent",
        content="PostgreSQL pgvector stores 768-dimensional cosine embeddings.",
        summary="PostgreSQL pgvector stores 768-dimensional cosine embeddings.",
        heavy_payload="Full transcript: pgvector enables cosine similarity search on 768-dim vectors.",
        metadata={"user_id": test_id, "session_id": test_id},
    )
    await engine.store_memory(payload)

    results_sr = await engine.semantic_search(
        "vector database embeddings",
        str(namespace_id),
        limit=3,
    )
    assert len(results_sr) > 0, "No results returned"
    assert "pgvector" in str(results_sr[0].get("raw_data", "")), (
        f"Expected pgvector in top result, got: {results_sr[0].get('raw_data', '')!r}"
    )


@_skip_no_containers
@pytest.mark.asyncio
async def test_index_and_search_code(engine, namespace_id):
    """index_code_file + search_codebase finds the function"""
    from nce.models import IndexCodeFileRequest

    run_id = str(int(time.time()))
    sample_code = (
        "def calculate_embedding_distance(vec_a, vec_b):\n    pass\nclass VectorStore:\n    pass\n"
    )
    req = IndexCodeFileRequest(
        filepath=f"test_fixtures/vector_utils_{run_id}.py",
        raw_code=sample_code,
        language="python",
        namespace_id=namespace_id,
    )
    result = await engine.index_code_file(req)
    assert result["status"] == "indexed", f"Unexpected status: {result}"

    code_results = await engine.search_codebase(
        "cosine distance between vectors",
        namespace_id=str(namespace_id),
        top_k=3,
    )
    assert len(code_results) > 0, "No code results returned"
    assert any("calculate_embedding_distance" in r.get("name", "") for r in code_results)


@_skip_no_containers
@pytest.mark.asyncio
async def test_change_detection(engine, namespace_id):
    """Re-indexing unchanged file returns status=skipped"""
    from nce.models import IndexCodeFileRequest

    code = "def noop(): pass\n"
    fp = "test_fixtures/noop.py"
    req1 = IndexCodeFileRequest(
        filepath=fp,
        raw_code=code,
        language="python",
        namespace_id=namespace_id,
    )
    await engine.index_code_file(req1)
    req2 = IndexCodeFileRequest(
        filepath=fp,
        raw_code=code,
        language="python",
        namespace_id=namespace_id,
    )
    result2 = await engine.index_code_file(req2)
    assert result2["status"] == "skipped", f"Expected skipped, got: {result2}"


@_skip_no_containers
@pytest.mark.asyncio
async def test_graph_search(engine, namespace_id):
    """store_memory extracts KG entities; graph_search returns a subgraph"""
    test_id = str(uuid4())
    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="test-agent",
        content="MongoDB stores raw data. Redis connects to the cache layer.",
        summary="MongoDB stores raw data. Redis connects to the cache layer.",
        heavy_payload="Heavy payload for T5.",
        metadata={"user_id": test_id, "session_id": test_id},
    )
    await engine.store_memory(payload)

    from nce.models import GraphSearchRequest

    req = GraphSearchRequest(
        namespace_id=namespace_id,
        query="MongoDB storage",
        max_depth=2,
    )
    subgraph = await engine.graph_search(req)
    assert "nodes" in subgraph, "No nodes key in subgraph"
    assert "edges" in subgraph, "No edges key in subgraph"
    assert len(subgraph["nodes"]) > 0, "Subgraph has no nodes"


@_skip_no_containers
@pytest.mark.asyncio
async def test_rollback(engine, namespace_id):
    """Forcing a PG failure must leave MongoDB clean"""
    from motor.motor_asyncio import AsyncIOMotorClient

    test_id = str(uuid4())

    db = AsyncIOMotorClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")).memory_archive
    before_count = await db.episodes.count_documents({})

    real_pool = engine.pg_pool
    engine.pg_pool = None
    await engine._ensure_memory()
    real_mem_pool = engine.memory.pg_pool
    engine.memory.pg_pool = None
    try:
        await engine.store_memory(
            MemoryPayload(
                namespace_id=namespace_id,
                agent_id="test-agent",
                content="This write must be rolled back.",
                summary="This write must be rolled back.",
                heavy_payload="Rollback test payload.",
                metadata={"user_id": test_id, "session_id": test_id},
            )
        )
        pytest.fail("Exception was NOT raised — rollback did not trigger")
    except Exception:
        pass
    finally:
        engine.pg_pool = real_pool
        engine.memory.pg_pool = real_mem_pool

    after_count = await db.episodes.count_documents({})
    assert after_count == before_count, (
        f"MongoDB grew by {after_count - before_count} — orphan NOT cleaned up"
    )


@_skip_no_containers
@pytest.mark.asyncio
async def test_post_commit_failure_saga_recovery(engine, namespace_id, monkeypatch):
    """If a crash (BaseException) occurs post-PG commit:
    1. MongoDB document is preserved.
    2. Saga is left in 'pg_committed' state.
    3. Cron recovery tick transitions it to 'completed' after aging.
    """
    from unittest.mock import AsyncMock

    from motor.motor_asyncio import AsyncIOMotorClient

    from nce.cron import _saga_recovery_tick
    from nce.db_utils import scoped_pg_session

    test_id = str(uuid4())

    db = AsyncIOMotorClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")).memory_archive
    before_count = await db.episodes.count_documents({})

    # Mock working memory caching to simulate an unhandled exit (BaseException) post-commit
    monkeypatch.setattr(
        engine.memory,
        "_cache_working_memory_redis",
        AsyncMock(side_effect=KeyboardInterrupt("Simulated exit")),
    )

    payload = MemoryPayload(
        namespace_id=namespace_id,
        agent_id="test-agent",
        content="Saga post-commit crash test.",
        summary="Saga post-commit crash test.",
        heavy_payload="Durable data payload.",
        metadata={"user_id": test_id, "session_id": test_id},
    )

    with pytest.raises(KeyboardInterrupt):
        await engine.store_memory(payload)

    # 1. MongoDB document is NOT deleted (since PG committed)
    after_count = await db.episodes.count_documents({})
    assert after_count == before_count + 1

    # 2. Retrieve saga and verify it is in 'pg_committed' state
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, state FROM saga_execution_log WHERE namespace_id = $1",
            namespace_id,
        )
        assert row is not None
        assert row["state"] == "pg_committed"
        saga_id = row["id"]

        # Manually age the saga log so the recovery cron processes it
        await conn.execute(
            "UPDATE saga_execution_log SET created_at = now() - interval '10 minutes', updated_at = now() - interval '10 minutes' WHERE id = $1",
            saga_id,
        )

    # 3. Trigger cron recovery tick
    await _saga_recovery_tick(engine.pg_pool)

    # 4. Verify saga is now 'completed'
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            "SELECT state FROM saga_execution_log WHERE id = $1",
            saga_id,
        )
        assert row["state"] == "completed"


class TestChaosGraphRAG:
    """Chaos tests for GraphRAG bottlenecking and circuit breaker."""

    @pytest.mark.asyncio
    async def test_graph_query_circuit_breaker_trips(self, monkeypatch):
        """Simulate database timeouts to verify the GraphRAG circuit breaker trips and fails fast."""
        from unittest.mock import AsyncMock, MagicMock

        from nce.graph_query import GraphRAGTraverser
        from nce.providers import LLMCircuitOpenError

        # Create a mock traverser
        mock_pg_pool = MagicMock()
        # Mock pg_pool.acquire to raise TimeoutError to simulate DB failure
        mock_pg_pool.acquire = MagicMock(side_effect=asyncio.TimeoutError("DB Timeout"))

        traverser = GraphRAGTraverser(
            pg_pool=mock_pg_pool,
            mongo_client=MagicMock(),
            embedding_fn=AsyncMock(return_value=[0.1] * 768),
            max_concurrent_searches=10,
        )
        # Set failure threshold to 3 for faster testing
        traverser.circuit_breaker.failure_threshold = 3

        # First 3 attempts fail due to simulated timeout
        for i in range(3):
            with pytest.raises(asyncio.TimeoutError):
                await traverser.search("query", namespace_id="00000000-0000-4000-8000-000000000001")

        # 4th attempt should fail fast with LLMCircuitOpenError
        with pytest.raises(LLMCircuitOpenError) as exc_info:
            await traverser.search("query", namespace_id="00000000-0000-4000-8000-000000000001")

        assert "circuit breaker is OPEN" in str(exc_info.value)
        assert traverser.circuit_breaker.state.value == "open"

    @pytest.mark.asyncio
    async def test_a2a_server_circuit_breaker_503(self, monkeypatch):
        """Verify A2A server maps LLMCircuitOpenError to a 503 JSON-RPC error."""
        from unittest.mock import AsyncMock, MagicMock

        from starlette.requests import Request

        import nce.a2a_server as a2a_server
        from nce.auth import NamespaceContext
        from nce.providers import LLMCircuitOpenError

        # Mock requests.json()
        mock_req = MagicMock(spec=Request)
        mock_req.state = MagicMock()
        mock_req.state.namespace_ctx = NamespaceContext(
            namespace_id=UUID("00000000-0000-4000-8000-000000000001"),
            agent_id="test-agent",
        )
        mock_req.json = AsyncMock(
            return_value={
                "id": "task-1",
                "skill": "find_related_decisions",
                "params": {"query": "test", "namespace_id": "00000000-0000-4000-8000-000000000001"},
            }
        )

        # Mock engine
        mock_engine = MagicMock()
        mock_engine.redis_client = AsyncMock()
        mock_engine.pg_pool = MagicMock()
        a2a_server._engine = mock_engine

        # Mock _dispatch_skill to raise LLMCircuitOpenError
        async def mock_dispatch(*args, **kwargs):
            raise LLMCircuitOpenError("Circuit open", provider="test", status_code=503)

        monkeypatch.setattr(a2a_server, "_dispatch_skill", mock_dispatch)

        resp = await a2a_server.tasks_send(mock_req)
        assert resp.status_code == 503

        import json

        body = json.loads(resp.body.decode())
        assert body["error"]["code"] == -32016
        assert "Service temporarily degraded" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_a2a_server_memory_protection_503(self, monkeypatch):
        """Verify Uvicorn memory limit is enforced and returns a 503 response."""
        from unittest.mock import MagicMock

        from starlette.requests import Request

        import nce.a2a_server as a2a_server
        from nce.config import cfg

        # Mock requests
        mock_req = MagicMock(spec=Request)
        mock_req.state = MagicMock()

        # Mock engine and memory tracking
        mock_engine = MagicMock()
        a2a_server._engine = mock_engine

        monkeypatch.setattr(cfg, "NCE_A2A_MEMORY_LIMIT_MB", 100.0, raising=False)
        monkeypatch.setattr(a2a_server, "_get_process_memory_mb", lambda: 150.0)

        resp = await a2a_server.tasks_send(mock_req)
        assert resp.status_code == 503

        import json

        body = json.loads(resp.body.decode())
        assert body["error"]["code"] == -32017
        assert "Resource exhaustion" in body["error"]["message"]


class TestChaosSwarm:
    """Chaos tests for Swarm simulations and Redis connectivity limits."""

    @pytest.mark.asyncio
    async def test_concurrent_a2a_negotiations(self, monkeypatch):
        """Simulate thousands of concurrent A2A token verification / caching requests under heavy load."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from starlette.requests import Request

        import nce.a2a_server as a2a_server
        from nce.auth import NamespaceContext

        mock_engine = MagicMock()
        mock_engine.redis_client = AsyncMock()
        mock_engine.redis_client.set = AsyncMock()
        a2a_server._engine = mock_engine

        # Mock _dispatch_skill to return success
        monkeypatch.setattr(
            a2a_server, "_dispatch_skill", AsyncMock(return_value={"success": True})
        )

        # Create concurrent requests
        tasks = []
        for i in range(100):
            mock_req = MagicMock(spec=Request)
            mock_req.state = MagicMock()
            mock_req.state.namespace_ctx = NamespaceContext(
                namespace_id=UUID("00000000-0000-4000-8000-000000000001"),
                agent_id=f"agent-{i}",
            )
            mock_req.json = AsyncMock(
                return_value={
                    "id": f"task-{i}",
                    "skill": "get_cognitive_state",
                    "params": {
                        "namespace_id": "00000000-0000-4000-8000-000000000001",
                        "agent_id": f"agent-{i}",
                    },
                }
            )
            tasks.append(a2a_server.tasks_send(mock_req))

        responses = await asyncio.gather(*tasks)
        for resp in responses:
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_redis_connection_failure_handling(self, monkeypatch):
        """Verify that Redis connection failures do not crash the A2A endpoint."""
        from unittest.mock import AsyncMock, MagicMock

        import redis.exceptions
        from starlette.requests import Request

        import nce.a2a_server as a2a_server
        from nce.auth import NamespaceContext

        mock_engine = MagicMock()
        # Mock Redis client to raise ConnectionError on writes
        mock_redis = AsyncMock()
        mock_redis.set.side_effect = redis.exceptions.ConnectionError("Redis connection lost")
        mock_engine.redis_client = mock_redis
        a2a_server._engine = mock_engine

        # Mock _dispatch_skill to return success
        monkeypatch.setattr(
            a2a_server, "_dispatch_skill", AsyncMock(return_value={"success": True})
        )

        mock_req = MagicMock(spec=Request)
        mock_req.state = MagicMock()
        mock_req.state.namespace_ctx = NamespaceContext(
            namespace_id=UUID("00000000-0000-4000-8000-000000000001"),
            agent_id="test-agent",
        )
        mock_req.json = AsyncMock(
            return_value={
                "id": "task-redis-fail",
                "skill": "get_cognitive_state",
                "params": {
                    "namespace_id": "00000000-0000-4000-8000-000000000001",
                    "agent_id": "test-agent",
                },
            }
        )

        # Request should succeed because Redis failure is caught and falls back to in-memory dict
        resp = await a2a_server.tasks_send(mock_req)
        assert resp.status_code == 200
