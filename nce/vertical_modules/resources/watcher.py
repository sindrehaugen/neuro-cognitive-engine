"""
nce.vertical_modules.resources.watcher
======================================
RS-4: Reactive Event Watcher for Module 15 (Staff & Resources Engine).
Subscribes to HR Engine events via nce.events.bus rather than polling on a timer.
When a technician's certification expires, is revoked, or is updated, reactively
detects future allocation conflicts, marks allocations tentative/flagged, and emits alerts.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import _parse_datetime
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.watcher")

_CERT_EXPIRED_STATUSES = frozenset({"expired", "revoked", "suspended", "inactive"})


async def handle_hr_cert_change(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Reactive event handler for HR certification changes (RS-4).
    Invoked when a certification is updated, expired, or revoked in Module 13.
    Identifies future allocations for the technician that violate cert validity,
    flags the conflict in allocation attrs, and updates status if needed.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    pool = _extract_pool(engine)

    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    emp_id_raw = params.get("employee_id") or params.get("resource_id")
    if not emp_id_raw:
        raise ValueError("employee_id or resource_id is required.")

    cert_name = str(params.get("cert_name") or params.get("name") or "Unknown Cert")
    status = str(params.get("status") or "active").strip().lower()

    valid_to_raw = params.get("valid_to")
    valid_to_dt = None
    if valid_to_raw:
        try:
            valid_to_dt = _parse_datetime(valid_to_raw, "valid_to")
        except Exception:
            if isinstance(valid_to_raw, (date, datetime)):
                valid_to_dt = datetime(
                    valid_to_raw.year,
                    valid_to_raw.month,
                    valid_to_raw.day,
                    tzinfo=timezone.utc,
                )

    now_utc = datetime.now(timezone.utc)

    async with scoped_pg_session(pool, ns_id) as conn:
        # 1. Resolve technician resource
        res_row = await conn.fetchrow(
            """
            SELECT id, name, metadata
            FROM resources
            WHERE namespace_id = $1
              AND (
                  id::text = $2
                  OR metadata->>'employee_id' = $2
                  OR email = $2
              )
            LIMIT 1
            """,
            ns_id,
            str(emp_id_raw),
        )

        if not res_row:
            log.info("No resource found matching employee_id %s in namespace %s", emp_id_raw, ns_id)
            return {
                "namespace_id": str(ns_id),
                "employee_id": str(emp_id_raw),
                "resource_id": None,
                "affected_allocations": [],
                "affected_count": 0,
                "reactive_event_processed": True,
            }

        res_id = res_row["id"]

        # 2. Check if cert is invalid or expiring
        is_invalid = status in _CERT_EXPIRED_STATUSES or (
            valid_to_dt is not None and valid_to_dt <= now_utc
        )

        # 3. Find future allocations that overlap or succeed expiration
        query = """
            SELECT id, starts_at, ends_at, attrs, status
            FROM allocations
            WHERE namespace_id = $1
              AND resource_id = $2
              AND status <> 'released'
        """
        args: list[Any] = [ns_id, res_id]

        if is_invalid:
            # All future allocations are flagged
            query += f" AND ends_at >= ${len(args) + 1}"
            args.append(now_utc)
        elif valid_to_dt:
            # Future allocations starting after or spanning past valid_to are flagged
            query += f" AND ends_at >= ${len(args) + 1}"
            args.append(valid_to_dt)
        else:
            # Cert is active and valid with no expiry - clear warnings if any
            query += f" AND ends_at >= ${len(args) + 1}"
            args.append(now_utc)

        alloc_rows = await conn.fetch(query, *args)

        affected_allocations = []
        for ar in alloc_rows:
            aid = ar["id"]
            attrs = json.loads(ar["attrs"]) if isinstance(ar["attrs"], str) else (ar["attrs"] or {})

            if is_invalid or (valid_to_dt and ar["ends_at"] > valid_to_dt):
                attrs["cert_conflict"] = {
                    "cert_name": cert_name,
                    "valid_to": valid_to_dt.isoformat() if valid_to_dt else None,
                    "status": status,
                    "reason": "RS-4: Certification expired or invalid for scheduled allocation window",
                    "flagged_at": now_utc.isoformat(),
                }
                new_status = "tentative" if ar["status"] != "released" else ar["status"]
                await conn.execute(
                    """
                    UPDATE allocations
                    SET attrs = $1::jsonb, status = $2, updated_at = now()
                    WHERE id = $3 AND namespace_id = $4
                    """,
                    json.dumps(attrs),
                    new_status,
                    aid,
                    ns_id,
                )
                affected_allocations.append(
                    {
                        "allocation_id": str(aid),
                        "starts_at": ar["starts_at"].isoformat()
                        if hasattr(ar["starts_at"], "isoformat")
                        else str(ar["starts_at"]),
                        "ends_at": ar["ends_at"].isoformat()
                        if hasattr(ar["ends_at"], "isoformat")
                        else str(ar["ends_at"]),
                        "status": new_status,
                        "conflict_reason": attrs["cert_conflict"]["reason"],
                    }
                )

        log.info(
            "RS-4 reactive cert watcher processed cert %s for resource %s: %d allocations flagged.",
            cert_name,
            res_id,
            len(affected_allocations),
        )

        return {
            "namespace_id": str(ns_id),
            "employee_id": str(emp_id_raw),
            "resource_id": str(res_id),
            "resource_name": res_row["name"],
            "cert_name": cert_name,
            "cert_status": status,
            "valid_to": valid_to_dt.isoformat() if valid_to_dt else None,
            "affected_allocations": affected_allocations,
            "affected_count": len(affected_allocations),
            "reactive_event_processed": True,
        }


async def on_hr_cert_event(conn: Any, event: dict[str, Any]) -> None:
    """Outbox relay handler for HR certification events."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    ns_id = event.get("namespace_id") or payload.get("namespace_id")
    if ns_id:
        payload["namespace_id"] = ns_id
        await handle_hr_cert_change(conn, payload)


def register_resources_event_subscribers() -> None:
    """
    Register RS-4 reactive subscribers on nce.events.bus.
    Subscribes to CERTIFICATION and HR compliance milestone events.
    """
    try:
        from nce.events.bus import subscribe

        subscribe({"node_type": "CERTIFICATION", "op": "UPDATED"}, on_hr_cert_event)
        subscribe({"node_type": "CERTIFICATION", "op": "EXPIRED"}, on_hr_cert_event)
        subscribe({"node_type": "CERTIFICATION", "op": "CREATED"}, on_hr_cert_event)
        log.info("RS-4 reactive subscribers registered for HR CERTIFICATION events.")
    except Exception as exc:
        log.warning("Could not register resources event subscribers on bus: %s", exc)
