"""
nce/vertical_modules/economy/gl.py
==================================
General Ledger query and record retrieval for the Economy vertical module.

Provides :func:`do_get_gl_records` to query ``economy_postings`` scoped strictly
to tenant namespaces, optionally filtered by date bounds, account, period, or source.
All returned records are projected through Contract C8 allow-list redaction
(``gl-records-redaction.json``) to ensure sensitive fields never leak across boundaries.

Seam contract with Agreements (coverage & kickback):
Each record dict returns at minimum::

    {
        "supplier_name": str,       # raw or resolved supplier name from GL
        "supplier_id":  str | None, # optional supplier identifier (e.g. orgnr)
        "amount_nok":   float,      # spend amount in NOK
        "gl_date":      str,        # ISO-8601 date (YYYY-MM-DD)
    }
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.redaction.redactor import project

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.economy.gl")

_DEFAULT_LIMIT: int = 5000
_MAX_LIMIT: int = 10000


def _parse_iso_boundary(val: Any, name: str) -> datetime | None:
    """Parse an ISO date or datetime string to UTC datetime.

    Raises ValueError if string is non-empty and unparseable.
    """
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time(), tzinfo=timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"do_get_gl_records: '{name}' must be a valid ISO datetime or date string (got {val!r})"
        ) from exc


async def do_get_gl_records(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Query GL postings from ``economy_postings`` for a namespace with C8 redaction.

    Parameters
    ----------
    engine:
        NCEEngine instance (provides ``pg_pool``).
    params:
        ``{
            "namespace_id":       str | UUID,        # required
            "since_iso":          str | None,        # optional lower bound (created_at >= since)
            "until_iso":          str | None,        # optional upper bound (created_at <= until)
            "account":            str | None,        # optional exact GL account code (e.g. '4300')
            "account_prefix":     str | None,        # optional account prefix (e.g. '4')
            "period_id":          str | None,        # optional accounting period (e.g. '2026-08')
            "economy_source_id":  str | None,        # optional source identifier
            "limit":              int | None,        # optional row limit (default 5000, max 10000)
        }``

    Returns
    -------
    dict
        ``{
            "ok": True,
            "records": list[dict],   # C8-redacted GL record dicts
            "count": int,
        }``
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = UUID(str(namespace_id))

    since_dt = _parse_iso_boundary(params.get("since_iso"), "since_iso")
    until_dt = _parse_iso_boundary(params.get("until_iso"), "until_iso")

    account = str(params["account"]).strip() if params.get("account") else None
    account_prefix = str(params["account_prefix"]).strip() if params.get("account_prefix") else None
    period_id = str(params["period_id"]).strip() if params.get("period_id") else None
    source_id = (
        str(params["economy_source_id"]).strip() if params.get("economy_source_id") else None
    )

    raw_limit = params.get("limit")
    if raw_limit is not None:
        try:
            limit = min(max(1, int(raw_limit)), _MAX_LIMIT)
        except (ValueError, TypeError):
            limit = _DEFAULT_LIMIT
    else:
        limit = _DEFAULT_LIMIT

    if getattr(engine, "pg_pool", None) is None:
        raise ValueError("do_get_gl_records: engine.pg_pool is not configured")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT
                ep.id,
                ep.namespace_id,
                ep.event_id,
                ep.event_type,
                ep.line_no,
                ep.account,
                ep.amount,
                ep.period_id,
                ep.economy_source_id,
                ep.change_origin,
                ep.created_at,
                kn.label AS vendor_label
            FROM economy_postings ep
            LEFT JOIN kg_nodes kn ON kn.namespace_id = ep.namespace_id
                                 AND kn.entity_type = 'VENDOR'
                                 AND (kn.label = ep.economy_source_id
                                      OR kn.economy_source_id = ep.economy_source_id
                                      OR kn.label = 'Vendor:' || ep.economy_source_id
                                      OR kn.label = 'VENDOR:' || ep.economy_source_id)
            WHERE ep.namespace_id = $1::uuid
              AND ($2::timestamptz IS NULL OR ep.created_at >= $2)
              AND ($3::timestamptz IS NULL OR ep.created_at <= $3)
              AND ($4::text IS NULL OR ep.account = $4)
              AND ($5::text IS NULL OR starts_with(ep.account, $5))
              AND ($6::text IS NULL OR ep.period_id = $6)
              AND ($7::text IS NULL OR ep.economy_source_id = $7)
            ORDER BY ep.created_at ASC, ep.id ASC
            LIMIT $8
            """,
            ns_uuid,
            since_dt,
            until_dt,
            account,
            account_prefix,
            period_id,
            source_id,
            limit,
        )

    records: list[dict[str, Any]] = []
    for row in rows:
        vendor_label = row["vendor_label"]
        ec_source_id = row["economy_source_id"]
        account_code = row["account"]

        # Derive supplier_id and supplier_name
        supplier_id: str | None = None
        supplier_name: str
        if vendor_label:
            supplier_id = vendor_label.split(":")[-1].strip()
            supplier_name = vendor_label
        elif ec_source_id:
            supplier_id = ec_source_id.split(":")[-1].strip()
            supplier_name = ec_source_id
        else:
            supplier_id = None
            supplier_name = f"GL Account {account_code}"

        # gl_date: YYYY-MM-DD string
        created_at_dt = row["created_at"]
        if created_at_dt is not None and isinstance(created_at_dt, (datetime, date)):
            gl_date = created_at_dt.strftime("%Y-%m-%d")
        elif row["period_id"]:
            gl_date = str(row["period_id"])
        else:
            gl_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        raw_record = {
            "id": str(row["id"]),
            "namespace_id": str(row["namespace_id"]),
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "line_no": int(row["line_no"]),
            "account": str(account_code),
            "amount_nok": float(row["amount"]),
            "gl_date": gl_date,
            "period_id": row["period_id"],
            "economy_source_id": ec_source_id,
            "change_origin": str(row["change_origin"]),
            "created_at": created_at_dt.isoformat() if created_at_dt else None,
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
        }

        # Contract C8 allow-list projection
        records.append(project(raw_record, "gl-records"))

    return {
        "ok": True,
        "records": records,
        "count": len(records),
    }
