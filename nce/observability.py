"""
NCE Observability Layer
==========================
Centralised Prometheus metrics and OpenTelemetry tracing for the NCE stack.
Handles initialization and provides decorators/context managers for instrumentation.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from contextlib import ContextDecorator, asynccontextmanager
from typing import Any, Literal, TypeVar

log = logging.getLogger("nce.observability")


# ---------------------------------------------------------------------------
# Stub metric — always defined so it can be imported by other modules
# (e.g. background_task_manager) regardless of whether prometheus_client is
# installed.  The real Prometheus classes are assigned conditionally below.
# ---------------------------------------------------------------------------


class _StubMetric:
    """No-op drop-in for Prometheus Counter/Gauge/Histogram when the library is absent."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def labels(self, *args: object, **kwargs: object) -> _StubMetric:
        return self

    def inc(self, amount: float = 1, **kwargs: object) -> None:
        pass

    def dec(self, amount: float = 1, **kwargs: object) -> None:
        pass

    def observe(self, *args: object, **kwargs: object) -> None:
        pass

    def set(self, *args: object, **kwargs: object) -> None:
        pass


try:
    from opentelemetry import context as otel_context
    from opentelemetry import propagate, trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

try:
    from prometheus_client import (
        REGISTRY as _PROM_REGISTRY,
    )
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    HAS_PROMETHEUS = True

    def _safe_metric(metric_cls, name: str, *args, **kwargs):
        """Create or retrieve an existing Prometheus metric by name.

        When the same metric name is registered twice in the same process
        (e.g. during test-suite module reloads), prometheus_client raises
        ``ValueError: Duplicated timeseries``.  This helper catches that
        error and returns the already-registered collector instead so the
        module stays usable.
        """
        try:
            return metric_cls(name, *args, **kwargs)
        except ValueError:
            # Already registered — retrieve the existing collector.
            # _names_to_collectors is a private prometheus_client attribute; guard
            # with getattr so a future library rename doesn't cause AttributeError.
            collectors = getattr(_PROM_REGISTRY, "_names_to_collectors", {})
            return collectors.get(name) or metric_cls(name, *args, registry=None, **kwargs)

    def _safe_counter(name: str, *args, **kwargs) -> Counter:
        return _safe_metric(Counter, name, *args, **kwargs)

    def _safe_histogram(name: str, *args, **kwargs) -> Histogram:
        return _safe_metric(Histogram, name, *args, **kwargs)

    def _safe_gauge(name: str, *args, **kwargs) -> Gauge:
        return _safe_metric(Gauge, name, *args, **kwargs)

except ImportError:
    HAS_PROMETHEUS = False

    Counter = Histogram = Gauge = _StubMetric  # type: ignore[misc, assignment]

    def start_http_server(*args, **kwargs):
        pass

    def _safe_counter(name, *args, **kwargs):  # type: ignore[misc]
        return _StubMetric()

    def _safe_histogram(name, *args, **kwargs):  # type: ignore[misc]
        return _StubMetric()

    def _safe_gauge(name, *args, **kwargs):  # type: ignore[misc]
        return _StubMetric()


from nce.config import cfg  # noqa: E402 — after optional-dep stubs

# --- Types ---
F = TypeVar("F", bound=Callable[..., Any])

# --- Prometheus Metrics ---

# Tool level metrics (server.py)
TOOL_CALLS = _safe_counter(
    "nce_tool_calls_total",
    "Total count of MCP tool calls",
    ["tool_name", "status"],
)
TOOL_LATENCY = _safe_histogram(
    "nce_tool_latency_seconds",
    "Latency of MCP tool calls in seconds",
    ["tool_name"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, float("inf")),
)

# Saga level metrics (orchestrator.py)
SAGA_DURATION = _safe_histogram(
    "nce_saga_duration_seconds",
    "Duration of distributed saga transactions",
    ["operation", "result"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, float("inf")),
)
SAGA_FAILURES = _safe_counter(
    "nce_saga_failures_total",
    "Total count of saga step failures",
    ["stage"],  # stage = pg, mongo, redis, etc.
)

# Component specific
EMBEDDING_COUNT = _safe_counter(
    "nce_embedding_count",
    "Total count of individual chunks embedded",
    ["model_id"],
)
REEMBEDDING_PROGRESS = _safe_gauge(
    "nce_reembedding_progress",
    "Progress of the background re-embedding worker",
    ["worker_id"],
)

