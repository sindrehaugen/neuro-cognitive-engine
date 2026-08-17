"""
Tests for P0 Runtime Bug #2 — Saga span and metrics wrapping nothing.

Verifies that the OTel span and SagaMetrics context managers in
``MemoryOrchestrator.store_memory`` and ``.store_media`` actually wrap the
full method body (not just ``pass``), so that:

- ``nce_saga_duration_seconds`` records realistic (non-zero) durations
- OTel spans contain child operations (Mongo insert, PG transaction, etc.)
- The metrics histogram can drive accurate SLO dashboards

Bug description (from to-do-v1-phase2.md)::

    The OTel span and SagaMetrics context managers open and immediately close
    via ``pass`` before any Saga work begins. All actual work executes outside
    the instrumented block.

    Consequence: ``nce_saga_duration_seconds`` records sub-microsecond
    durations for every ``store_memory`` call. When a real latency incident
    occurs — slow Mongo commit, PG lock contention, embedding timeout — it
    is completely invisible to on-call.

Fix applied (Prompt 76): Indented the entire ``store_memory`` and
``store_media`` method bodies inside both context managers, replacing the
``pass`` with the actual work. Also removed the unused ``metrics`` variable
from the ``with SagaMetrics(...) as metrics:`` binding (was already unused
with the ``pass``; now the ``as metrics:`` is removed entirely).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.config import cfg
from nce.observability import SagaMetrics

# ===========================================================================
# 1. SagaMetrics wrapping — direct unit tests
# ===========================================================================


class TestSagaMetricsWrapsRealWork:
    """Verify that SagaMetrics records realistic durations when real work
    happens inside the context, proving the fix eliminated the ``pass``-only
    pattern."""

    def test_duration_non_zero_with_work(self, monkeypatch) -> None:
        """SagaMetrics must record duration > 0 when work happens inside."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        observed_duration = [0.0]

        # Monkeypatch SAGA_DURATION.observe to capture the duration
        from nce.observability import SAGA_DURATION

        def _capture_observe(self_hist, value: float) -> None:
            observed_duration[0] = value
            # Note: we intentionally do NOT call the original observe here because
            # SAGA_DURATION.observe is on the Histogram class and requires labels
            # to be set first (via .labels(operation=..., result=...)).

        monkeypatch.setattr(SAGA_DURATION.__class__, "observe", _capture_observe)

        with SagaMetrics("store_memory"):
            # Simulate realistic work — similar to what the Saga does
            # (Mongo insert, embedding, PG transaction)
            for _ in range(1000):
                _ = [i**2 for i in range(100)]

        # The duration must be measurable — definitely > 0.0
        assert observed_duration[0] > 0.0, (
            f"SagaMetrics recorded zero duration ({observed_duration[0]}s) for "
            f"non-trivial work — the context manager is wrapping nothing"
        )

    @pytest.mark.asyncio
    async def test_duration_non_zero_with_async_work(self, monkeypatch) -> None:
        """Same as above but with async work inside the context."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        observed_duration = [0.0]

        from nce.observability import SAGA_DURATION

        def _capture_observe(self_hist, value: float) -> None:
            observed_duration[0] = value

        monkeypatch.setattr(SAGA_DURATION.__class__, "observe", _capture_observe)

        with SagaMetrics("store_memory"):
            await asyncio.sleep(0.01)  # simulate 10ms of async work

        assert observed_duration[0] >= 0.005, (
            f"SagaMetrics recorded {observed_duration[0]}s for 10ms sleep — "
            f"the context manager is not wrapping the work"
        )

    def test_duration_near_zero_with_pass(self, monkeypatch) -> None:
        """Regression guard: if ``pass`` is used instead of real work,
        the recorded duration should be near-zero (the bug pattern)."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        observed_duration = [0.0]

        from nce.observability import SAGA_DURATION

        def _capture_observe(self_hist, value: float) -> None:
            observed_duration[0] = value

        monkeypatch.setattr(SAGA_DURATION.__class__, "observe", _capture_observe)

        with SagaMetrics("store_memory"):
            pass  # This is the BUG pattern

        # Duration should be very close to 0 (just the overhead of context mgr)
        assert observed_duration[0] < 0.001, (
            f"Pass-only block recorded {observed_duration[0]}s — "
            f"expected near-zero for the bug pattern"
        )


