"""
MCP tool handlers for time-travel snapshots (§9).

Thin transport adapter — delegates data transformation to
``nce.snapshot_serializer`` so the snapshot serialization logic can be
unit-tested independently of the MCP transport layer.

Each handler receives the engine and raw arguments dict, builds a request
object via the serializer, calls the engine, and returns a JSON string
that ``call_tool()`` wraps in ``TextContent``.

Streaming export
────────────────
``stream_snapshot_export()`` is an async generator that yields NDJSON
lines for HTTP streaming.  It uses a server-side asyncpg cursor so the
full export is never materialised in Python heap memory — enabling
GB-scale tenant exports without crashing the orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine
from nce.snapshot_serializer import (
    SNAPSHOT_ARG_KEYS,
    build_compare_states_request,
    build_create_snapshot_request,
    serialize_delete_result,
    serialize_snapshot_list,
    serialize_snapshot_record,
    serialize_state_diff,
)

log = logging.getLogger("nce.snapshot_mcp_handlers")

# ── Stream batching constants ─────────────────────────────────────────────
_STREAM_BATCH_SIZE: int = 500  # rows per server-side cursor fetch
_STREAM_PROGRESS_INTERVAL: int = 1000  # emit a progress line every N rows
_MAX_EXPORT_ROWS: int = 1_000_000


@mcp_handler
async def handle_create_snapshot(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Create a named point-in-time reference for a namespace."""
    req = build_create_snapshot_request(arguments)
    res = await engine.create_snapshot(req)
    return serialize_snapshot_record(res)


