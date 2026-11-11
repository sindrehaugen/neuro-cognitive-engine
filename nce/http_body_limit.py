"""Pure-ASGI request body size limiter middleware (HTTP 413 Payload Too Large)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

_DEFAULT_MAX_HTTP_BODY_BYTES: int = 10_485_760


class _BodyTooLargeError(Exception):
    """Internal exception raised when streaming request body exceeds configured limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"Request body exceeds maximum allowed size of {limit} bytes.")
        self.limit = limit


def _resolve_default_limit() -> int:
    raw = os.environ.get("NCE_MAX_HTTP_BODY_BYTES")
    if raw is not None and raw.strip():
        try:
            return max(1, int(raw.strip()))
        except ValueError:
            pass
    cfg_mod = sys.modules.get("nce.config")
    if cfg_mod is not None and hasattr(cfg_mod, "live_max_http_body_bytes"):
        try:
            return int(cfg_mod.live_max_http_body_bytes())
        except Exception:
            pass
    return _DEFAULT_MAX_HTTP_BODY_BYTES


def _build_413_response(limit: int) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.dumps(
        {
            "error": "payload_too_large",
            "detail": f"Request body exceeds maximum allowed size of {limit} bytes.",
        }
    ).encode("utf-8")
    start_message = {
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    }
    body_message = {
        "type": "http.response.body",
        "body": payload,
    }
    return start_message, body_message


class BoundedBodyMiddleware:
    """Pure-ASGI middleware that rejects oversized HTTP request bodies with 413 Payload Too Large.

    Inspects Content-Length before dispatch and enforces bounded accumulation on
    chunked/streaming request bodies without buffering unbounded streams into memory.
    """

    def __init__(
        self,
        app: Any,
        max_bytes: int | Callable[[], int] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _get_limit(self) -> int:
        if callable(self.max_bytes):
            try:
                return max(1, int(self.max_bytes()))
            except Exception:
                return _resolve_default_limit()
        if isinstance(self.max_bytes, int):
            return max(1, self.max_bytes)
        return _resolve_default_limit()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = self._get_limit()

        # 1. Content-Length header inspection
        content_length_header: bytes | None = None
        for key, val in scope.get("headers", []):
            if key.lower() == b"content-length":
                content_length_header = val
                break

        if content_length_header is not None:
            try:
                cl_val = int(content_length_header.decode("latin1").strip())
                if cl_val < 0 or cl_val > limit:
                    start_msg, body_msg = _build_413_response(limit)
                    await send(start_msg)
                    await send(body_msg)
                    return
            except (ValueError, UnicodeDecodeError):
                start_msg, body_msg = _build_413_response(limit)
                await send(start_msg)
                await send(body_msg)
                return

        # 2. Chunked / Content-Length-omitted write requests: bound at perimeter
        method = str(scope.get("method", "")).upper()
        if content_length_header is None and method in ("POST", "PUT", "PATCH"):
            buffered_messages: list[dict[str, Any]] = []
            chunk_bytes = 0
            while True:
                message = await receive()
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"")
                    if chunk:
                        chunk_bytes += len(chunk)
                        if chunk_bytes > limit:
                            start_msg, body_msg = _build_413_response(limit)
                            await send(start_msg)
                            await send(body_msg)
                            return
                    buffered_messages.append(message)
                    if not message.get("more_body", False):
                        break
                else:
                    buffered_messages.append(message)
                    break

            idx = 0

            async def replay_receive() -> dict[str, Any]:
                nonlocal idx
                if idx < len(buffered_messages):
                    msg = buffered_messages[idx]
                    idx += 1
                    return msg
                return await receive()

            await self.app(scope, replay_receive, send)
            return

        # 3. Streaming guard when Content-Length is present (defends against lying headers)
        bytes_received = 0
        response_started = False

        async def wrapped_receive() -> dict[str, Any]:
            nonlocal bytes_received
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    bytes_received += len(chunk)
                    if bytes_received > limit:
                        raise _BodyTooLargeError(limit)
            return message

        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except _BodyTooLargeError as exc:
            if not response_started:
                start_msg, body_msg = _build_413_response(exc.limit)
                await send(start_msg)
                await send(body_msg)
            else:
                raise