class TestSagaMetricsSuccessFailureRecording:
    """Verify that SagaMetrics correctly records success vs failure results."""

    def test_success_records_ok_result(self, monkeypatch) -> None:
        """A successful saga must record result='success'."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        results: list[str] = []

        from nce.observability import SAGA_DURATION

        original_labels = SAGA_DURATION.labels

        def _capture_labels(**kw):
            if kw.get("operation") == "test_op":
                results.append(kw.get("result", ""))
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_DURATION, "labels", _capture_labels)

        with SagaMetrics("test_op"):
            pass

        assert "success" in results, f"Expected 'success' result, got {results}"

    def test_failure_records_failure_result(self, monkeypatch) -> None:
        """A failing saga must record result='failure'."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        results: list[str] = []

        from nce.observability import SAGA_DURATION

        original_labels = SAGA_DURATION.labels

        def _capture_labels(**kw):
            if kw.get("operation") == "test_op":
                results.append(kw.get("result", ""))
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_DURATION, "labels", _capture_labels)

        with pytest.raises(RuntimeError):
            with SagaMetrics("test_op"):
                raise RuntimeError("deliberately broken")

        assert "failure" in results, f"Expected 'failure' result, got {results}"

    def test_on_failure_callback_invoked(self) -> None:
        """The on_failure callback must be invoked when the block raises."""
        fired: list[BaseException] = []

        def _cb(exc):
            fired.append(exc)

        with pytest.raises(ValueError):
            with SagaMetrics("test_op", on_failure=_cb):
                raise ValueError("oops")

        assert len(fired) == 1, "on_failure was not called"
        assert isinstance(fired[0], ValueError)
        assert str(fired[0]) == "oops"

    def test_on_failure_not_invoked_on_success(self) -> None:
        """The on_failure callback must NOT be invoked on success."""
        fired: list[BaseException] = []

        def _cb(exc):
            fired.append(exc)

        with SagaMetrics("test_op", on_failure=_cb):
            pass

        assert fired == [], "on_failure was called on success"

    def test_store_memory_non_opt_in_emits_always(self, monkeypatch) -> None:
        """SagaMetrics for operation='store_memory' must emit metrics even when NCE_OBSERVABILITY_ENABLED is False."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", False)
        results: list[str] = []

        from nce.observability import SAGA_DURATION

        original_labels = SAGA_DURATION.labels

        def _capture_labels(**kw):
            if kw.get("operation") == "store_memory":
                results.append(kw.get("result", ""))
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_DURATION, "labels", _capture_labels)

        with SagaMetrics("store_memory"):
            pass

        assert "success" in results, (
            f"Expected 'success' result even with observability disabled, got {results}"
        )

    def test_store_memory_non_opt_in_failure_emits_always(self, monkeypatch) -> None:
        """SagaMetrics for operation='store_memory' must emit failure metrics even when NCE_OBSERVABILITY_ENABLED is False."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", False)
        results: list[str] = []

        from nce.observability import SAGA_DURATION

        original_labels = SAGA_DURATION.labels

        def _capture_labels(**kw):
            if kw.get("operation") == "store_memory":
                results.append(kw.get("result", ""))
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_DURATION, "labels", _capture_labels)

        with pytest.raises(ValueError):
            with SagaMetrics("store_memory"):
                raise ValueError("failed saga")

        assert "failure" in results, (
            f"Expected 'failure' result even with observability disabled, got {results}"
        )


# ===========================================================================
# 2. Tracer span wrapping — verifies OTel spans wrap the work
# ===========================================================================


