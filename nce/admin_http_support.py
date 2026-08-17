"""Shared helpers for admin_server HTTP handlers."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from nce.config import cfg

log = logging.getLogger("nce-admin")

_INVALID_JSON_BODY = JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)
_ENGINE_UNAVAILABLE = JSONResponse({"error": "Engine not connected"}, status_code=503)

# Starlette/uvicorn may surface decode errors as ValueError or TypeError.
_REQUEST_JSON_ERRORS = (json.JSONDecodeError, ValueError, TypeError)


async def read_admin_json_body(request: Request) -> dict | JSONResponse:
    """Parse JSON object body or return a 400 JSONResponse."""
    try:
        body = await request.json()
    except _REQUEST_JSON_ERRORS:
        return _INVALID_JSON_BODY
    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
    return body


def engine_unavailable() -> JSONResponse | None:
    from nce import admin_state

    if not admin_state.engine:
        return _ENGINE_UNAVAILABLE
    return None


def admin_error_response(
    message: str,
    exc: Exception,
    *,
    status_code: int = 500,
    log_event: str | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    """Log full exception; omit internal details from JSON in production."""
    if log_event:
        log.exception("%s", log_event)
    else:
        log.exception("%s: %s", message, exc)
    body: dict[str, object] = {"error": message}
    if extra:
        body.update(extra)
    if not cfg.IS_PROD:
        body["detail"] = str(exc)
    return JSONResponse(body, status_code=status_code)


def admin_client_error(
    message: str,
    *,
    status_code: int = 400,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    """Return a fixed, caller-vetted error message (safe for production)."""
    body: dict[str, object] = {"error": message}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status_code)


def admin_validation_error(
    exc: Exception,
    *,
    status_code: int = 422,
    message: str | None = None,
) -> JSONResponse:
    """Map validation/domain errors to client JSON without leaking internals in prod."""
    from pydantic import ValidationError as PydanticValidationError

    if isinstance(exc, PydanticValidationError):
        if cfg.IS_PROD:
            return admin_client_error(message or "Validation failed", status_code=status_code)
        return admin_client_error(
            message or "Validation failed",
            status_code=status_code,
            extra={"detail": exc.errors()},
        )
    if isinstance(exc, (ValueError, KeyError)):
        return admin_client_error(
            message or str(exc) or "Invalid request",
            status_code=status_code,
        )
    if isinstance(exc, RuntimeError):
        return admin_client_error(
            str(exc) or message or "Request failed",
            status_code=status_code,
        )
    if message:
        return admin_client_error(message, status_code=status_code)
    log.warning("Sanitized unexpected admin client error (%s): %s", type(exc).__name__, exc)
    fallback = "Invalid request" if cfg.IS_PROD else (str(exc) or "Invalid request")
    return admin_client_error(fallback, status_code=status_code)


def sanitize_admin_reason(exc: Exception) -> str:
    """Short reason string safe to embed in list payloads (e.g. bulk verify)."""
    if isinstance(exc, (ValueError, KeyError)):
        return str(exc) or type(exc).__name__
    if cfg.IS_PROD:
        return type(exc).__name__
    return str(exc) or type(exc).__name__


def update_dotenv(updates: dict[str, str]) -> None:
    """Update key-value pairs in the local ``.env`` file (dev-only; atomic write)."""
    if not cfg.NCE_ALLOW_ADMIN_DOTENV_PERSIST:
        raise RuntimeError(
            "Persisting admin configuration to .env is disabled. "
            "Set NCE_ALLOW_ADMIN_DOTENV_PERSIST=true for local development only."
        )
    if not updates:
        return

    import tempfile

    dotenv_path = Path(".env")
    if dotenv_path.exists():
        lines = dotenv_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = []

    new_lines: list[str] = []
    keys_updated: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                keys_updated.add(k)
                continue
        new_lines.append(line if line.endswith("\n") else f"{line}\n")

    for k, v in updates.items():
        if k not in keys_updated:
            new_lines.append(f"{k}={v}\n")

    content = "".join(new_lines)
    parent = dotenv_path.parent if str(dotenv_path.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f"{dotenv_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, dotenv_path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def mask_uri_password(uri: str) -> str:
    """Mask the password field of a standard connection URI with dots."""
    if not uri:
        return ""
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(uri)
        if parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            if parsed.username:
                netloc = f"{parsed.username}:••••••••@{netloc}"
            else:
                netloc = f":••••••••@{netloc}"
            return urlunparse(parsed._replace(netloc=netloc))
        return uri
    except (ValueError, AttributeError):
        return uri


def serialize_pg_row(row: Any) -> dict:
    """Convert an asyncpg Record to a JSON-serialisable dict."""
    from datetime import datetime, timezone
    from decimal import Decimal

    utc = timezone.utc
    d = row if isinstance(row, dict) else dict(row)
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(utc).isoformat() if v else None
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif hasattr(v, "hex"):
            out[k] = str(v)
        else:
            out[k] = v
    return out