# VRAM metrics for re-embedder CUDA memory pressure monitoring (Item 49)
REEMBEDDER_VRAM_ALLOCATED = _safe_gauge(
    "nce_reembedder_vram_allocated_bytes",
    "Current VRAM allocated to PyTorch tensors by the re-embedder (torch.cuda.memory_allocated)",
    ["worker_id"],
)
REEMBEDDER_VRAM_RESERVED = _safe_gauge(
    "nce_reembedder_vram_reserved_bytes",
    "Current VRAM reserved by the CUDA caching allocator for the re-embedder (torch.cuda.memory_reserved)",
    ["worker_id"],
)
REEMBEDDER_VRAM_PEAK = _safe_gauge(
    "nce_reembedder_vram_peak_bytes",
    "Peak VRAM allocated since last measurement reset (torch.cuda.max_memory_allocated)",
    ["worker_id"],
)

# VRAM metrics for NLI cross-encoder CUDA memory pressure monitoring (Batch 127)
NLI_VRAM_ALLOCATED = _safe_gauge(
    "nce_nli_vram_allocated_bytes",
    "Current VRAM allocated to PyTorch tensors by the NLI cross-encoder (torch.cuda.memory_allocated)",
)
NLI_VRAM_RESERVED = _safe_gauge(
    "nce_nli_vram_reserved_bytes",
    "Current VRAM reserved by the CUDA caching allocator for the NLI cross-encoder (torch.cuda.memory_reserved)",
)

# Connection pool / RLS overhead
SCOPED_SESSION_LATENCY = _safe_histogram(
    "nce_scoped_session_latency_seconds",
    "Latency of scoped_session acquisition + SET LOCAL RLS",
    buckets=(0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, float("inf")),
)

# Dead Letter Queue / Poison Pill metrics (Phase 3 tasks.py)
TASK_DLQ_TOTAL = _safe_counter(
    "nce_task_dlq_total",
    "Total count of background tasks routed to the Dead Letter Queue after exhausting retries",
    ["task_name"],
)
TASK_DLQ_BACKLOG = _safe_gauge(
    "nce_task_dlq_backlog",
    "Current number of pending (un-replayed, un-purged) entries in the Dead Letter Queue",
    ["task_name"],
)

# Partition maintenance runway (Item C)
EVENT_LOG_PARTITION_MONTHS_AHEAD = _safe_gauge(
    "nce_event_log_partition_months_ahead",
    "Number of future monthly partitions ahead of current month for event_log",
)

# Merkle chain verification gauge (B2) — 1=valid, 0=corrupted
MERKLE_CHAIN_VALID = _safe_gauge(
    "nce_merkle_chain_valid",
    "Merkle chain validity: 1=valid, 0=corrupted",
)

# Event signature sampling gauge (Batch 102) — 1=all sampled valid, 0=any tampered
EVENT_SIGNATURE_VALID = _safe_gauge(
    "nce_event_signature_valid",
    "Event signature sampling validity: 1=all sampled valid, 0=any tampered or unverifiable",
)

# Transactional outbox relay (Phase 1.1)
OUTBOX_DELIVERED_TOTAL = _safe_counter(
    "nce_outbox_delivered_total",
    "Outbox events successfully published by the relay",
    ["event_type"],
)
OUTBOX_DELIVERY_FAILURES_TOTAL = _safe_counter(
    "nce_outbox_delivery_failures_total",
    "Outbox delivery attempts that failed (may retry until DLQ)",
    ["event_type"],
)
OUTBOX_DLQ_TOTAL = _safe_counter(
    "nce_outbox_dlq_total",
    "Outbox events routed to DLQ after exhausting relay attempts",
    ["event_type"],
)

# Signing key cache (Item 31)
SIGNING_KEY_CACHE_HIT_TOTAL = _safe_counter(
    "nce_signing_key_cache_hit_total",
    "Total count of signing key cache hits",
)
SIGNING_KEY_CACHE_MISS_TOTAL = _safe_counter(
    "nce_signing_key_cache_miss_total",
    "Total count of signing key cache misses",
)

# Extraction security (Items E, K)
EXTRACTION_MIME_MISMATCH_TOTAL = _safe_counter(
    "nce_extraction_mime_mismatch_total",
    "Total count of attachments rejected due to extension/magic-byte MIME mismatch",
)
EXTRACTION_REJECTED_TOO_LARGE_TOTAL = _safe_counter(
    "nce_extraction_rejected_too_large_total",
    "Total count of attachments rejected due to size limit",
)

