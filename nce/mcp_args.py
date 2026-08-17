"""Shared argument shaping for MCP tools → Pydantic domain models.

Also provides MCP cache management — namespace-scoped cache key construction
and tenant-lifecycle-synchronised cache purging.

Nested validation (May 2026):
  ``SafeMetadataDict`` replaces bare ``dict[str, Any]`` on MCP input models
  (``StoreMemoryRequest.metadata``, ``CreateSnapshotRequest.metadata``, etc.).
  It uses a Pydantic ``AfterValidator`` to reject non-JSON-primitive values
  and nested dicts — closing the schema-pollution vector where arbitrary
  deeply-nested objects bypassed top-level ``extra='forbid'`` guards.

  ``validate_nested_models()`` is a recursive walker that can be applied to
  raw MCP arguments before model construction for defense-in-depth.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator

from nce.config import cfg

log = logging.getLogger(__name__)

# Keys supplied for transport/auth at the MCP layer, not part of domain payloads.
_MCP_AUTH_KEYS = frozenset({"admin_api_key", "mcp_api_key", "is_admin", "admin_identity"})

# Redis key prefix for MCP response cache entries.
_MCP_CACHE_PREFIX = "mcp_cache"

# Cache TTL for cacheable tool responses (seconds).
# Canonical value lives in nce.constants.MCP_CACHE_TTL_S (300 s).
# The old value here was 60 — stale, never matched the dispatch loop's 300 s.

# Redis key for the global cache-generation counter.
_MCP_CACHE_GENERATION_KEY: str = "mcp_cache_generation"

# Maximum serialized size of arguments when building a cache key.
_MAX_ARGUMENTS_JSON_SIZE: int = cfg.NCE_MAX_ARGUMENTS_JSON_SIZE  # 1 MB

# ---------------------------------------------------------------------------
# Strict nested validation — metadata / context sub-models
# ---------------------------------------------------------------------------

# Maximum number of metadata keys accepted on any MCP request.
_MAX_METADATA_KEYS: int = cfg.NCE_MAX_METADATA_KEYS

# Maximum length of a metadata key string.
_MAX_METADATA_KEY_LEN: int = cfg.NCE_MAX_METADATA_KEY_LEN

# Maximum length of a metadata string value.
_MAX_METADATA_STRING_VALUE_LEN: int = cfg.NCE_MAX_METADATA_STRING_VALUE_LEN

# Maximum number of items in a metadata list value.
_MAX_METADATA_LIST_ITEMS: int = cfg.NCE_MAX_METADATA_LIST_ITEMS


def _validate_metadata_values(v: dict[str, Any]) -> dict[str, Any]:
    """Reject metadata with non-JSON-primitive values or excessive nesting.

    Allowed value types: ``str``, ``int``, ``float``, ``bool``, ``None``,
    ``list[str|int|float|bool|None]``.

    Rejected: nested dicts (schema pollution), callables, custom objects,
    bytes, complex numbers.

    This is the boundary guard that closes the schema-pollution vector where
    arbitrary deeply-nested JSON objects bypass top-level ``extra='forbid'``
    Pydantic guards.  With this validator in place, metadata is constrained
    to flat key-value pairs of JSON-safe types — no injection of executable
    payloads, no deeply-nested object graphs.
    """
    if not isinstance(v, dict):
        raise ValueError("metadata must be a JSON object (dict)")

    if len(v) > _MAX_METADATA_KEYS:
        raise ValueError(f"metadata has {len(v)} keys — maximum {_MAX_METADATA_KEYS} allowed")

    _allowed = (str, int, float, bool, type(None))
    for key, val in v.items():
        if not isinstance(key, str):
            raise ValueError(f"metadata key {key!r} is not a string")
        if len(key) > _MAX_METADATA_KEY_LEN:
            raise ValueError(
                f"metadata key {key[:40]!r}... is {len(key)} chars — "
                f"maximum {_MAX_METADATA_KEY_LEN} allowed"
            )
        if isinstance(val, dict):
            raise ValueError(
                f"metadata['{key}'] is a nested dict — only flat key-value pairs are allowed"
            )
        if isinstance(val, list):
            if len(val) > _MAX_METADATA_LIST_ITEMS:
                raise ValueError(
                    f"metadata['{key}'] list has {len(val)} items — "
                    f"maximum {_MAX_METADATA_LIST_ITEMS} allowed"
                )
            for i, item in enumerate(val):
                if not isinstance(item, _allowed):
                    raise ValueError(
                        f"metadata['{key}'][{i}] has disallowed type "
                        f"{type(item).__name__} — only str, int, float, bool, None allowed"
                    )
        elif isinstance(val, str):
            if len(val) > _MAX_METADATA_STRING_VALUE_LEN:
                raise ValueError(
                    f"metadata['{key}'] string value is {len(val)} chars — "
                    f"maximum {_MAX_METADATA_STRING_VALUE_LEN} allowed"
                )
        elif not isinstance(val, _allowed):
            raise ValueError(
                f"metadata['{key}'] has disallowed type {type(val).__name__} — "
                "only str, int, float, bool, None allowed"
            )
    return v


#: A ``dict[str, Any]`` that is validated at the Pydantic boundary to reject
#: nested objects and non-JSON-primitive values.  Use this type annotation on
#: any MCP input model field that accepts arbitrary caller-supplied metadata.
SafeMetadataDict = Annotated[
    dict[str, Any],
    AfterValidator(_validate_metadata_values),
]


def validate_nested_models(
    arguments: dict[str, Any],
    *,
    nested_fields: dict[str, type] | None = None,
) -> dict[str, Any]:
    """Recursively validate nested dict fields in MCP arguments.

    This is a defense-in-depth layer applied **before** Pydantic model
    construction.  It walks the arguments dict and validates any field
    whose name appears in *nested_fields* by constructing the provided
    Pydantic model subclass on the nested value.

    When *nested_fields* is ``None`` or a field is not in the mapping,
    the nested value passes through unchanged.

    Returns a **new dict** with validated nested values substituted in.
    The original *arguments* dict is never modified.

    Raises ``ValueError`` (wrapping ``ValidationError``) on failure,
    with the field name and error details included in the message.
    """
    if not nested_fields:
        return arguments

    out = dict(arguments)
    for field_name, model_cls in nested_fields.items():
        nested = out.get(field_name)
        if nested is None:
            continue
        if not isinstance(nested, dict):
            raise ValueError(
                f"Expected a JSON object for '{field_name}', got {type(nested).__name__}"
            )
        try:
            validated = model_cls(**nested)
            out[field_name] = validated
        except Exception as exc:
            raise ValueError(f"Invalid nested field '{field_name}': {exc}") from exc
    return out


def model_kwargs(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop MCP auth/transport-only entries before ``**`` into ``extra='forbid'`` models."""
    return {k: v for k, v in arguments.items() if k not in _MCP_AUTH_KEYS}


