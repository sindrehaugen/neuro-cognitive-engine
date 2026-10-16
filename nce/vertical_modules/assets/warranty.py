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
import re
from datetime import date, datetime, timedelta, timezone
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


def _parse_successor_sku(
    raw_sku: Any, default_mfr: str | None = None
) -> tuple[str | None, str | None]:
    """Parse a successor SKU string into ``(manufacturer, mfr_part_no)``."""
    s = str(raw_sku or "").strip()
    if not s:
        return default_mfr, None
    parts = [p.strip() for p in s.split(":") if p.strip()]
    if len(parts) >= 3 and parts[0].upper() in ("PRODUCT", "PRODUCT_SKU"):
        return parts[1], ":".join(parts[2:])
    if len(parts) == 2:
        return parts[0], parts[1]
    return default_mfr, s


def evaluate_regulatory_compliance(
    params: dict[str, Any], check_date: date | datetime | None = None
) -> dict[str, Any]:
    """Evaluate DNV Type Approval Certificate (TAC), ATEX, and ISO 12944 compliance (Wave E-2).

    Parameters
    ----------
    params:
        - ``dnv_tac_number`` (str, optional): DNV Type Approval Certificate identifier.
        - ``dnv_tac_expiry`` (date | str, optional): TAC expiry date.
        - ``atex_marking`` (str, optional): ATEX gas/dust marking string.
        - ``atex_cert_number`` (str, optional): ATEX Notified Body certificate reference.
        - ``atex_cert_expiry`` (date | str, optional): ATEX certificate expiry date.
        - ``iso12944_class`` (str, optional): ISO 12944 corrosivity classification (C1-CX).
        - ``ip_rating`` (str, optional): Ingress protection rating (e.g. IP66).
    check_date:
        Reference date for checking certificate expiration. Defaults to current UTC date.
    """
    if isinstance(check_date, datetime):
        ref_date = check_date.date()
    elif isinstance(check_date, date):
        ref_date = check_date
    elif isinstance(check_date, str) and check_date.strip():
        try:
            ref_date = date.fromisoformat(check_date.strip()[:10])
        except ValueError:
            ref_date = datetime.now(timezone.utc).date()
    else:
        ref_date = datetime.now(timezone.utc).date()

    violations: list[str] = []
    warnings: list[str] = []

    # 1. DNV Type Approval Certificate (TAC)
    dnv_tac_number = str(params.get("dnv_tac_number") or "").strip()
    raw_tac_expiry = params.get("dnv_tac_expiry")

    tac_expiry_date: date | None = None
    if isinstance(raw_tac_expiry, datetime):
        tac_expiry_date = raw_tac_expiry.date()
    elif isinstance(raw_tac_expiry, date):
        tac_expiry_date = raw_tac_expiry
    elif isinstance(raw_tac_expiry, str) and raw_tac_expiry.strip():
        try:
            tac_expiry_date = date.fromisoformat(raw_tac_expiry.strip()[:10])
        except ValueError:
            violations.append(f"Invalid dnv_tac_expiry date format: '{raw_tac_expiry}'")

    tac_is_expired = False
    tac_days_remaining: int | None = None
    tac_valid = False

    if dnv_tac_number or tac_expiry_date:
        if not dnv_tac_number:
            violations.append("DNV TAC expiry specified without valid dnv_tac_number.")
        elif tac_expiry_date is None:
            violations.append(f"DNV TAC '{dnv_tac_number}' has no valid expiration date specified.")
        else:
            tac_days_remaining = (tac_expiry_date - ref_date).days
            if tac_days_remaining < 0:
                tac_is_expired = True
                violations.append(
                    f"DNV Type Approval Certificate '{dnv_tac_number}' expired on "
                    f"{tac_expiry_date.isoformat()} ({abs(tac_days_remaining)} days ago)."
                )
            else:
                tac_valid = True
                if tac_days_remaining <= 90:
                    warnings.append(
                        f"DNV Type Approval Certificate '{dnv_tac_number}' expires in "
                        f"{tac_days_remaining} days ({tac_expiry_date.isoformat()})."
                    )

    # 2. ATEX Compliance
    atex_marking = str(params.get("atex_marking") or "").strip()
    atex_cert_num = str(params.get("atex_cert_number") or "").strip()
    raw_atex_expiry = params.get("atex_cert_expiry")

    atex_expiry_date: date | None = None
    if isinstance(raw_atex_expiry, datetime):
        atex_expiry_date = raw_atex_expiry.date()
    elif isinstance(raw_atex_expiry, date):
        atex_expiry_date = raw_atex_expiry
    elif isinstance(raw_atex_expiry, str) and raw_atex_expiry.strip():
        try:
            atex_expiry_date = date.fromisoformat(raw_atex_expiry.strip()[:10])
        except ValueError:
            violations.append(f"Invalid atex_cert_expiry date format: '{raw_atex_expiry}'")

    atex_valid = False
    atex_is_expired = False
    if atex_marking or atex_cert_num or atex_expiry_date:
        if atex_marking:
            has_ex_concept = bool(
                re.search(r"\b(Ex|I|II|1G|2G|3G|1D|2D|3D)\b", atex_marking, re.IGNORECASE)
            )
            if not has_ex_concept:
                violations.append(f"Invalid or unrecognized ATEX marking string: '{atex_marking}'.")
            else:
                atex_valid = True

        if atex_expiry_date:
            days_rem = (atex_expiry_date - ref_date).days
            if days_rem < 0:
                atex_is_expired = True
                atex_valid = False
                violations.append(
                    f"ATEX Certificate '{atex_cert_num or 'unspecified'}' expired on {atex_expiry_date.isoformat()}."
                )

    # 3. ISO 12944 / Environmental Protection
    iso_class = str(params.get("iso12944_class") or "").strip().upper()
    ip_rating = str(params.get("ip_rating") or "").strip().upper()

    valid_iso_classes = {"C1", "C2", "C3", "C4", "C5", "C5-I", "C5-M", "C5M", "CX"}
    env_valid = True
    if iso_class:
        if iso_class not in valid_iso_classes:
            env_valid = False
            violations.append(
                f"Invalid ISO 12944 corrosion category '{iso_class}'. Permitted: C1, C2, C3, C4, C5, CX."
            )

    if ip_rating:
        if not re.match(r"^IP[0-6X][0-9X]$", ip_rating):
            env_valid = False
            violations.append(
                f"Invalid EN 60529 IP rating format: '{ip_rating}'. Expected format e.g. IP66."
            )

    return {
        "compliant": len(violations) == 0,
        "check_date": ref_date.isoformat(),
        "dnv_tac": {
            "certificate_number": dnv_tac_number or None,
            "expiry_date": tac_expiry_date.isoformat() if tac_expiry_date else None,
            "is_expired": tac_is_expired,
            "days_remaining": tac_days_remaining,
            "valid": tac_valid,
        },
        "atex": {
            "marking": atex_marking or None,
            "cert_number": atex_cert_num or None,
            "cert_expiry": atex_expiry_date.isoformat() if atex_expiry_date else None,
            "is_expired": atex_is_expired,
            "valid": atex_valid,
        },
        "environmental": {
            "iso12944_class": iso_class or None,
            "ip_rating": ip_rating or None,
            "valid": env_valid,
        },
        "violations": violations,
        "warnings": warnings,
    }


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

    eol_entries: list[dict[str, Any]] = []
    warranty_alerts: list[dict[str, Any]] = []
    eol_alerts: list[dict[str, Any]] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Load EOL entries from Product vertical module if available
        try:
            import inspect

            from nce.vertical_modules.product.watchers import _resolve_eol_entries

            try:
                res = _resolve_eol_entries(conn, params, 0.0)
            except TypeError:
                res = _resolve_eol_entries()
            if inspect.isawaitable(res):
                res = await res
            eol_entries = list(res or [])
        except Exception as exc:
            log.debug("Could not load product EOL entries: %s", exc)

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

    # Build EOL match map keyed by lower-cased part numbers
    eol_by_part: dict[str, dict[str, Any]] = {}
    for entry in eol_entries:
        part = str(entry.get("mfr_part_no") or "").strip().lower()
        if part:
            eol_by_part[part] = entry

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
            if not successor_part and matched_eol_entry.get("successor_sku"):
                successor_mfr, successor_part = _parse_successor_sku(
                    matched_eol_entry.get("successor_sku"),
                    successor_mfr or matched_eol_entry.get("manufacturer"),
                )
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

    res_dict: dict[str, Any] = {
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

    # Wave E-2: Include regulatory_compliance ONLY when regulatory compliance params are supplied
    regulatory_keys = ("dnv_tac_number", "dnv_tac_expiry", "atex_marking", "iso12944_class")
    if any(k in params for k in regulatory_keys):
        res_dict["regulatory_compliance"] = evaluate_regulatory_compliance(params, now_dt.date())

    return res_dict