# Circuit breaker state (Item 44)
CIRCUIT_BREAKER_STATE = _safe_gauge(
    "nce_circuit_breaker_state",
    "Current circuit breaker state: 0=closed, 1=half_open, 2=open",
    ["provider"],
)
CIRCUIT_BREAKER_FAILURES = _safe_gauge(
    "nce_circuit_breaker_failures",
    "Current consecutive failure count inside the circuit breaker",
    ["provider"],
)
MINIO_ORPHAN_CLEANUP_FAILURES_TOTAL = _safe_counter(
    "nce_minio_orphan_cleanup_failures_total",
    "Number of MinIO object deletions that failed during orphan cleanup",
)
EXTERNAL_HTTP_ATTEMPTS_TOTAL = _safe_counter(
    "nce_external_http_attempts_total",
    "Total individual HTTP attempts including all retries",
    ["operation"],
)
EXTERNAL_HTTP_RETRIES_TOTAL = _safe_counter(
    "nce_external_http_retries_total",
    "Total retry attempts (excludes the initial attempt)",
    ["operation"],
)
EXTERNAL_HTTP_FAILURES_TOTAL = _safe_counter(
    "nce_external_http_failures_total",
    "Total HTTP calls that raised a client error or exhausted retries",
    ["operation", "error_type"],
)
EXTERNAL_HTTP_LATENCY_SECONDS = _safe_histogram(
    "nce_external_http_latency_seconds",
    "End-to-end wall-clock latency of HTTP calls including all retry attempts",
    ["operation"],
)

# Quota and embedding-fallback metrics (Batch 19)
QUOTA_CONSUMED = _safe_gauge(
    "nce_quota_consumed_total",
    "Current consumed resource amount for a namespace/agent quota",
    ["namespace_id", "resource_type", "agent_id"],
)
QUOTA_REMAINING = _safe_gauge(
    "nce_quota_remaining",
    "Current remaining resource limit for a namespace/agent quota",
    ["namespace_id", "resource_type", "agent_id"],
)
EMBEDDING_FALLBACKS = _safe_counter(
    "nce_embedding_fallbacks_total",
    "Total count of embedding fallback/hash-stub triggerings",
)

# Envelope-encryption decrypt failures in re-embed consumers (Batch 109)
# Incremented whenever maybe_decrypt_raw_data raises for a memory in the
# re-embedding pipeline.  A zeroed wrapped_dek (provable forgetting) is the
# primary driver; the memory is flagged dek_unreadable and skipped, not raised.
ECHO_SUPPRESSED_TOTAL = _safe_counter(
    "nce_echo_suppressed_total",
    "Total count of webhook events suppressed because they were caused by NCE itself",
    ["system"],
)

ENVELOPE_DECRYPT_FAILURES = _safe_counter(
    "nce_envelope_decrypt_failures_total",
    "Total count of envelope-decryption failures in the re-embedding pipeline "
    "(memory skipped and flagged dek_unreadable=true)",
    ["consumer"],  # consumer = "re_embedder" | "reembedding_worker"
)

# --- Initialization ---

_tracer_initialized = False