# ---------------------------------------------------------------------------
# Namespace extraction from MCP arguments
# ---------------------------------------------------------------------------


def extract_namespace_id(arguments: dict[str, Any]) -> str | None:
    """Extract and validate ``namespace_id`` from MCP tool *arguments*.

    Returns the canonical UUID string, or ``None`` when the key is absent.

    Raises:
        ValueError: If ``namespace_id`` is present but is not a valid UUID.
                    Absent (key missing) → ``None``.
                    Present but invalid (bad format) → ``ValueError``.
    """
    raw = arguments.get("namespace_id")
    if raw is None:
        return None
    try:
        return str(UUID(str(raw)))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid namespace_id: {str(raw)[:64]!r}") from exc


def require_namespace_id(arguments: dict[str, Any]) -> str:
    """Like :func:`extract_namespace_id` but raises ``ValueError`` when absent.

    Use this in handlers where ``namespace_id`` is unconditionally required.
    Returns the canonical UUID string (lowercase, hyphenated).

    Raises:
        ValueError: If ``namespace_id`` is absent or not a valid UUID.
    """
    ns = extract_namespace_id(arguments)
    if ns is None:
        raise ValueError("namespace_id is required")
    return ns


# ---------------------------------------------------------------------------
# Cache key construction  (namespace-scoped)
# ---------------------------------------------------------------------------


def _canonicalize(obj: Any) -> Any:
    """Recursively normalize a value for deterministic JSON serialization.

    Converts UUID objects to their canonical string form so that
    ``UUID("abc...")`` and ``"abc..."`` produce the same JSON.
    Dict keys are sorted at every level (json.dumps sort_keys handles
    top-level only — this handles nested dicts too).
    """
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _canonicalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonicalize(v) for v in obj]
    return obj


