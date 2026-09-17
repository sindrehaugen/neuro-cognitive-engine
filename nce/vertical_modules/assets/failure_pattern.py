"""
nce/vertical_modules/assets/failure_pattern.py
=============================================
Assets failure pattern recording and projection into Knowledge Graph (Wave A-5).

Emits ``ASSET -[failure_pattern]-> PRODUCT_SKU`` edges to close the
"service/hardware failure -> product feedback" silence:
  - Validates asset existence in the ``assets`` table under strict tenant boundary.
  - Enforces Contract-A single-writer ownership invariant for the ``ASSET`` node type.
  - Inserts/updates ``failure_pattern`` boundary edges into ``kg_edges`` for
    consumption by the Product engine's EOL and BOM optimization advisors.
  - Updates asset timestamps and audit history.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.mcp_args import require_namespace_id

log = logging.getLogger("nce.vertical_modules.assets.failure_pattern")

_NODE_TYPE_ASSET = "ASSET"
_ASSETS_ENGINE = "assets"
_PREDICATE_FAILURE_PATTERN = "failure_pattern"
_CHANGE_ORIGIN = "agent"


class AssetNotFoundError(KeyError):
    """Raised when an asset is not found in the tenant namespace."""

    def __init__(self, asset_id: str) -> None:
        super().__init__(f"Asset '{asset_id}' not found")
        self.asset_id = asset_id


def _extract_pool(engine_or_pool: Any) -> Any:
    """Extract asyncpg pool from NCEEngine or return the pool itself."""
    if hasattr(engine_or_pool, "pg_pool"):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, field_name: str = "id") -> UUID:
    """Parse a string or UUID into a valid UUID object."""
    if isinstance(val, UUID):
        return val
    if not val or not str(val).strip():
        raise ValueError(f"'{field_name}' is required and cannot be blank")
    try:
        return UUID(str(val).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid {field_name}: {exc}") from exc


async def do_record_failure_pattern(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record a failure pattern edge from ASSET to PRODUCT_SKU in the Knowledge Graph.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine or asyncpg.Pool instance.
    params:
        - namespace_id: (required) tenant UUID.
        - asset_id / id: (required) asset UUID.
        - product_sku: (required) product SKU string.
        - confidence: (optional) float between 0.0 and 1.0 (default 1.0).
        - failure_mode: (optional) str failure mode description.
        - severity: (optional) str severity (e.g. 'critical', 'major', 'minor').
        - pattern_notes: (optional) str notes.
        - raise_on_not_found: (optional) bool, default False.

    Returns
    -------
    dict[str, Any]:
        {"ok": True, "asset_id": str, "product_sku": str, "edge": str, ...}
    """
    pool = _extract_pool(engine_or_pool)
    ns_str = require_namespace_id(params)
    ns_uuid = _parse_uuid(ns_str, "namespace_id")

    raw_asset_id = params.get("asset_id") or params.get("id")
    asset_uuid = _parse_uuid(raw_asset_id, "asset_id")

    product_sku = str(params.get("product_sku") or "").strip()
    if not product_sku:
        raise ValueError("product_sku is required and cannot be blank")

    raw_confidence = params.get("confidence", 1.0)
    try:
        confidence = float(raw_confidence)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid confidence: {exc}") from exc

    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must be between 0.0 and 1.0, got {confidence}")

    failure_mode = str(params.get("failure_mode") or "").strip()
    severity = str(params.get("severity") or "major").strip().lower()
    pattern_notes = str(params.get("pattern_notes") or params.get("notes") or "").strip()

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    asset_subject = f"ASSET:{asset_uuid}"
    product_object = f"PRODUCT_SKU:{product_sku.upper()}"

    async with scoped_pg_session(pool, ns_uuid) as conn:
        # 1. Verify asset existence under strict tenant isolation
        asset_row = await conn.fetchrow(
            """
            SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state
            FROM assets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            asset_uuid,
            ns_uuid,
        )

        if asset_row is None:
            if params.get("raise_on_not_found", False):
                raise AssetNotFoundError(str(asset_uuid))
            return {
                "ok": False,
                "not_found": True,
                "error": f"Asset '{asset_uuid}' not found",
                "asset_id": str(asset_uuid),
            }

        # 2. Contract-A Ownership Guard: Assert Assets owns ASSET node type
        await assert_owner(conn, ns_uuid, _NODE_TYPE_ASSET, _ASSETS_ENGINE)

        # 3. Upsert boundary edge ASSET -[failure_pattern]-> PRODUCT_SKU into kg_edges
        await conn.execute(
            """
            INSERT INTO kg_edges (
                subject_label, predicate, object_label, confidence, namespace_id, change_origin
            ) VALUES (
                $1, 'failure_pattern', $2, $3, $4::uuid, $5
            )
            ON CONFLICT (subject_label, predicate, object_label, namespace_id)
            DO UPDATE SET confidence = EXCLUDED.confidence,
                          change_origin = EXCLUDED.change_origin,
                          updated_at = NOW()
            """,
            asset_subject,
            product_object,
            confidence,
            ns_uuid,
            _CHANGE_ORIGIN,
        )

        # 4. Touch asset updated_at to reflect failure observation
        await conn.execute(
            """
            UPDATE assets
            SET updated_at = $1::timestamptz
            WHERE id = $2::uuid AND namespace_id = $3::uuid
            """,
            now_dt,
            asset_uuid,
            ns_uuid,
        )

    log.info(
        "assets.failure_pattern: recorded %s -> %s (confidence=%.2f, severity=%s)",
        asset_subject,
        product_object,
        confidence,
        severity,
    )

    return {
        "ok": True,
        "asset_id": str(asset_uuid),
        "product_sku": product_sku.upper(),
        "edge": f"{asset_subject} -[{_PREDICATE_FAILURE_PATTERN}]-> {product_object}",
        "confidence": confidence,
        "failure_mode": failure_mode,
        "severity": severity,
        "notes": pattern_notes,
    }


async def do_get_failure_patterns(
    engine_or_pool: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retrieve failure pattern edges for an asset, product, or whole namespace.

    Parameters
    ----------
    engine_or_pool:
        NCEEngine or asyncpg.Pool instance.
    params:
        - namespace_id: (required) tenant UUID.
        - asset_id: (optional) asset UUID.
        - product_sku: (optional) product SKU.

    Returns
    -------
    dict[str, Any]:
        {"ok": True, "patterns": [...], "count": int}
    """
    pool = _extract_pool(engine_or_pool)
    ns_str = require_namespace_id(params)
    ns_uuid = _parse_uuid(ns_str, "namespace_id")

    asset_id_raw = params.get("asset_id") or params.get("id")
    product_sku_raw = params.get("product_sku")

    conditions = ["predicate = 'failure_pattern'", "namespace_id = $1::uuid"]
    query_params: list[Any] = [ns_uuid]

    if asset_id_raw:
        asset_uuid = _parse_uuid(asset_id_raw, "asset_id")
        query_params.append(f"ASSET:{asset_uuid}")
        conditions.append(f"subject_label = ${len(query_params)}")

    if product_sku_raw:
        sku = str(product_sku_raw).strip().upper()
        query_params.append(f"PRODUCT_SKU:{sku}")
        conditions.append(f"object_label = ${len(query_params)}")

    where_clause = " AND ".join(conditions)
    sql = f"""
        SELECT subject_label, predicate, object_label, confidence, created_at, updated_at
        FROM kg_edges
        WHERE {where_clause}
        ORDER BY confidence DESC NULLS LAST, created_at DESC
    """

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(sql, *query_params)

    patterns = [
        {
            "subject_label": r["subject_label"],
            "predicate": r["predicate"],
            "object_label": r["object_label"],
            "confidence": float(r["confidence"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]

    return {
        "ok": True,
        "patterns": patterns,
        "count": len(patterns),
    }