def init_observability() -> None:
    """Initializes OTel tracer and starts Prometheus exporter."""
    global _tracer_initialized
    if not cfg.NCE_OBSERVABILITY_ENABLED:
        return
    if not HAS_OTEL:
        return

    if not _tracer_initialized:
        # 1. Prometheus
        try:
            start_http_server(cfg.NCE_PROMETHEUS_PORT)
        except Exception:
            # Port might already be in use if running in a multi-process environment
            pass

        # 2. OpenTelemetry
        resource = Resource(attributes={"service.name": cfg.NCE_OTEL_SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        processor = BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{cfg.NCE_OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces")
        )
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        _tracer_initialized = True


def get_tracer():
    if not HAS_OTEL:
        # Return a mock tracer that does nothing
        class _MockSpan:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def set_attribute(self, *args):
                pass

            def record_exception(self, *args):
                pass

            def set_status(self, *args):
                pass

        class _MockTracer:
            def start_as_current_span(self, *args, **kwargs):
                return _MockSpan()

        return _MockTracer()
    return trace.get_tracer("nce")


# --- Instrumentation Helpers ---


def instrument_tool(tool_name: str):
    """Decorator to instrument an MCP tool with metrics and a span."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not cfg.NCE_OBSERVABILITY_ENABLED:
                return await func(*args, **kwargs)

            tracer = get_tracer()
            start_time = time.perf_counter()
            status = "success"

            with tracer.start_as_current_span(f"mcp_tool:{tool_name}") as span:
                span.set_attribute("nce.tool", tool_name)
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception as e:
                    status = "error"
                    span.record_exception(e)
                    if HAS_OTEL:
                        span.set_status(trace.Status(trace.StatusCode.ERROR))
                    raise
                finally:
                    duration = time.perf_counter() - start_time
                    TOOL_CALLS.labels(tool_name=tool_name, status=status).inc()
                    TOOL_LATENCY.labels(tool_name=tool_name).observe(duration)

        return wrapper  # type: ignore

    return decorator


@asynccontextmanager
async def instrument_tool_call(tool_name: str):
    """Context manager to instrument an MCP tool call."""
    if not cfg.NCE_OBSERVABILITY_ENABLED:
        yield None
        return

    tracer = get_tracer()
    start_time = time.perf_counter()
    status = "success"

    with tracer.start_as_current_span(f"mcp_tool:{tool_name}") as span:
        span.set_attribute("nce.tool", tool_name)
        try:
            yield span
        except Exception as e:
            status = "error"
            span.record_exception(e)
            if HAS_OTEL:
                span.set_status(trace.Status(trace.StatusCode.ERROR))
            raise
        finally:
            duration = time.perf_counter() - start_time
            TOOL_CALLS.labels(tool_name=tool_name, status=status).inc()
            TOOL_LATENCY.labels(tool_name=tool_name).observe(duration)


class SagaMetrics:
    """Context manager for saga transactions.

    Optional *on_failure* callback
    --------------------------------
    Pass a callable ``on_failure(exc: BaseException, **kwargs)`` to receive
    structured information when the saga fails.  The callback MUST use
    ``.get()`` (or keyword defaults) for every key it reads from ``kwargs``
    because callers are not required to supply any particular key.

    Example::

        def _on_fail(exc, **kw):
            step = kw.get("step_name", "unknown")   # safe — never KeyError
            SagaMetrics.record_failure(stage=step)

        with SagaMetrics("store_memory", on_failure=_on_fail):
            ...
    """

    def __init__(
        self,
        operation: str,
        on_failure: Callable[..., None] | None = None,
    ) -> None:
        self.operation = operation
        self.start_time = 0.0
        self._on_failure = on_failure

    def __enter__(self) -> SagaMetrics:
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if cfg.NCE_OBSERVABILITY_ENABLED or self.operation == "store_memory":
            result = "success" if exc_type is None else "failure"
            duration = time.perf_counter() - self.start_time
            SAGA_DURATION.labels(operation=self.operation, result=result).observe(duration)

        if exc_type is not None and self._on_failure is not None:
            try:
                # Fire the callback — it receives the live exception.
                # kwargs are intentionally empty here; callers that need
                # richer context should bind them via functools.partial or
                # a closure so the callback signature stays (exc, **kw).
                self._on_failure(exc_val)
            except Exception as cb_exc:  # pragma: no cover
                log.warning("[SagaMetrics] on_failure callback raised: %s", cb_exc)

    @staticmethod
    def record_failure(stage: str) -> None:
        """Increment SAGA_FAILURES counter for the given pipeline stage."""
        if cfg.NCE_OBSERVABILITY_ENABLED:
            SAGA_FAILURES.labels(stage=stage).inc()

    @staticmethod
    def on_saga_failure(exc: BaseException, **kwargs: Any) -> None:
        """Safe Saga failure handler for use as an *on_failure* callback.

        Reads ``kwargs`` with ``.get()`` throughout so omitting any key
        (including ``step_name``) never raises ``KeyError``.  The metric
        is always emitted — it defaults to ``"unknown"`` when ``step_name``
        is absent.

        Usage::

            import functools
            cb = functools.partial(
                SagaMetrics.on_saga_failure, step_name="pg_insert"
            )
            with SagaMetrics("store_memory", on_failure=cb):
                ...
        """
        step_name: str = kwargs.get("step_name", "unknown")  # never raises
        stage: str = kwargs.get("stage", step_name)
        if cfg.NCE_OBSERVABILITY_ENABLED:
            SAGA_FAILURES.labels(stage=stage).inc()


# ---------------------------------------------------------------------------
# Distributed tracing — W3C Trace Context propagation
# ---------------------------------------------------------------------------

# Re-export for convenience so callers don't need to import opentelemetry directly
HasOTel = HAS_OTEL


def inject_trace_headers(
    headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Inject the current OpenTelemetry trace context into *headers*.

    Adds the W3C ``traceparent`` (and optionally ``tracestate``) header to an
    outbound HTTP request so the downstream service can continue the same trace.

    Usage::

        headers = inject_trace_headers({"content-type": "application/json"})
        async with httpx.AsyncClient() as client:
            await client.post(url, headers=headers, json=body)

    Returns a new dict if *headers* is ``None``, or mutates and returns the
    same dict.  Safe to call when OTel is disabled (returns headers unchanged).
    """
    headers = {} if headers is None else headers
    if HAS_OTEL and cfg.NCE_OBSERVABILITY_ENABLED:
        propagate.inject(headers)
    return headers


# ---------------------------------------------------------------------------
# Starlette / ASGI middleware — extract trace context from incoming requests
# ---------------------------------------------------------------------------

try:
    from starlette.types import ASGIApp, Receive, Scope, Send

    HAS_STARLETTE = True
except ImportError:
    HAS_STARLETTE = False


class OpenTelemetryTraceMiddleware:
    """Starlette ASGI middleware that extracts W3C ``traceparent`` from incoming
    HTTP requests and activates the remote trace context.

    Place this **before** auth middleware so the trace is established before any
    handler runs::

        app = Starlette(
            middleware=[
                Middleware(OpenTelemetryTraceMiddleware),
                Middleware(BasicAuthMiddleware, ...),
                Middleware(HMACAuthMiddleware, ...),
            ],
            ...
        )

    When no ``traceparent`` header is present on the request, this middleware is
    a no-op — the current trace remains whatever OTel auto-instrumentation or
    the SDK has already established.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and HAS_OTEL and cfg.NCE_OBSERVABILITY_ENABLED:
            # Build a plain dict from the ASGI scope headers (list of (bytes, bytes))
            headers = {
                k.decode("ascii", errors="replace").lower(): v.decode("ascii", errors="replace")
                for k, v in scope.get("headers", [])
            }
            if "traceparent" in headers:
                ctx = propagate.extract(headers)
                # Set the extracted context as the active context for this request
                token = otel_context.attach(ctx)
                try:
                    await self.app(scope, receive, send)
                finally:
                    otel_context.detach(token)
                return

        await self.app(scope, receive, send)


def enqueue_traced(queue, func, *args, **kwargs):
    """Enqueue a job onto *queue* while propagating OpenTelemetry trace context.

    Extracts the active tracing context and injects it into the job's `meta` dictionary
    so the worker can associate the async execution with the enqueuer's span.
    """
    meta = kwargs.setdefault("meta", {})
    if HAS_OTEL and cfg.NCE_OBSERVABILITY_ENABLED:
        try:
            propagate.inject(meta)
        except Exception as e:
            log.warning("Failed to inject trace context into job meta: %s", e)
    return queue.enqueue(func, *args, **kwargs)


class traced_worker_job(ContextDecorator):
    """Context manager and decorator for RQ worker tasks to extract OTel trace context from job.meta.

    Restores the remote trace context and starts a new nested span for the job execution.
    """

    def __init__(self, operation_name: str) -> None:
        self.operation_name = operation_name
        self.token = None
        self.span_ctx = None
        self.span = None

    def __enter__(self):
        if not (HAS_OTEL and cfg.NCE_OBSERVABILITY_ENABLED):
            return self

        from rq import get_current_job

        job = get_current_job()
        if job and job.meta:
            try:
                ctx = propagate.extract(job.meta)
                self.token = otel_context.attach(ctx)
            except Exception as e:
                log.warning("Failed to extract trace context from job meta: %s", e)

        tracer = get_tracer()
        self.span_ctx = tracer.start_as_current_span(f"rq_worker:{self.operation_name}")
        self.span = self.span_ctx.__enter__()
        if job and self.span:
            self.span.set_attribute("rq.job_id", job.id)
            self.span.set_attribute("rq.queue", job.origin)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        if not (HAS_OTEL and cfg.NCE_OBSERVABILITY_ENABLED):
            return False

        try:
            if self.span_ctx:
                if exc_type is not None and self.span:
                    self.span.record_exception(exc_val)
                    self.span.set_status(trace.Status(trace.StatusCode.ERROR))
                self.span_ctx.__exit__(exc_type, exc_val, exc_tb)
        finally:
            if self.token:
                otel_context.detach(self.token)
        return False
