"""
Snapshot serialization — purely functional data transformation layer.

Extracted from ``snapshot_mcp_handlers.py`` to decouple snapshot data aggregation
and JSON serialization from the MCP transport layer. Every function in this
module is synchronous, stateless, and depends only on Pydantic models — no
engine references, no async I/O, no transport concerns.

Unit-testable without mocks::

    from nce.snapshot_serializer import serialize_snapshot_record, SNAPSHOT_ARG_KEYS
    assert SNAPSHOT_ARG_KEYS.NAMESPACE_ID == "namespace_id"
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nce.models import (
    CompareStatesRequest,
    CreateSnapshotRequest,
    DeleteSnapshotResult,
    SnapshotRecord,
    StateDiffResult,
)

# ── Argument key constants ───────────────────────────────────────────────────
# Replace magic-string lookups in MCP handlers with typed, greppable constants.


@dataclass(frozen=True)
class _SnapshotArgKeys:
    """Dict keys expected in ``arguments`` dicts for snapshot MCP tools.

    Centralising these eliminates magic strings scattered across handler code.
    """

    NAMESPACE_ID: str = "namespace_id"
    NAME: str = "name"
    AGENT_ID: str = "agent_id"
    SNAPSHOT_AT: str = "snapshot_at"
    METADATA: str = "metadata"
    SNAPSHOT_ID: str = "snapshot_id"
    AS_OF_A: str = "as_of_a"
    AS_OF_B: str = "as_of_b"
    QUERY: str = "query"
    TOP_K: str = "top_k"


SNAPSHOT_ARG_KEYS = _SnapshotArgKeys()

# Sentinel for default-agent lookups — avoids repeating "default" as a literal.
_DEFAULT_AGENT_ID: str = "default"
_DEFAULT_TOP_K: int = 10
_MAX_QUERY_LEN: int = 2_048


# ── Pure serializers ─────────────────────────────────────────────────────────


def serialize_snapshot_record(record: SnapshotRecord) -> str:
    """Serialize a single ``SnapshotRecord`` to a JSON string.

    Args:
        record: A validated snapshot record from the database.

    Returns:
        A JSON string suitable for ``TextContent`` wrapping.
    """
    return json.dumps(record.model_dump(mode="json"), default=str)


def serialize_snapshot_list(records: list[SnapshotRecord]) -> str:
    """Serialize a list of ``SnapshotRecord`` to a JSON string."""
    return json.dumps([s.model_dump(mode="json") for s in records], default=str)


def serialize_delete_result(result: DeleteSnapshotResult) -> str:
    """Serialize the ``DeleteSnapshotResult`` to a JSON string."""
    return json.dumps(result.model_dump(mode="json"), default=str)


def serialize_state_diff(diff: StateDiffResult) -> str:
    """Serialize a ``StateDiffResult`` to a JSON string.

    Args:
        diff: A validated state-diff result from ``compare_states``.

    Returns:
        A JSON string suitable for ``TextContent`` wrapping.
    """
    return json.dumps(diff.model_dump(mode="json"), default=str)


# ── Request builders ─────────────────────────────────────────────────────────
# These extract and coerce raw MCP ``arguments`` dict entries into validated
# Pydantic request objects.  Each builder is a pure function — no I/O, no
# engine coupling, trivially unit-testable.


def build_create_snapshot_request(arguments: dict[str, Any]) -> CreateSnapshotRequest:
    """Construct a ``CreateSnapshotRequest`` from a raw MCP arguments dict.

    Args:
        arguments: The raw arguments dict received by ``handle_create_snapshot``.

    Returns:
        A validated ``CreateSnapshotRequest`` instance.
    """
    from nce.auth import validate_agent_id
    from nce.temporal import parse_as_of

    # --- name ---
    raw_name = arguments.get(SNAPSHOT_ARG_KEYS.NAME)
    if not raw_name:
        raise ValueError("name is required")
    name = str(raw_name).strip()
    if not name or len(name) > 256:
        raise ValueError(f"name must be between 1 and 256 characters, got {len(name)!r}")

    # --- agent_id ---
    agent_id = validate_agent_id(
        str(arguments.get(SNAPSHOT_ARG_KEYS.AGENT_ID) or _DEFAULT_AGENT_ID)
    )

    # --- metadata (fresh dict, never shared reference) ---
    meta_raw = arguments.get(SNAPSHOT_ARG_KEYS.METADATA)
    if meta_raw is not None and not isinstance(meta_raw, dict):
        raise ValueError(f"metadata must be a JSON object, got {type(meta_raw).__name__!r}")
    metadata = dict(meta_raw) if isinstance(meta_raw, dict) else {}

    return CreateSnapshotRequest(
        namespace_id=arguments[SNAPSHOT_ARG_KEYS.NAMESPACE_ID],
        name=name,
        agent_id=agent_id,
        snapshot_at=parse_as_of(arguments.get(SNAPSHOT_ARG_KEYS.SNAPSHOT_AT)),
        metadata=metadata,
    )


def build_compare_states_request(arguments: dict[str, Any]) -> CompareStatesRequest:
    """Construct a ``CompareStatesRequest`` from a raw MCP arguments dict.

    Args:
        arguments: The raw arguments dict received by ``handle_compare_states``.

    Returns:
        A validated ``CompareStatesRequest`` instance.
    """
    from nce.models import _MAX_TOP_K
    from nce.temporal import parse_as_of

    as_of_a = parse_as_of(arguments.get(SNAPSHOT_ARG_KEYS.AS_OF_A))
    as_of_b = parse_as_of(arguments.get(SNAPSHOT_ARG_KEYS.AS_OF_B))
    if as_of_a is None or as_of_b is None:
        raise ValueError("compare_states requires both as_of_a and as_of_b timestamps")
    if as_of_a >= as_of_b:
        raise ValueError(
            f"as_of_a must be strictly before as_of_b "
            f"(got as_of_a={as_of_a.isoformat()}, as_of_b={as_of_b.isoformat()})"
        )

    raw_query = arguments.get(SNAPSHOT_ARG_KEYS.QUERY)
    if raw_query is not None and len(str(raw_query)) > _MAX_QUERY_LEN:
        raise ValueError(f"query exceeds maximum length of {_MAX_QUERY_LEN} characters")
    query = raw_query

    raw_top_k = arguments.get(SNAPSHOT_ARG_KEYS.TOP_K, _DEFAULT_TOP_K)
    top_k = max(1, min(int(raw_top_k), _MAX_TOP_K))

    return CompareStatesRequest(
        namespace_id=arguments[SNAPSHOT_ARG_KEYS.NAMESPACE_ID],
        as_of_a=as_of_a,
        as_of_b=as_of_b,
        query=query,
        top_k=top_k,
    )
