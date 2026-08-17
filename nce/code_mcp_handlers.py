"""
MCP tool handlers for code indexing operations (§4). Extracted from server.py:call_tool().
Follows the same pattern as bridge_mcp_handlers.py — each handler receives the engine
and raw arguments dict, and returns a JSON string that call_tool() wraps in TextContent.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from nce.config import cfg
from nce.constants import ALLOWED_LANGUAGES as _ALLOWED_LANGUAGES
from nce.mcp_args import require_namespace_id as _require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.models import IndexCodeFileRequest
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.code_mcp_handlers")

# Safe filepath pattern: no traversal, no null bytes, no leading slashes.
_SAFE_FILEPATH_RE = re.compile(r"^[\w][\w/\\.@\-]{0,511}$")

# Clamped top_k for semantic code search.
_TOP_K_MIN = 1
_TOP_K_MAX = 50
_TOP_K_DEFAULT = 5


def _validate_language(raw: Any) -> str:
    """Normalize and allowlist a language identifier."""
    lang = str(raw).strip().lower()
    if lang not in _ALLOWED_LANGUAGES:
        raise ValueError(
            f"language {lang!r} is not supported. Allowed: {', '.join(sorted(_ALLOWED_LANGUAGES))}"
        )
    return lang


def _validate_filepath(raw: Any) -> str:
    """Reject path traversal and overly long or suspicious filepaths."""
    path = str(raw).strip()
    if not path:
        raise ValueError("filepath is required")
    if ".." in path.split("/") or ".." in path.split("\\"):
        raise ValueError("filepath must not contain path traversal components")
    if not _SAFE_FILEPATH_RE.match(path):
        raise ValueError("filepath contains invalid characters or is too long (max 512 chars)")
    return path


def _clamp_top_k(raw: Any) -> int:
    """Parse and clamp top_k to a safe range."""
    return max(_TOP_K_MIN, min(int(raw), _TOP_K_MAX))


def _bool_arg(arguments: dict[str, Any], key: str, *, default: bool) -> bool:
    """Parse a boolean MCP argument safely — treats string "false"/"0"/"no"/"off" as False."""
    val = arguments.get(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


@mcp_handler
async def handle_index_code_file(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Index a source code file into the Tri-Stack. Runs asynchronously — returns a job_id.

    Routes to the ``high_priority`` queue lane so user-facing MCP calls
    are never blocked behind batch indexing jobs (§5.4).

    Required: filepath, raw_code, language, namespace_id.
    """
    namespace_id = _require_namespace_id(arguments)
    filepath = _validate_filepath(arguments.get("filepath", ""))
    language = _validate_language(arguments.get("language", ""))

    raw_code = arguments.get("raw_code", "")
    if not raw_code:
        raise ValueError("raw_code is required")
    raw_bytes = len(raw_code.encode("utf-8"))
    if raw_bytes > cfg.NCE_MAX_CODE_INDEX_BYTES:
        raise ValueError(
            f"raw_code exceeds maximum size: {raw_bytes} bytes "
            f"(limit: {cfg.NCE_MAX_CODE_INDEX_BYTES} bytes). "
            "Split large files before indexing."
        )

    result = await engine.index_code_file(
        IndexCodeFileRequest(
            filepath=filepath,
            raw_code=raw_code,
            language=language,
            namespace_id=namespace_id,
            user_id=arguments.get("user_id"),
            # Default to private=True: caller must explicitly opt out.
            private=_bool_arg(arguments, "private", default=True),
        ),
        priority=10,  # high-priority lane for real-time API calls
    )
    return json.dumps(result)


@mcp_handler
async def handle_check_indexing_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Check the status of a background indexing job.

    Required: job_id (non-empty string).
    """
    raw_job_id = arguments.get("job_id")
    if not raw_job_id or not str(raw_job_id).strip():
        raise ValueError("job_id is required")
    job_id = str(raw_job_id).strip()

    result = await engine.get_job_status(job_id=job_id)
    return json.dumps(result)


@mcp_handler
async def handle_search_codebase(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Semantic search over indexed code chunks. Returns matching functions/classes.

    Required: query, namespace_id.
    Optional: language_filter (allowlisted), top_k (1–50), user_id, private, aspect.
    """
    namespace_id = _require_namespace_id(arguments)

    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")

    language_filter: str | None = None
    raw_lang = arguments.get("language_filter")
    if raw_lang:
        language_filter = _validate_language(raw_lang)

    top_k = _clamp_top_k(arguments.get("top_k", _TOP_K_DEFAULT))

    aspect = arguments.get("aspect")
    if aspect is not None:
        aspect = str(aspect).strip()

    results = await engine.search_codebase(
        query=query,
        namespace_id=namespace_id,
        language_filter=language_filter,
        top_k=top_k,
        user_id=arguments.get("user_id"),
        # Default to private=True: callers must opt out explicitly.
        private=_bool_arg(arguments, "private", default=True),
        aspect=aspect,
    )
    return json.dumps(results)