@mcp_handler
async def handle_list_snapshots(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """List all snapshots for a namespace."""
    res = await engine.list_snapshots(arguments[SNAPSHOT_ARG_KEYS.NAMESPACE_ID])
    return serialize_snapshot_list(res)


@mcp_handler
async def handle_delete_snapshot(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Delete a point-in-time reference."""
    res = await engine.delete_snapshot(
        snapshot_id=arguments[SNAPSHOT_ARG_KEYS.SNAPSHOT_ID],
        namespace_id=arguments[SNAPSHOT_ARG_KEYS.NAMESPACE_ID],
    )
    return serialize_delete_result(res)


@mcp_handler
async def handle_compare_states(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Diff two temporal views of a namespace."""
    req = build_compare_states_request(arguments)
    res = await engine.compare_states(req)
    return serialize_state_diff(res)


# ── Streaming export ──────────────────────────────────────────────────────
# Used by the admin HTTP server to stream large snapshot exports as NDJSON.
# Avoids buffering the full dataset in RAM by using a server-side cursor.


async def stream_snapshot_export(
    engine: NCEEngine,
    namespace_id: str,
    *,
    as_of: datetime | None = None,
    snapshot_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Yield NDJSON lines for a namespace's full snapshot export.

    Uses a server-side asyncpg cursor to batch-fetch memories, keeping
    orchestrator RAM usage flat regardless of export size.

    Args:
        engine: The NCEEngine with connected pg_pool.
        namespace_id: Target namespace UUID as string.
        as_of: Point-in-time for the export (defaults to now).
        snapshot_id: Optional snapshot ID; resolved to ``as_of`` if given.

    Yields:
        NDJSON lines (``{"type": "metadata"|"memory"|"progress"|"complete"}``).
    """
    if not engine.pg_pool:
        yield json.dumps({"type": "error", "message": "Engine not connected"}) + "\n"
        return

    try:
        ns_uuid = uuid.UUID(namespace_id)
    except ValueError:
        yield json.dumps({"type": "error", "message": "Invalid namespace_id"}) + "\n"
        return

    export_as_of = as_of or datetime.now(timezone.utc)

    # ── Resolve snapshot_id to as_of ─────────────────────────────────────
    if snapshot_id:
        try:
            snap_uuid = uuid.UUID(snapshot_id)
            async with engine.pg_pool.acquire(timeout=10.0) as conn:
                snap_row = await conn.fetchrow(
                    "SELECT snapshot_at FROM snapshots WHERE id = $1 AND namespace_id = $2",
                    snap_uuid,
                    ns_uuid,
                )
                if snap_row:
                    export_as_of = snap_row["snapshot_at"]
                    log.info(
                        "Snapshot export resolved %s → as_of=%s",
                        snapshot_id,
                        export_as_of.isoformat(),
                    )
                else:
                    yield (
                        json.dumps(
                            {
                                "type": "error",
                                "message": f"Snapshot {snapshot_id} not found",
                            }
                        )
                        + "\n"
                    )
                    return
        except ValueError:
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "message": f"Invalid snapshot_id: {snapshot_id}",
                    }
                )
                + "\n"
            )
            return

    # ── Export header ────────────────────────────────────────────────────
    yield (
        json.dumps(
            {
                "type": "metadata",
                "namespace_id": namespace_id,
                "as_of": export_as_of.isoformat(),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "format": "nce-snapshot-export-v1",
            }
        )
        + "\n"
    )

    # ── Stream memories via server-side cursor ───────────────────────────
    total = 0
    try:
        async with engine.pg_pool.acquire(timeout=10.0) as conn:
            # Count total rows first (lightweight)
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*)::bigint AS total
                FROM memories
                WHERE namespace_id = $1
                  AND valid_from <= $2
                  AND (valid_to IS NULL OR valid_to > $2)
                """,
                ns_uuid,
                export_as_of,
            )
            total_expected = int(count_row["total"]) if count_row else 0

            if total_expected > _MAX_EXPORT_ROWS:
                yield (
                    json.dumps(
                        {
                            "type": "error",
                            "message": (
                                f"Export size {total_expected} rows exceeds the maximum "
                                f"of {_MAX_EXPORT_ROWS}. Use snapshot_id + filtering to export "
                                "smaller subsets."
                            ),
                        }
                    )
                    + "\n"
                )
                return

            # Server-side cursor — never materialises full result set
            async with conn.transaction():
                cursor = await conn.cursor(
                    """
                    SELECT
                        m.id AS memory_id,
                        m.namespace_id,
                        m.agent_id,
                        m.payload_ref,
                        m.assertion_type,
                        m.memory_type,
                        m.valid_from,
                        m.pii_redacted,
                        m.derived_from,
                        m.wrapped_dek,
                        COALESCE(m.metadata, '{}'::jsonb) AS metadata,
                        (SELECT ms.salience_score
                         FROM memory_salience ms
                         WHERE ms.memory_id = m.id
                           AND ms.namespace_id = m.namespace_id
                         ORDER BY ms.updated_at DESC NULLS LAST
                         LIMIT 1) AS salience
                    FROM memories m
                    WHERE m.namespace_id = $1
                      AND m.valid_from <= $2
                      AND (m.valid_to IS NULL OR m.valid_to > $2)
                    ORDER BY m.valid_from ASC, m.id ASC
                    """,
                    ns_uuid,
                    export_as_of,
                )

                _next_progress = _STREAM_PROGRESS_INTERVAL
                while True:
                    try:
                        batch = await asyncio.wait_for(
                            cursor.fetch(_STREAM_BATCH_SIZE),
                            timeout=30.0,
                        )
                    except asyncio.TimeoutError:
                        log.error(
                            "Snapshot export cursor timed out at row %d; aborting export.",
                            total,
                        )
                        yield (json.dumps({"type": "error", "message": "Export timed out"}) + "\n")
                        return
                    if not batch:
                        break
                    for row in batch:
                        memory = _serialize_memory_row(row)
                        yield (
                            json.dumps(
                                {
                                    "type": "memory",
                                    "memory": memory,
                                }
                            )
                            + "\n"
                        )
                        total += 1
                        if total >= _next_progress:
                            yield (
                                json.dumps(
                                    {
                                        "type": "progress",
                                        "exported": total,
                                        "total_expected": total_expected,
                                    }
                                )
                                + "\n"
                            )
                            _next_progress += _STREAM_PROGRESS_INTERVAL

    except Exception:
        log.exception("Snapshot export failed at memory %d", total)
        yield (
            json.dumps(
                {
                    "type": "error",
                    "message": f"Export failed after {total} records",
                }
            )
            + "\n"
        )
        return

    # ── Completion ───────────────────────────────────────────────────────
    yield (
        json.dumps(
            {
                "type": "complete",
                "exported": total,
                "total_expected": total_expected,
            }
        )
        + "\n"
    )

    log.info(
        "Snapshot export complete ns=%s as_of=%s exported=%d expected=%d",
        namespace_id,
        export_as_of.isoformat(),
        total,
        total_expected,
    )


def _serialize_memory_row(row: Any) -> dict[str, Any]:
    """Convert an asyncpg row to a JSON-safe dict.

    Args:
        row: An asyncpg record or dict-like object from a cursor fetch.

    Returns:
        A plain dict with UUIDs → str, datetimes → ISO strings.
    """
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, (bytes, bytearray, memoryview)):
            # Part II.4: wrapped_dek is BYTEA; hex-encode so it survives NDJSON
            # and import can decrypt the source ciphertext before re-storing.
            out[k] = bytes(v).hex()
        elif isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat() if v else None
        elif k == "metadata" and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                out[k] = {}
        else:
            out[k] = v
    return out


@mcp_handler
async def handle_import_snapshot(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Rebuild a namespace from an exported NDJSON snapshot via the Saga path."""
    target_ns = arguments["target_namespace_id"]
    snapshot_data = arguments["snapshot_data"]
    res = await restore_namespace(engine, target_ns, snapshot_data)
    return json.dumps(res)


async def restore_namespace(
    engine: NCEEngine,
    target_namespace_id: str,
    snapshot_data: str,
) -> dict[str, Any]:
    """Rebuild a namespace from an exported NDJSON snapshot via the Saga path.

    Note: Reusing deterministic remap once Phase H lands. Until then, this
    performs a non-verifiable restore (new UUIDs and signature versions are
    generated fresh).

    Args:
        engine: The NCEEngine with connected mongo_client.
        target_namespace_id: Target namespace UUID as string.
        snapshot_data: Raw NDJSON snapshot string containing metadata and memories.

    Returns:
        A dict with import status, count of imported records, and errors.
    """
    from bson import ObjectId

    from nce.models import AssertionType, MemoryType, StoreMemoryRequest

    if not engine.mongo_client:
        return {"status": "error", "message": "MongoDB client not connected"}

    try:
        target_uuid = uuid.UUID(target_namespace_id)
    except ValueError:
        return {"status": "error", "message": "Invalid target_namespace_id"}

    imported_count = 0
    errors = []

    db = engine.mongo_client.memory_archive
    episodes_col = db.episodes

    lines = snapshot_data.strip().split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"Line {i + 1}: Invalid JSON: {str(e)}")
            continue

        rec_type = record.get("type")
        if rec_type != "memory":
            continue

        memory_data = record.get("memory")
        if not memory_data:
            errors.append(f"Line {i + 1}: Missing memory details")
            continue

        payload_ref = memory_data.get("payload_ref")
        if not payload_ref:
            errors.append(f"Line {i + 1}: Missing payload_ref in memory details")
            continue

        try:
            doc = await episodes_col.find_one({"_id": ObjectId(payload_ref)})
        except Exception as e:
            errors.append(
                f"Line {i + 1}: MongoDB fetch failed for payload_ref {payload_ref}: {str(e)}"
            )
            continue

        if not doc:
            errors.append(f"Line {i + 1}: MongoDB document not found for payload_ref {payload_ref}")
            continue

        # Part II.4: decrypt the source ciphertext (if encrypted) back to plaintext
        # before re-storing; store_memory will re-encrypt under a fresh DEK if the
        # target has envelope encryption enabled.  Legacy docs read as plaintext.
        from nce.envelope import maybe_decrypt_raw_data

        wrapped_hex = memory_data.get("wrapped_dek")
        wrapped_bytes = bytes.fromhex(wrapped_hex) if wrapped_hex else None
        raw_data = maybe_decrypt_raw_data(doc.get("raw_data", ""), wrapped_bytes)

        # Merge metadata with salience and bypass_quarantine
        metadata = dict(memory_data.get("metadata") or {})
        if "salience" in memory_data:
            try:
                metadata["salience"] = float(memory_data["salience"])
            except (ValueError, TypeError):
                pass
        metadata["bypass_quarantine"] = True

        derived_from_raw = memory_data.get("derived_from")
        derived_from = None
        if derived_from_raw:
            if isinstance(derived_from_raw, list):
                derived_from = [uuid.UUID(uid) for uid in derived_from_raw]
            else:
                try:
                    derived_from = json.loads(derived_from_raw)
                    derived_from = [uuid.UUID(uid) for uid in derived_from]
                except Exception:
                    pass

        try:
            req = StoreMemoryRequest(
                namespace_id=target_uuid,
                agent_id=memory_data.get("agent_id", "default"),
                content=raw_data,
                summary=raw_data[:8192],
                heavy_payload=raw_data,
                memory_type=MemoryType(memory_data.get("memory_type", "episodic")),
                assertion_type=AssertionType(memory_data.get("assertion_type", "fact")),
                metadata=metadata,
                derived_from=derived_from,
                check_contradictions=False,
            )
            await engine.store_memory(req)
            imported_count += 1
        except Exception as e:
            errors.append(f"Line {i + 1}: Ingest failed: {str(e)}")

    if errors and imported_count == 0:
        return {
            "status": "error",
            "message": "All records failed to import",
            "errors": errors,
        }

    return {
        "status": "ok",
        "imported": imported_count,
        "errors": errors,
    }