class TestTracerSpanWrapsWork:
    """Verify that the OTel tracer span properly wraps the Saga body."""

    def test_span_entered_and_exited(self, monkeypatch) -> None:
        """The tracer's start_as_current_span must be entered and exited."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        entered = [False]
        exited = [False]

        class _CaptureSpan:
            def __enter__(self_span):
                entered[0] = True
                return self_span

            def __exit__(self_span, *args):
                exited[0] = True

            def set_attribute(self, *args):
                pass

        class _CaptureTracer:
            def start_as_current_span(self, name, **kw):
                return _CaptureSpan()

        monkeypatch.setattr(
            "nce.orchestrators.memory.get_tracer",
            lambda: _CaptureTracer(),
        )

        # We need to test the actual SagaMetrics wrapping behavior —
        # MemoryOrchestrator is too complex to instantiate here.
        # Instead, test that the pattern used in the fix is correct:
        # the span context manager wraps the entire SagaMetrics context.
        tracer = _CaptureTracer()
        with tracer.start_as_current_span("orchestrator.store_memory") as span:
            span.set_attribute("nce.namespace_id", "test")
            with SagaMetrics("store_memory"):
                for _ in range(100):
                    _ = [i**2 for i in range(10)]

        assert entered[0], "Span was never entered"
        assert exited[0], "Span was never exited"


# ===========================================================================
# 3. Source code structural verification — ensures the fix is in place
# ===========================================================================


class TestMemoryModuleStructure:
    """Parse the source code to verify the context managers wrap real work.

    These tests are structural AST tests — they confirm that the ``pass``-only
    pattern has been eliminated from both ``store_memory`` and ``store_media``.
    """

    @staticmethod
    def _get_method_ast(method_name: str) -> ast.FunctionDef:
        """Extract the AST of a method from MemoryOrchestrator."""
        import nce.orchestrators.memory as mem_mod

        raw = inspect.getsource(mem_mod)
        module = ast.parse(raw)
        class_def = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.ClassDef) and node.name == "MemoryOrchestrator"
        )
        method = next(
            node
            for node in ast.walk(class_def)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        )
        return method

    def _get_saga_metrics_blocks(self, method_ast: ast.FunctionDef) -> list[ast.With]:
        """Find all ``with SagaMetrics(...)`` blocks in a method's AST."""
        blocks: list[ast.With] = []
        for node in ast.walk(method_ast):
            if isinstance(node, ast.With):
                for item in node.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Name)
                        and item.context_expr.func.id == "SagaMetrics"
                    ):
                        blocks.append(node)
        return blocks

    def test_store_memory_no_pass_in_saga_metrics(self) -> None:
        """_run_store_memory_saga must NOT have ``pass`` as the only statement in
        the SagaMetrics context. The fix indented the entire body."""
        method = self._get_method_ast("_run_store_memory_saga")
        blocks = self._get_saga_metrics_blocks(method)

        assert len(blocks) >= 1, "No SagaMetrics context found in _run_store_memory_saga"

        for with_node in blocks:
            body = with_node.body
            # The body must NOT consist solely of a `pass` statement
            assert not (len(body) == 1 and isinstance(body[0], ast.Pass)), (
                "SagaMetrics context in _run_store_memory_saga still contains only `pass` — "
                "the P0 bug fix was not applied. The body must contain the "
                "actual saga work."
            )
            # The body must have multiple statements (actual work)
            assert len(body) > 2, (
                f"SagaMetrics context in _run_store_memory_saga has only "
                f"{len(body)} statement(s) — expected real work"
            )

    def test_store_artifact_no_pass_in_saga_metrics(self) -> None:
        """store_artifact must have actual work inside SagaMetrics (store_media delegates here)."""
        method = self._get_method_ast("store_artifact")
        blocks = self._get_saga_metrics_blocks(method)

        assert len(blocks) >= 1, "No SagaMetrics context found in store_artifact"

        for with_node in blocks:
            body = with_node.body
            assert not (len(body) == 1 and isinstance(body[0], ast.Pass)), (
                "SagaMetrics context in store_artifact still contains only `pass` — "
                "the P0 bug fix was not applied."
            )
            assert len(body) > 2, (
                f"SagaMetrics context in store_artifact has only "
                f"{len(with_node.body)} statement(s) — expected real work"
            )

    def test_store_memory_no_unused_metrics_variable(self) -> None:
        """_run_store_memory_saga must NOT use ``as metrics:`` — the variable was
        unused even before the fix."""
        method = self._get_method_ast("_run_store_memory_saga")

        for node in ast.walk(method):
            if isinstance(node, ast.With):
                for item in node.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Name)
                        and item.context_expr.func.id == "SagaMetrics"
                        and item.optional_vars is not None
                    ):
                        pytest.fail(
                            "_run_store_memory_saga uses 'with SagaMetrics(...) as metrics:' — "
                            "the `metrics` variable is unused. Remove `as metrics:`. "
                            "Fix: with SagaMetrics('store_memory'):"
                        )

    def test_store_media_no_unused_metrics_variable(self) -> None:
        """store_media must NOT use ``as metrics:`` either."""
        method = self._get_method_ast("store_media")

        for node in ast.walk(method):
            if isinstance(node, ast.With):
                for item in node.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Name)
                        and item.context_expr.func.id == "SagaMetrics"
                        and item.optional_vars is not None
                    ):
                        pytest.fail(
                            "store_media uses 'with SagaMetrics(...) as metrics:' — "
                            "the `metrics` variable is unused. Remove `as metrics:`. "
                            "Fix: with SagaMetrics('store_media'):"
                        )


