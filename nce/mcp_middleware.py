"""MCP middleware layers and helpers for the Neuro Cognitive Engine.

Designed to address Claude Desktop quirks:
1. StderrSanitizer: Safely redirects stray stdout writes to stderr, preventing protocol corruption.
2. GracefulTimeoutHandler: Runs long-running async tool operations without blocking or timing out.
3. SchemaEnforcer: Validates JSON tool outputs before sending them back to the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import sys
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import jsonschema

from nce.background_task_manager import create_tracked_task

log = logging.getLogger("nce.mcp_middleware")

# Type variables for decorators
F = TypeVar("F", bound=Callable[..., Any])


# ==============================================================================
# 1. StderrSanitizer
# ==============================================================================


class StderrSanitizer(contextlib.AbstractContextManager):
    """Context manager to ensure absolutely no debug logs or print statements
    leak into `stdout`, routing all diagnostics safely to `stderr`.

    Leaks into stdout corrupt the MCP JSON stream and crash the connection in
    Claude Desktop.
    """

    def __init__(self, target_stream: Any = None) -> None:
        self.target_stream = target_stream or sys.stderr
        self._redirector = None

    def __enter__(self) -> StderrSanitizer:
        # Redirect standard output to the designated target stream (stderr)
        self._redirector = contextlib.redirect_stdout(self.target_stream)
        self._redirector.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        if self._redirector:
            return self._redirector.__exit__(exc_type, exc_val, exc_tb)
        return False


def sanitize_stdout(target_stream: Any = None) -> Callable[[F], F]:
    """Decorator to sanitize standard output of a function, routing all output to stderr."""

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with StderrSanitizer(target_stream):
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with StderrSanitizer(target_stream):
                    return func(*args, **kwargs)

            return sync_wrapper  # type: ignore

    return decorator


# ==============================================================================
# 2. GracefulTimeoutHandler
# ==============================================================================


async def run_with_graceful_timeout(
    coro: Coroutine[Any, Any, Any],
    timeout_seconds: float,
    task_name: str = "nce_cognitive_task",
) -> Any:
    """Wrapper that runs the given coroutine. If the execution exceeds timeout_seconds,
    it returns a status payload indicating "Processing..." along with a task ID, while
    allowing the background task to continue processing safely.
    """
    task = asyncio.create_task(coro)
    try:
        # Wait for the task to complete within timeout_seconds.
        # Shield the task so that timeout cancellation doesn't kill the underlying job.
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        task_id = str(uuid.uuid4())
        log.warning(
            "Task '%s' exceeded timeout limit of %s seconds. "
            "Continuing execution in the background under task ID: %s",
            task_name,
            timeout_seconds,
            task_id,
        )

        async def background_runner() -> Any:
            try:
                result = await task
                log.info(
                    "Background task '%s' (ID: %s) completed successfully. Result payload size: %d",
                    task_name,
                    task_id,
                    len(str(result)),
                )
                return result
            except Exception as exc:
                log.exception(
                    "Background task '%s' (ID: %s) failed with exception: %s",
                    task_name,
                    task_id,
                    exc,
                )
                raise

        # Track the background task using the central background task manager
        create_tracked_task(background_runner(), name=f"bg_timeout_{task_name}")

        # Return a "Processing..." response envelope to prevent client UI timeout/rejection
        return json.dumps(
            {
                "status": "processing",
                "message": "The cognitive load exceeds standard timeout limits. Processing continues in the background.",
                "task_id": task_id,
                "estimated_wait_seconds": timeout_seconds,
            }
        )


def graceful_timeout(timeout_seconds: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to apply graceful timeout protection to an async handler."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            coro = func(*args, **kwargs)
            return await run_with_graceful_timeout(
                coro,
                timeout_seconds=timeout_seconds,
                task_name=func.__name__,
            )

        return wrapper

    return decorator


# ==============================================================================
# 3. SchemaEnforcer
# ==============================================================================


class SchemaEnforcer:
    """Pre-flight validator to ensure that NCE tool outputs strictly conform
    to the expected JSON schemas required by the client (Claude Desktop).
    """

    def __init__(self, schemas: dict[str, Any] | None = None) -> None:
        self.schemas = schemas or {}

    def register_schema(self, tool_name: str, schema: dict[str, Any]) -> None:
        """Register a target output JSON schema for a specific tool."""
        self.schemas[tool_name] = schema

    def validate_output(self, tool_name: str, output: Any) -> None:
        """Validate a tool's output against its registered JSON Schema.

        If output is a JSON string, it is parsed and validated.
        Raises ValueError or jsonschema.ValidationError on failure.
        """
        if tool_name not in self.schemas:
            log.debug("No output schema registered for tool '%s'. Skipping validation.", tool_name)
            return

        schema = self.schemas[tool_name]
        data = output

        if isinstance(output, str):
            try:
                data = json.loads(output)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Tool '{tool_name}' output is not valid JSON, cannot validate schema: {exc}"
                ) from exc

        try:
            jsonschema.validate(instance=data, schema=schema)
            log.info("Output schema verification passed for tool '%s'", tool_name)
        except jsonschema.ValidationError as exc:
            log.error(
                "Output schema verification failed for tool '%s': %s (path: %s)",
                tool_name,
                exc.message,
                list(exc.path),
            )
            raise ValueError(
                f"Tool '{tool_name}' output violated schema constraints: {exc.message}"
            ) from exc

    def enforce_schema(self, tool_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to wrap an async handler and enforce output schema validation."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                res = await func(*args, **kwargs)
                self.validate_output(tool_name, res)
                return res

            return wrapper

        return decorator


# --- Pre-configured Output Schemas ---

DEFAULT_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "store_memory": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "payload_ref": {"type": "string"},
            "contradiction": {
                "type": ["object", "null"],
                "properties": {
                    "contradiction_id": {"type": "string"},
                    "level": {"type": "string"},
                },
                "required": ["contradiction_id"],
            },
        },
        "required": ["status", "payload_ref"],
    },
    "store_artifact": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "payload_ref": {"type": "string"},
            "storage": {"type": "string", "enum": ["minio"]},
        },
        "required": ["status", "payload_ref", "storage"],
    },
    "store_media": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "payload_ref": {"type": "string"},
            "storage": {"type": "string", "enum": ["minio"]},
            "deprecated": {"type": "boolean"},
            "replacement": {"type": "string"},
        },
        "required": ["status", "payload_ref"],
    },
    "boost_memory": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "memory_id": {"type": "string"},
            "new_salience": {"type": "number"},
        },
        "required": ["status", "memory_id"],
    },
    "forget_memory": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "memory_id": {"type": "string"},
            "new_salience": {"type": "number"},
        },
        "required": ["status", "memory_id"],
    },
}

# Instantiate default enforcer
enforcer = SchemaEnforcer(DEFAULT_OUTPUT_SCHEMAS)
