"""
nce/vertical_modules/hr/certs.py
================================
Certification lifecycle monitoring and expiry tracking for Module 13.

Functions:
  - do_cert_status: Computes valid, expiring, and expired certifications.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.events.bus import publish

log = logging.getLogger("nce.vertical_modules.hr.certs")

_NODE_TYPE_CERTIFICATION = "CERTIFICATION"
_OP_EXPIRED = "EXPIRED"


def _extract_pool(engine_or_pool: Any) -> Any:
    if hasattr(engine_or_pool, "pg_pool") and (
        "pg_pool" in getattr(engine_or_pool, "__dict__", {})
        or hasattr(type(engine_or_pool), "pg_pool")
    ):
        return engine_or_pool.pg_pool
    return engine_or_pool


def _parse_uuid(val: Any, name: str) -> UUID:
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val).strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid UUID for {name}: {val!r}") from exc


async def do_cert_status(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Check certification status and impending expiration across employees.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - employee_id: (optional) Filter by employee ID.
        - warn_days: (optional, default 90) Days threshold for expiring warning.
        - status: (optional) Filter by certification status ('active', 'expired', 'all').
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    employee_id = params.get("employee_id")
    warn_days = max(1, min(365, int(params.get("warn_days") or 90)))
    status_filter = str(params.get("status") or "all").strip().lower()

    today = date.today()
    warn_threshold = today + timedelta(days=warn_days)

    query = [
        """
        SELECT c.id, c.cert_id, c.employee_id, c.authority, c.name,
               c.issued, c.valid_to, c.status, e.name AS employee_name
        FROM   certifications c
        JOIN   employees e ON e.employee_id = c.employee_id AND e.namespace_id = c.namespace_id
        WHERE  c.namespace_id = $1::uuid
        """
    ]
    query_args: list[Any] = [ns_uuid]
    idx = 2

    if employee_id:
        query.append(f"AND c.employee_id = ${idx}")
        query_args.append(str(employee_id).strip())
        idx += 1

    query.append("ORDER BY c.valid_to ASC NULLS LAST")

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(" ".join(query), *query_args)

    cert_items: list[dict[str, Any]] = []
    expiring_count = 0
    expired_count = 0
    valid_count = 0

    for r in rows:
        v_to = r["valid_to"]
        if isinstance(v_to, datetime):
            v_date = v_to.date()
        elif isinstance(v_to, date):
            v_date = v_to
        else:
            v_date = None

        if v_date is None:
            computed_state = "perpetual"
            valid_count += 1
            days_remaining = None
        elif v_date < today:
            computed_state = "expired"
            expired_count += 1
            days_remaining = (v_date - today).days
        elif v_date <= warn_threshold:
            computed_state = "expiring_soon"
            expiring_count += 1
            days_remaining = (v_date - today).days
        else:
            computed_state = "valid"
            valid_count += 1
            days_remaining = (v_date - today).days

        if status_filter != "all":
            if status_filter == "expiring" and computed_state != "expiring_soon":
                continue
            if status_filter == "expired" and computed_state != "expired":
                continue
            if status_filter == "active" and computed_state in ("expired"):
                continue

        cert_items.append(
            {
                "id": str(r["id"]),
                "cert_id": r["cert_id"],
                "employee_id": r["employee_id"],
                "employee_name": r["employee_name"],
                "authority": r["authority"],
                "name": r["name"],
                "issued": r["issued"].isoformat() if r["issued"] else None,
                "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
                "status": computed_state,
                "days_remaining": days_remaining,
            }
        )

    return {
        "namespace_id": str(ns_uuid),
        "warn_days": warn_days,
        "certifications": cert_items,
        "total_count": len(cert_items),
        "expiring_soon_count": expiring_count,
        "expired_count": expired_count,
        "valid_count": valid_count,
    }


async def do_check_hr_cert_expiry(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Scan certifications in active namespace and idempotently publish CERTIFICATION.EXPIRED for expired ones.

    Parameters
    ----------
    params : dict[str, Any]
        - namespace_id: (required) Tenant UUID.
        - reference_date: (optional) Base date to check against (defaults to today).
    """
    pool = _extract_pool(engine)
    ns_uuid = _parse_uuid(params.get("namespace_id"), "namespace_id")

    ref_date_raw = params.get("reference_date")
    if ref_date_raw:
        if isinstance(ref_date_raw, (date, datetime)):
            ref_date = ref_date_raw.date() if isinstance(ref_date_raw, datetime) else ref_date_raw
        else:
            ref_date = date.fromisoformat(str(ref_date_raw).split("T")[0])
    else:
        ref_date = date.today()

    query = """
        SELECT c.id, c.cert_id, c.employee_id, c.name, c.valid_to, c.status
        FROM certifications c
        WHERE c.namespace_id = $1::uuid
          AND (
              (c.valid_to IS NOT NULL AND c.valid_to::date <= $2::date)
              OR LOWER(c.status) IN ('expired', 'revoked', 'suspended', 'inactive')
          )
    """

    checked = 0
    expired = 0
    published = 0

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(query, ns_uuid, ref_date)
        checked = len(rows)

        for r in rows:
            v_to = r["valid_to"]
            if isinstance(v_to, datetime):
                v_date = v_to.date()
            elif isinstance(v_to, date):
                v_date = v_to
            else:
                v_date = None

            status_str = str(r["status"] or "").strip().lower()
            is_expired = (v_date is not None and v_date <= ref_date) or status_str in (
                "expired",
                "revoked",
                "suspended",
                "inactive",
            )

            if not is_expired:
                continue

            expired += 1
            aggregate_id = str(r["cert_id"] or r["id"])

            already_published = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM outbox_events
                    WHERE namespace_id = $1::uuid
                      AND event_type = 'CERTIFICATION.EXPIRED'
                      AND aggregate_id = $2
                )
                """,
                ns_uuid,
                aggregate_id,
            )

            if not already_published:
                await publish(
                    conn,
                    namespace_id=ns_uuid,
                    node_type=_NODE_TYPE_CERTIFICATION,
                    op=_OP_EXPIRED,
                    aggregate_id=aggregate_id,
                    payload={
                        "namespace_id": str(ns_uuid),
                        "employee_id": str(r["employee_id"]),
                        "resource_id": str(r["employee_id"]),
                        "cert_name": str(r["name"]),
                        "status": "expired",
                        "valid_to": v_to.isoformat() if v_to else None,
                        "cert_id": aggregate_id,
                    },
                )
                published += 1

    return {
        "namespace_id": str(ns_uuid),
        "checked": checked,
        "expired": expired,
        "published": published,
    }