def build_cache_key(
    tool_name: str,
    arguments: dict[str, Any],
    generation: int = 0,
    *,
    namespace_id: str | None = None,
) -> str:
    """Build a namespace-scoped MCP cache Redis key.

    Key format: ``mcp_cache:v{gen}:{ns}:{tool}:{args_sha256}``

    Arguments are filtered (auth/transport keys removed) and canonicalized
    before hashing so the same logical request always produces the same key
    regardless of dict ordering, UUID representation, or auth-key presence.

    Raises:
        ValueError: If tool_name exceeds 100 chars, arguments exceed
                    1 MB when serialized, or namespace_id is present but
                    not a valid UUID.
    """
    if len(tool_name) > 100:
        raise ValueError(f"tool_name too long for cache key: {len(tool_name)} chars (max 100)")

    # Strip auth/transport keys — different API keys must not produce
    # different cache entries for the same logical request.
    clean_args = model_kwargs(arguments)
    if namespace_id is None:
        namespace_id = extract_namespace_id(clean_args) or "global"

    norm = _canonicalize(clean_args)
    args_str = json.dumps(norm, separators=(",", ":"), sort_keys=True)
    if len(args_str) > _MAX_ARGUMENTS_JSON_SIZE:
        raise ValueError(
            f"arguments too large for cache key: "
            f"{len(args_str)} chars (limit {_MAX_ARGUMENTS_JSON_SIZE})"
        )
    args_hash = hashlib.sha256(args_str.encode()).hexdigest()
    return f"{_MCP_CACHE_PREFIX}:v{generation}:{namespace_id}:{tool_name}:{args_hash}"


# ---------------------------------------------------------------------------
# Cache purging  —  synchronised with tenant / document lifecycles
# ---------------------------------------------------------------------------


def _namespace_cache_pattern(namespace_id: str) -> str:
    """Return a Redis ``SCAN``-compatible glob pattern for all cache keys under *namespace_id*."""
    return f"{_MCP_CACHE_PREFIX}:v*:{namespace_id}:*"


def _document_cache_pattern(namespace_id: str, memory_id: str) -> str:
    """Return a Redis ``SCAN``-compatible glob for cache keys referencing a specific document."""
    return f"{_MCP_CACHE_PREFIX}:v*:{namespace_id}:*{memory_id}*"


async def purge_namespace_cache(
    redis_client: Any,
    namespace_id: str,
    *,
    batch_size: int = 100,
) -> int:
    """Delete **all** MCP cache entries scoped to *namespace_id*.

    Uses ``SCAN`` with a namespace-specific glob pattern to avoid blocking
    the Redis event loop (no ``KEYS *``).  Returns the number of deleted keys.

    Call this when a namespace (tenant) is deleted so stale cached responses
    from the deleted tenant cannot be served to new tenants reusing the
    same namespace UUID slot (rare) — and more importantly, so the cache
    does not retain references to deleted tenant data after the tenant's
    lifecycle ends.
    """
    pattern = _namespace_cache_pattern(namespace_id)
    deleted = 0
    cursor = 0

    while True:
        cursor, keys = await redis_client.scan(
            cursor=cursor,
            match=pattern,
            count=batch_size,
        )
        if keys:
            # redis-py / aioredis: ``delete`` accepts *keys
            n = await redis_client.delete(*keys)
            deleted += n
            log.info(
                "MCP cache: purged %d key(s) for namespace %s (batch cursor=%d)",
                n,
                namespace_id[:8],
                cursor,
            )
        if cursor == 0:
            break

    log.info(
        "MCP cache: namespace %s purge complete — %d key(s) deleted",
        namespace_id[:8],
        deleted,
    )
    return deleted


async def purge_document_cache(
    redis_client: Any,
    namespace_id: str,
    memory_id: str,
    *,
    batch_size: int = 100,
) -> int:
    """Delete MCP cache entries referencing a specific document / memory.

    Uses ``SCAN`` with a pattern matching the document ID within the
    namespace scope.  Returns the number of deleted keys.

    Call this when ``forget_memory`` (or equivalent) is invoked so the
    stale cached response for that document's search results is evicted
    proactively, rather than waiting for TTL expiry or the next global
    generation bump.
    """
    pattern = _document_cache_pattern(namespace_id, memory_id)
    deleted = 0
    cursor = 0

    while True:
        cursor, keys = await redis_client.scan(
            cursor=cursor,
            match=pattern,
            count=batch_size,
        )
        if keys:
            n = await redis_client.delete(*keys)
            deleted += n
        if cursor == 0:
            break

    if deleted:
        log.info(
            "MCP cache: purged %d key(s) for document %s in namespace %s",
            deleted,
            memory_id[:8],
            namespace_id[:8],
        )
    return deleted


async def bump_cache_generation(redis_client: Any) -> int:
    """Increment the global MCP cache generation counter in Redis.

    Returns the new generation value.
    This is a coarse invalidation: **all** cache entries with a lower
    generation become unreachable on the next read.  For fine-grained
    namespace-scoped purge, use :func:`purge_namespace_cache` instead.
    """
    gen = await redis_client.incr(_MCP_CACHE_GENERATION_KEY)
    return gen
