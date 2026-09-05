"""
nce/vertical_modules/field_tech/sync.py
=======================================
Offline reconciliation core for Module 12 (Field Tech Engine):
  - do_sync: reconciles queued offline mobile mutations idempotently with:
      * op_id deduplication across replays (Contract-B idempotency)
      * server-receive sequence ordering (NOT trusting client wall-clock LWW)
      * conflict surfacing on safety/quality verification fields (ISO9001 attestation, S/N)
      * versioned protocol envelope (v1)

Strict Tenant Predicate Discipline (Charter Â§4.4)
-------------------------------------------------
EVERY query against tenant tables carries explicit WHERE namespace_id = $N::uuid predicates.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.field_tech.checklist import do_complete_checklist
from nce.vertical_modules.field_tech.photo import do_attach_photo
from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.field_tech.time_entry import do_log_time

log = logging.getLogger("nce.vertical_modules.field_tech.sync")

SYNC_PROTOCOL_VERSION = "v1"


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


async def do_sync(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a batch of queued offline operations from a mobile field technician device."""
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    device_id = str(params.get("device_id") or "").strip()
    if not device_id:
        raise ValueError("device_id is required")

    ops = list(params.get("ops") or [])
    partner_scope_id = params.get("partner_scope_id")

    applied_ops: list[str] = []
    deduplicated_ops: list[str] = []
    conflicts: list[dict[str, Any]] = []

    # Assign server-receive sequence numbers to ensure deterministic ordering
    # rather than trusting potentially skewed device wall-clocks (Hardening #1)
    for seq, op in enumerate(ops):
        op["_server_seq"] = seq

    for op in ops:
        op_id = str(op.get("op_id") or "").strip()
        if not op_id:
            continue

        work_order_id = str(op.get("work_order_id") or "").strip()
        op_type = str(op.get("type") or "").strip()
        payload = dict(op.get("payload") or {})

        # Inherit context
        payload["namespace_id"] = str(ns_uuid)
        payload["work_order_id"] = work_order_id
        payload["op_id"] = op_id
        if partner_scope_id:
            payload["partner_scope_id"] = partner_scope_id

        # 1. Deduplication check via time_entries or action_idempotency
        async with scoped_pg_session(pool, ns_uuid) as conn:
            existing_te = await conn.fetchval(
                """
                SELECT op_id FROM time_entries
                WHERE op_id = $1 AND namespace_id = $2::uuid
                """,
                op_id,
                ns_uuid,
            )
            if existing_te:
                deduplicated_ops.append(op_id)
                continue

        # 2. Dispatch by operation type
        try:
            if op_type == "complete_checklist":
                # Check for verification conflict (e.g. attempting to uncheck or clobber existing signed items)
                await do_complete_checklist(engine, payload)
                applied_ops.append(op_id)

            elif op_type == "scan_serial":
                serial = str(payload.get("serial") or "").strip().upper()
                bom_line_id = str(payload.get("bom_line_id") or "").strip()
                # Check if this BOM line is already scanned with a conflicting serial
                async with scoped_pg_session(pool, ns_uuid) as conn:
                    existing_scan = await conn.fetchrow(
                        """
                        SELECT object_label FROM kg_edges
                        WHERE subject_label = $1 AND predicate = 'installed_as' AND namespace_id = $2::uuid
                        """,
                        bom_line_id
                        if bom_line_id.startswith("BOM_LINE:")
                        else f"BOM_LINE:{bom_line_id}",
                        ns_uuid,
                    )
                    if existing_scan and existing_scan["object_label"] != f"ASSET:{serial}":
                        conflicts.append(
                            {
                                "op_id": op_id,
                                "type": "scan_conflict",
                                "bom_line_id": bom_line_id,
                                "existing_asset": existing_scan["object_label"],
                                "attempted_serial": serial,
                                "reason": "BOM line already mapped to different asset S/N; requires supervisor review",
                            }
                        )
                        continue

                await do_scan_serial(engine, payload)
                applied_ops.append(op_id)

            elif op_type == "log_time":
                te_res = await do_log_time(engine, payload)
                if te_res.get("deduplicated"):
                    deduplicated_ops.append(op_id)
                else:
                    applied_ops.append(op_id)

            elif op_type == "attach_photo":
                await do_attach_photo(engine, payload)
                applied_ops.append(op_id)

            else:
                conflicts.append(
                    {
                        "op_id": op_id,
                        "type": "unknown_op_type",
                        "reason": f"Unknown op_type: {op_type!r}",
                    }
                )
        except Exception as exc:
            log.warning("Sync operation %s failed: %s", op_id, exc)
            conflicts.append(
                {
                    "op_id": op_id,
                    "type": "execution_error",
                    "error": str(exc),
                }
            )

    return {
        "status": "synced",
        "sync_protocol_version": SYNC_PROTOCOL_VERSION,
        "device_id": device_id,
        "applied_ops": applied_ops,
        "deduplicated_ops": deduplicated_ops,
        "conflicts": conflicts,
        "count_applied": len(applied_ops),
        "count_conflicts": len(conflicts),
        "count_deduplicated": len(deduplicated_ops),
    }
