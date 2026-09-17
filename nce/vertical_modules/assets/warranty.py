"""
nce/vertical_modules/assets/warranty.py
========================================
Assets warranty and EOL/EOS watcher core (Wave A-3).

Role: **Watcher** — scans assets for nearing warranty expiration,
firmware/manufacturer EOL status, and lifespan exceeded.

Invariants:
  - Read-only across assets and product catalog (zero mutation on assets table).
  - Explicit namespace scoping on every DB query.
  - Zero DDL migrations (Q-4 compliance).
  - Pure domain calculations for age, lifespan, and warranty windows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id

log = logging.getLogger("nce.vertical_modules.assets.warranty")

# Default expected hardware lifespan (5 years / 1825 days)
_DEFAULT_LIFESPAN_DAYS: int = 1825

# Default warranty duration (24 months ~ 730 days) when not overridden
_DEFAULT_WARRANTY_MONTHS: int = 24


def _parse_now(raw: Any) -> datetime:
    """Parse reference datetime or default to current UTC time."""
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def do_check_warranty_eol(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Check assets for expiring warranty, firmware/product EOL, or exceeded lifespan.

    Parameters
    ----------
    engine:
        NCEEngine instance with ``pg_pool``.
    params:
        - ``namespace_id`` (UUID | str, required): active tenant namespace.
        - ``warranty_window_days`` (int, optional): days lookahead for warranty expiry.
          Defaults to ``cfg.NCE_ASSETS_WARRANTY_WARN_DAYS`` (30).
        - ``eol_window_days`` (int, optional): days lookahead for lifespan/EOL warning.
          Defaults to ``cfg.NCE_ASSETS_EOL_WARN_DAYS`` (90).
        - ``default_lifespan_days`` (int, optional): expected asset lifespan in days (default 1825).
        - ``default_warranty_months`` (int, optional): fallback warranty duration in months (default 24).
        - ``asset_id`` (str, optional): filter check to a single asset UUID.
        - ``functional_location_id`` (str, optional): filter check to a functional location.
        - ``now`` (datetime | str, optional): reference timestamp for deterministic testing.

    Returns
    -------
    dict
        Structured report with ``warranty_alerts``, ``eol_alerts``, and summary counts.
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = UUID(str(namespace_id))

    warranty_window = int(
        params.get("warranty_window_days") or getattr(cfg, "NCE_ASSETS_WARRANTY_WARN_DAYS", 30)
    )
    eol_window = int(params.get("eol_window_days") or getattr(cfg, "NCE_ASSETS_EOL_WARN_DAYS", 90))
    lifespan_days = int(params.get("default_lifespan_days") or _DEFAULT_LIFESPAN_DAYS)
    warranty_months = int(params.get("default_warranty_months") or _DEFAULT_WARRANTY_MONTHS)

    now_dt = _parse_now(params.get("now"))

    filter_asset_id = (params.get("asset_id") or "").strip()
    filter_loc_id = (params.get("functional_location_id") or "").strip()

    # Load EOL entries from Product vertical module if available
    eol_entries: list[dict[str, Any]] = []
    try:
        from nce.vertical_modules.product.watchers import _resolve_eol_entries

        eol_entries = _resolve_eol_entries()
    except Exception as exc:
        log.debug("Could not load product EOL entries: %s", exc)

    # Build EOL match map keyed by lower-cased part numbers
    eol_by_part: dict[str, dict[str, Any]] = {}
    for entry in eol_entries:
        part = str(entry.get("mfr_part_no") or "").strip().lower()
        if part:
            eol_by_part[part] = entry

    warranty_alerts: list[dict[str, Any]] = []
    eol_alerts: list[dict[str, Any]] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        query = """
            SELECT id, namespace_id, bom_line_id, serial, functional_location_id,
                   lifecycle_state, created_at, updated_at
            FROM assets
            WHERE namespace_id = $1::uuid
        """
        query_args: list[Any] = [str(ns_uuid)]

        if filter_asset_id:
            query += " AND id = $2::uuid"
            query_args.append(filter_asset_id)
        elif filter_loc_id:
            query += " AND functional_location_id = $2"
            query_args.append(filter_loc_id)

        query += " ORDER BY created_at ASC"
        rows = await conn.fetch(query, *query_args)

    for row in rows:
        asset_id_str = str(row["id"])
        serial = row["serial"]
        bom_line_id = row["bom_line_id"]
        func_loc = row["functional_location_id"]
        state = str(row["lifecycle_state"] or "")
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age_seconds = (now_dt - created_at).total_seconds()
        age_days = max(0.0, age_seconds / 86400.0)
        lifespan_remaining = lifespan_days - age_days

        # Check EOL status
        is_eol = False
        reasons: list[str] = []
        successor_part: str | None = None
        successor_mfr: str | None = None

        if state.upper() in ("EOL", "RETIRING", "RETIRED"):
            is_eol = True
            reasons.append(f"Lifecycle state is {state}")

        # Check against product catalog EOL entries (match by bom_line_id or serial)
        matched_eol_entry: dict[str, Any] | None = None
        if bom_line_id:
            cleaned_bom = bom_line_id.strip().lower()
            if cleaned_bom in eol_by_part:
                matched_eol_entry = eol_by_part[cleaned_bom]
            else:
                # Check quote:part format if present
                for part_key, entry_val in eol_by_part.items():
                    if part_key in cleaned_bom:
                        matched_eol_entry = entry_val
                        break

        if matched_eol_entry:
            is_eol = True
            successor_part = matched_eol_entry.get("successor_mfr_part_no")
            successor_mfr = matched_eol_entry.get("successor_manufacturer")
            reasons.append(f"Manufacturer EOL: {matched_eol_entry.get('mfr_part_no', bom_line_id)}")

        lifespan_exceeded = age_days >= lifespan_days
        lifespan_warning = (not lifespan_exceeded) and (lifespan_remaining <= eol_window)

        if lifespan_exceeded:
            is_eol = True
            reasons.append(
                f"Lifespan exceeded ({round(age_days, 1)} days >= {lifespan_days} days expected)"
            )
        elif lifespan_warning:
            reasons.append(
                f"Lifespan expiring in {round(lifespan_remaining, 1)} days (window: {eol_window} days)"
            )

        if is_eol or lifespan_warning:
            eol_alerts.append(
                {
                    "asset_id": asset_id_str,
                    "serial": serial,
                    "bom_line_id": bom_line_id,
                    "functional_location_id": func_loc,
                    "lifecycle_state": state,
                    "age_days": round(age_days, 1),
                    "lifespan_remaining_days": round(lifespan_remaining, 1),
                    "lifespan_exceeded": lifespan_exceeded,
                    "lifespan_warning": lifespan_warning,
                    "is_eol": is_eol,
                    "reason": "; ".join(reasons) if reasons else "EOL alert",
                    "successor_mfr_part_no": successor_part,
                    "successor_manufacturer": successor_mfr,
                }
            )

        # Check Warranty status
        # Calculate warranty end timestamp based on warranty_months
        warranty_duration_days = int(warranty_months * 30.4375)
        warranty_end_dt = created_at + timedelta(days=warranty_duration_days)
        warranty_remaining_seconds = (warranty_end_dt - now_dt).total_seconds()
        warranty_remaining_days = warranty_remaining_seconds / 86400.0

        if warranty_remaining_days <= warranty_window:
            warranty_alerts.append(
                {
                    "asset_id": asset_id_str,
                    "serial": serial,
                    "bom_line_id": bom_line_id,
                    "functional_location_id": func_loc,
                    "lifecycle_state": state,
                    "warranty_until": warranty_end_dt.isoformat(),
                    "warranty_remaining_days": round(warranty_remaining_days, 1),
                    "is_expired": warranty_remaining_days < 0,
                    "warning_window_days": warranty_window,
                }
            )

    return {
        "ok": True,
        "namespace_id": str(ns_uuid),
        "total_scanned": len(rows),
        "warranty_alerts_count": len(warranty_alerts),
        "eol_alerts_count": len(eol_alerts),
        "warranty_alerts": warranty_alerts,
        "eol_alerts": eol_alerts,
        "summary": {
            "total_assets": len(rows),
            "warranty_expiring_or_expired": len(warranty_alerts),
            "eol_or_lifespan_exceeded": len(eol_alerts),
        },
    }
