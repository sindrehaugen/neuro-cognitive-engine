"""
nce/vertical_modules/sales/baseline.py
======================================
Implement the baseline freeze logic for Sales.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.sales.baseline")


async def do_freeze_baseline(
    engine_or_conn: Any,
    params_or_ns: Any,
    *,
    quote_id: str | None = None,
    signed_margin_pct: float | None = None,
    signed_total_nok: float | None = None,
    signed_at: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """Freeze a quote's margin/sum baseline (immutable, append-only).

    Supports two signatures:
      1. do_freeze_baseline(engine, params)
      2. do_freeze_baseline(conn, namespace_id, *, quote_id, signed_margin_pct, signed_total_nok, signed_at=None)
    """
    if isinstance(params_or_ns, dict):
        # Signature 1: do_freeze_baseline(engine, params)
        engine = engine_or_conn
        params = params_or_ns

        namespace_id = params.get("namespace_id")
        if not namespace_id:
            raise ValueError("namespace_id is required")
        ns_uuid = UUID(str(namespace_id))

        quote_id_val = params.get("quote_id")
        if not quote_id_val:
            raise ValueError("quote_id is required")
        if not isinstance(quote_id_val, str) or not quote_id_val.strip():
            raise ValueError("quote_id must be a non-empty string")

        margin_val = params.get("signed_margin_pct")
        if margin_val is None:
            raise ValueError("signed_margin_pct is required")
        try:
            signed_margin_pct_val = float(margin_val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"signed_margin_pct must be a float: {exc}")

        if not (0.0 <= signed_margin_pct_val <= 1.0):
            raise ValueError("signed_margin_pct must be between 0.0 and 1.0")

        total_val = params.get("signed_total_nok")
        if total_val is None:
            raise ValueError("signed_total_nok is required")
        try:
            signed_total_nok_val = float(total_val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"signed_total_nok must be a float: {exc}")

        signed_at_val = params.get("signed_at")
        signed_at_dt: datetime.datetime
        if signed_at_val:
            if isinstance(signed_at_val, datetime.datetime):
                signed_at_dt = signed_at_val
            else:
                try:
                    signed_at_dt = datetime.datetime.fromisoformat(str(signed_at_val))
                except ValueError as exc:
                    raise ValueError(f"signed_at must be an ISO-8601 string or datetime: {exc}")
        else:
            signed_at_dt = datetime.datetime.now(datetime.timezone.utc)

        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            return await _do_freeze_baseline_direct(
                conn,
                ns_uuid,
                quote_id=quote_id_val,
                signed_margin_pct=signed_margin_pct_val,
                signed_total_nok=signed_total_nok_val,
                signed_at=signed_at_dt,
            )
    else:
        # Signature 2: do_freeze_baseline(conn, namespace_id, *, quote_id, ...)
        conn = engine_or_conn
        ns_uuid = UUID(str(params_or_ns)) if not isinstance(params_or_ns, UUID) else params_or_ns

        if not quote_id:
            raise ValueError("quote_id is required")
        if signed_margin_pct is None:
            raise ValueError("signed_margin_pct is required")
        if signed_total_nok is None:
            raise ValueError("signed_total_nok is required")

        signed_at_dt = datetime.datetime.now(datetime.timezone.utc)
        if signed_at:
            if isinstance(signed_at, datetime.datetime):
                signed_at_dt = signed_at
            else:
                signed_at_dt = datetime.datetime.fromisoformat(str(signed_at))

        return await _do_freeze_baseline_direct(
            conn,
            ns_uuid,
            quote_id=quote_id,
            signed_margin_pct=signed_margin_pct,
            signed_total_nok=signed_total_nok,
            signed_at=signed_at_dt,
        )


async def _do_freeze_baseline_direct(
    conn: Any,
    ns_uuid: UUID,
    *,
    quote_id: str,
    signed_margin_pct: float,
    signed_total_nok: float,
    signed_at: datetime.datetime,
) -> dict[str, Any]:
    # Check if a baseline already exists for this quote (enforce idempotency/no-op)
    existing = await conn.fetchrow(
        """
        SELECT id, quote_id, signed_margin_pct, signed_total_nok, signed_at
        FROM sales_signed_baselines
        WHERE namespace_id = $1 AND quote_id = $2
        """,
        ns_uuid,
        quote_id,
    )
    if existing:
        log.info("Quote baseline for %s already frozen (id: %s)", quote_id, existing["id"])
        return {
            "ok": True,
            "status": "already_frozen",
            "already_frozen": True,
            "id": str(existing["id"]),
            "quote_id": existing["quote_id"],
            "signed_margin_pct": float(existing["signed_margin_pct"]),
            "signed_total_nok": float(existing["signed_total_nok"]),
            "signed_at": existing["signed_at"].isoformat(),
        }

    # Insert new baseline row
    row = await conn.fetchrow(
        """
        INSERT INTO sales_signed_baselines (
            namespace_id, quote_id, signed_margin_pct, signed_total_nok, signed_at
        )
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, quote_id, signed_margin_pct, signed_total_nok, signed_at
        """,
        ns_uuid,
        quote_id,
        signed_margin_pct,
        signed_total_nok,
        signed_at,
    )

    log.info("Successfully froze baseline for quote %s (id: %s)", quote_id, row["id"])
    return {
        "ok": True,
        "status": "frozen",
        "already_frozen": False,
        "id": str(row["id"]),
        "quote_id": row["quote_id"],
        "signed_margin_pct": float(row["signed_margin_pct"]),
        "signed_total_nok": float(row["signed_total_nok"]),
        "signed_at": row["signed_at"].isoformat(),
    }


async def get_signed_baseline(
    conn: Any,
    namespace_id: UUID,
    quote_id: str,
) -> dict[str, Any] | None:
    """Retrieve the signed baseline for a quote under a namespace.

    Returns the baseline dictionary shape expected by reader engines, or None if not found.
    """
    row = await conn.fetchrow(
        """
        SELECT id, quote_id, signed_margin_pct, signed_total_nok, signed_at
        FROM sales_signed_baselines
        WHERE namespace_id = $1::uuid AND quote_id = $2
        """,
        namespace_id,
        quote_id,
    )
    if row is None:
        return None

    return {
        "id": str(row["id"]),
        "quote_id": row["quote_id"],
        "signed_margin_pct": float(row["signed_margin_pct"]),
        "signed_total_nok": float(row["signed_total_nok"]),
        "signed_at": row["signed_at"].isoformat(),
    }