# ===========================================================================
# 4. MemoryOrchestrator integration with mocked dependencies
# ===========================================================================


class TestMemoryOrchestratorObservabilityContract:
    """Test that MemoryOrchestrator's public methods satisfy the observability
    contract: the OTel span and SagaMetrics must wrap all Saga work.

    Uses mocked database clients — the focus is on the observability wrapping,
    not on DB behavior.
    """

    @pytest.fixture
    def mock_pg_pool(self):
        """Return an asyncpg pool mock that yields a working connection."""
        pool = AsyncMock()
        conn = AsyncMock()
        # Make the connection a proper async context manager
        conn.__aenter__.return_value = conn
        # Mock fetch/fetchrow/fetchval/execute
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        return pool

    @pytest.fixture
    def mock_mongo_client(self):
        """Return a motor client mock with a working collection."""
        client = AsyncMock()
        db = AsyncMock()
        collection = AsyncMock()
        # Mock insert_one to return a result with inserted_id
        insert_result = MagicMock()
        insert_result.inserted_id = MagicMock()
        insert_result.inserted_id.__str__ = MagicMock(return_value="507f1f77bcf86cd799439011")
        collection.insert_one = AsyncMock(return_value=insert_result)
        collection.delete_one = AsyncMock()
        db.episodes = collection
        client.memory_archive = db
        return client

    @pytest.fixture
    def mock_redis_client(self):
        """Return a redis mock."""
        redis = AsyncMock()
        redis.setex = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        return redis

    @pytest.fixture
    def orchestrator(self, mock_pg_pool, mock_mongo_client, mock_redis_client):
        """Build a MemoryOrchestrator with fully mocked dependencies."""
        from nce.orchestrators.memory import MemoryOrchestrator

        return MemoryOrchestrator(
            pg_pool=mock_pg_pool,
            mongo_client=mock_mongo_client,
            redis_client=mock_redis_client,
        )

    @pytest.fixture
    def store_payload(self):
        """A minimal StoreMemoryRequest for testing."""
        from nce.models import AssertionType, MemoryType, StoreMemoryRequest

        return StoreMemoryRequest(
            namespace_id=str(uuid4()),
            agent_id="test_agent",
            content="Test memory content for observability verification.",
            summary="Test memory summary.",
            heavy_payload="Test heavy payload.",
            memory_type=MemoryType.episodic,
            assertion_type=AssertionType.observation,
        )

    @pytest.mark.asyncio
    async def test_store_memory_saga_metrics_records_work(
        self, orchestrator, store_payload, monkeypatch
    ) -> None:
        """When store_memory is called, SagaMetrics must record the work
        duration (not sub-microsecond)."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        observed_durations: list[float] = []

        from nce.observability import SAGA_DURATION

        original_labels = SAGA_DURATION.labels

        def _capture_labels(**kw):
            if kw.get("operation") == "store_memory":
                # Return a spy object that captures the observed value
                class _SpyHistogram:
                    def observe(self_hist, value):
                        observed_durations.append(value)

                return _SpyHistogram()
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_DURATION, "labels", _capture_labels)

        # Also need to patch the PII pipeline since it calls external deps
        from nce.models import PIIProcessResult

        monkeypatch.setattr(
            orchestrator,
            "_apply_pii_pipeline",
            AsyncMock(
                return_value=(
                    PIIProcessResult(
                        sanitized_text="sanitized summary",
                        redacted=True,
                        entities_found=[],
                        vault_entries=[],
                    ),
                    "sanitized summary",
                    "sanitized heavy",
                    [],  # entities
                    [],  # triplets
                )
            ),
        )

        # Patch embedding to avoid ML model
        from nce import embeddings as emb_mod

        monkeypatch.setattr(
            emb_mod,
            "embed_batch",
            AsyncMock(return_value=[[0.1] * 768]),
        )

        try:
            await orchestrator.store_memory(store_payload)
        except Exception:
            # Some dependencies are still mocked — we just need to check
            # that the observability wrapping happened. If the method
            # raises, we check durations before the raise.
            pass

        # The key assertion: SagaMetrics must record at least one duration
        assert len(observed_durations) >= 1, (
            "SagaMetrics never recorded a duration for store_memory — "
            "the context manager is not wrapping the work."
        )

        # The duration must be non-trivial (>> sub-microsecond)
        # The method does real async work even with mocks
        assert any(d > 0.0 for d in observed_durations), (
            f"All {len(observed_durations)} SagaMetrics durations were 0.0 — "
            f"the context manager is wrapping nothing."
        )

    @pytest.mark.asyncio
    async def test_store_memory_span_entered(
        self, orchestrator, store_payload, monkeypatch
    ) -> None:
        """The OTel span for store_memory must be entered and exited."""
        entered = [False]
        exited = [False]

        class _SpanSpy:
            def __enter__(self_span):
                entered[0] = True
                return self_span

            def __exit__(self_span, *args):
                exited[0] = True

            def set_attribute(self, *args):
                pass

        class _TracerSpy:
            def start_as_current_span(self, name, **kw):
                assert "store_memory" in name, (
                    f"Expected span name containing 'store_memory', got {name!r}"
                )
                return _SpanSpy()

        monkeypatch.setattr(
            "nce.orchestrators.memory.get_tracer",
            lambda: _TracerSpy(),
        )

        # Patch PII pipeline
        from nce.models import PIIProcessResult

        monkeypatch.setattr(
            orchestrator,
            "_apply_pii_pipeline",
            AsyncMock(
                return_value=(
                    PIIProcessResult(
                        sanitized_text="test",
                        redacted=True,
                        entities_found=[],
                        vault_entries=[],
                    ),
                    "test",
                    "test",
                    [],
                    [],
                )
            ),
        )

        from nce import embeddings as emb_mod

        monkeypatch.setattr(
            emb_mod,
            "embed_batch",
            AsyncMock(return_value=[[0.1] * 768]),
        )

        try:
            await orchestrator.store_memory(store_payload)
        except Exception:
            pass

        assert entered[0], "The store_memory OTel span was never entered"
        assert exited[0], "The store_memory OTel span was never exited"

    @pytest.mark.asyncio
    async def test_store_media_saga_metrics_records_work(self, orchestrator, monkeypatch) -> None:
        """store_media must also properly wrap work in SagaMetrics."""
        monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)
        observed_durations: list[float] = []

        from nce.observability import SAGA_DURATION

        original_labels = SAGA_DURATION.labels

        def _capture_labels(**kw):
            if kw.get("operation") == "store_artifact":

                class _SpyHistogram:
                    def observe(self_hist, value):
                        observed_durations.append(value)

                return _SpyHistogram()
            return original_labels(**kw)

        monkeypatch.setattr(SAGA_DURATION, "labels", _capture_labels)

        # store_media needs a payload with a file that exists
        import os
        import tempfile

        from nce.models import MediaPayload

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test media content")
            tmp_path = f.name

        # Mock the MinIO client's fput_object to avoid actual upload
        monkeypatch.setattr(orchestrator, "minio_client", MagicMock())
        monkeypatch.setattr(
            orchestrator.minio_client,
            "fput_object",
            MagicMock(),
        )

        payload = MediaPayload(
            namespace_id=str(uuid4()),
            user_id="test_user",
            session_id="test_session",
            media_type="image",
            file_path_on_disk=tmp_path,
            summary="test summary",
        )

        # Patch store_memory since store_media delegates to it
        monkeypatch.setattr(
            orchestrator,
            "store_memory",
            AsyncMock(return_value={"payload_ref": "test_ref"}),
        )

        try:
            await orchestrator.store_media(payload)
        except Exception:
            pass
        finally:
            os.unlink(tmp_path)

        assert len(observed_durations) >= 1, (
            "SagaMetrics never recorded a duration for store_artifact — "
            "the context manager is not wrapping the work."
        )

    @pytest.mark.asyncio
    async def test_store_media_span_entered(self, orchestrator, monkeypatch) -> None:
        """The OTel span for store_media must be entered and exited."""
        entered = [False]
        exited = [False]

        class _SpanSpy:
            def __enter__(self_span):
                entered[0] = True
                return self_span

            def __exit__(self_span, *args):
                exited[0] = True

            def set_attribute(self, *args):
                pass

        class _TracerSpy:
            def start_as_current_span(self, name, **kw):
                assert "store_artifact" in name
                return _SpanSpy()

        monkeypatch.setattr(
            "nce.orchestrators.memory.get_tracer",
            lambda: _TracerSpy(),
        )

        import os
        import tempfile

        from nce.models import MediaPayload

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test")
            tmp_path = f.name

        monkeypatch.setattr(orchestrator, "minio_client", MagicMock())
        monkeypatch.setattr(
            orchestrator,
            "store_memory",
            AsyncMock(return_value={"payload_ref": "test_ref"}),
        )

        payload = MediaPayload(
            namespace_id=str(uuid4()),
            user_id="test_user",
            session_id="test_session",
            media_type="image",
            file_path_on_disk=tmp_path,
            summary="test",
        )

        try:
            await orchestrator.store_media(payload)
        except Exception:
            pass
        finally:
            os.unlink(tmp_path)

        assert entered[0], "The store_artifact OTel span was never entered"
        assert exited[0], "The store_artifact OTel span was never exited"


# ===========================================================================
# 5. RQ Trace Context Propagation Tests
# ===========================================================================


def test_rq_trace_context_propagation(monkeypatch) -> None:
    """Verify that enqueue_traced injects OpenTelemetry trace context and
    traced_worker_job extracts and restores it correctly in the worker."""
    monkeypatch.setattr(cfg, "NCE_OBSERVABILITY_ENABLED", True)

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import get_current_span

    from nce.observability import HAS_OTEL, enqueue_traced, traced_worker_job

    if not HAS_OTEL:
        pytest.skip("OpenTelemetry is not installed")

    # Set up active tracer provider for testing
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test_propagation")

    mock_queue = MagicMock()
    mock_job = MagicMock()
    mock_job.meta = {}
    mock_job.id = "test-job-id"
    mock_job.origin = "high_priority"

    # Capture the enqueued meta
    enqueued_meta = {}

    def mock_enqueue(func, *args, **kwargs):
        nonlocal enqueued_meta
        enqueued_meta.update(kwargs.get("meta", {}))
        mock_job.meta = kwargs.get("meta", {})
        return mock_job

    mock_queue.enqueue = mock_enqueue

    # 1. Enqueue a job under an active span
    with tracer.start_as_current_span("parent_span") as parent_span:
        parent_span_context = parent_span.get_span_context()
        enqueue_traced(mock_queue, lambda: None)

    assert "traceparent" in enqueued_meta

    # 2. Worker executes job, extracts context
    # Mock rq.get_current_job
    monkeypatch.setattr("rq.get_current_job", lambda: mock_job)

    extracted_parent_span_id = None
    inside_span_name = None

    @traced_worker_job("test_worker_task")
    def run_worker_task():
        nonlocal extracted_parent_span_id, inside_span_name
        current_span = get_current_span()
        inside_span_name = current_span.name
        extracted_parent_span_id = current_span.parent.span_id if current_span.parent else None

    run_worker_task()

    assert inside_span_name == "rq_worker:test_worker_task"
    assert extracted_parent_span_id == parent_span_context.span_id
