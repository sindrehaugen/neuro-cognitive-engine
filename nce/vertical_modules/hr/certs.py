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

REGULATORY_CERT_RULES: dict[str, dict[str, Any]] = {
    "FSE_LAVSPENNING": {
        "standard": "FSE § 7 / EN 50110-1",
        "max_validity_days": 365,
        "requires_first_aid": True,
        "description": "FSE Lavspenning (Forskrift om sikkerhet ved arbeid i og drift av elektriske anlegg)",
    },
    "FSE_HOYSPENNING": {
        "standard": "FSE § 7 / EN 50110-1",
        "max_validity_days": 365,
        "requires_first_aid": True,
        "description": "FSE Høyspenning",
    },
    "EN_50110_BA5": {
        "standard": "FSE § 7 / EN 50110-1",
        "max_validity_days": 365,
        "requires_first_aid": True,
        "description": "EN 50110-1 BA5 Competent person (electrical)",
    },
    "FAGBREV_GR_L": {
        "standard": "FEK § 6 (Elektriker Gr. L)",
        "max_validity_days": None,
        "requires_first_aid": False,
        "description": "Fagbrev Elektriker Gruppe L",
    },
    "FAGBREV_GR_H": {
        "standard": "FEK § 6 (Energimontør Gr. H)",
        "max_validity_days": None,
        "requires_first_aid": False,
        "description": "Fagbrev Energimontør Gruppe H",
    },
    "FEK_INSTALLATOR": {
        "standard": "FEK § 7 (Faglig ansvarlig / Installatør)",
        "max_validity_days": None,
        "requires_first_aid": False,
        "description": "Faglig ansvarlig / Registrert installatør",
    },
    "NKOM_ENA": {
        "standard": "Ekomforskriften (NKOM Ekomautorisasjon)",
        "max_validity_days": None,
        "requires_first_aid": False,
        "description": "NKOM Ekomnettautorisasjon (ENA)",
    },
    "ATEX_IECEX_COMPEX": {
        "standard": "ATEX 1999/92/EC / NEK 420 / IEC 60079",
        "max_validity_days": 1825,
        "requires_first_aid": False,
        "description": "ATEX / IECEx / CompEx Explosive Atmosphere Competency",
    },
    "DNV_MARINE_ELECTRICAL": {
        "standard": "DNV-RU-SHIP Pt.4 Ch.8 / NEK 410",
        "max_validity_days": None,
        "requires_first_aid": False,
        "description": "DNV Marine Electrical Installation & Commissioning",
    },
}

_SCOPE_REQUIREMENTS: dict[str, list[list[str]]] = {
    "electrical_lv": [
        ["FAGBREV_GR_L", "FEK_INSTALLATOR"],
        ["FSE_LAVSPENNING", "EN_50110_BA5"],
    ],
    "electrical_hv": [
        ["FAGBREV_GR_H", "FEK_INSTALLATOR"],
        ["FSE_HOYSPENNING", "EN_50110_BA5"],
    ],
    "ekom": [
        ["NKOM_ENA"],
    ],
    "atex_ex": [
        ["ATEX_IECEX_COMPEX"],
    ],
    "dnv_marine": [
        ["DNV_MARINE_ELECTRICAL"],
    ],
}

_CERT_ALIASES: dict[str, str] = {
    "FSE_LAV": "FSE_LAVSPENNING",
    "FSE_LV": "FSE_LAVSPENNING",
    "FSE": "FSE_LAVSPENNING",
    "FSE_HOY": "FSE_HOYSPENNING",
    "FSE_HV": "FSE_HOYSPENNING",
    "GR_L": "FAGBREV_GR_L",
    "GRUPPE_L": "FAGBREV_GR_L",
    "ELEKTRIKER": "FAGBREV_GR_L",
    "GR_H": "FAGBREV_GR_H",
    "GRUPPE_H": "FAGBREV_GR_H",
    "ENERGIMONTOR": "FAGBREV_GR_H",
    "INSTALLATOR": "FEK_INSTALLATOR",
    "FAGLIG_ANSVARLIG": "FEK_INSTALLATOR",
    "ENA": "NKOM_ENA",
    "EKOM": "NKOM_ENA",
    "EKOM_ENA": "NKOM_ENA",
    "ATEX": "ATEX_IECEX_COMPEX",
    "IECEX": "ATEX_IECEX_COMPEX",
    "COMPEX": "ATEX_IECEX_COMPEX",
    "NEK_420": "ATEX_IECEX_COMPEX",
    "DNV_MARINE": "DNV_MARINE_ELECTRICAL",
    "NEK_410": "DNV_MARINE_ELECTRICAL",
}


def _normalize_cert_code(raw_name: str) -> str:
    cleaned = raw_name.strip().upper().replace("-", "_").replace(" ", "_")
    return _CERT_ALIASES.get(cleaned, cleaned)


def _parse_date_safe(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        s = str(val).strip().split("T")[0]
        return date.fromisoformat(s)
    except Exception:
        return None


def validate_technician_regulatory_compliance(
    certs: list[dict[str, Any]] | list[Any],
    regulatory_scope: str,
    ref_date: date | None = None,
) -> dict[str, Any]:
    """Check whether a technician holds valid, non-expired certificates for a given regulatory scope.

    Parameters
    ----------
    certs : list[dict[str, Any]]
        List of certificate dictionaries or name strings.
    regulatory_scope : str
        Scope identifier ('electrical_lv', 'electrical_hv', 'ekom', 'atex_ex', 'dnv_marine').
    ref_date : date | None
        Reference date for validity evaluation (defaults to date.today()).

    Enforces:
    - FSE courses completed > 365 days before ref_date are expired under FSE § 7 / EN 50110-1
      even if a longer nominal expiry was entered.
    - Trade qualifications (FEK § 6 / § 7) required for electrical work.
    - ATEX/IECEx courses valid for maximum 1825 days (5 years).
    """
    if ref_date is None:
        ref_date = date.today()
    elif isinstance(ref_date, datetime):
        ref_date = ref_date.date()
    elif isinstance(ref_date, str):
        ref_date = _parse_date_safe(ref_date) or date.today()

    norm_scope = str(regulatory_scope or "").strip().lower()
    req_groups = _SCOPE_REQUIREMENTS.get(norm_scope)

    if req_groups is None:
        canon = _normalize_cert_code(regulatory_scope)
        if canon in REGULATORY_CERT_RULES:
            req_groups = [[canon]]
        else:
            return {
                "compliant": False,
                "regulatory_scope": regulatory_scope,
                "reasons": [f"Unknown or unsupported regulatory scope: {regulatory_scope!r}"],
                "missing_certs": [regulatory_scope],
                "expired_certs": [],
                "verified_certs": [],
            }

    parsed_certs: list[dict[str, Any]] = []
    for c in certs or []:
        if isinstance(c, str):
            c_dict = {"name": c}
        elif isinstance(c, dict):
            c_dict = dict(c)
        else:
            continue
        c_name = str(
            c_dict.get("name")
            or c_dict.get("cert_name")
            or c_dict.get("code")
            or c_dict.get("cert_id")
            or ""
        )
        c_code = _normalize_cert_code(c_name)
        c_dict["_code"] = c_code
        c_dict["_issued"] = _parse_date_safe(
            c_dict.get("issued")
            or c_dict.get("issued_at")
            or c_dict.get("issue_date")
            or c_dict.get("completed_at")
        )
        c_dict["_valid_to"] = _parse_date_safe(
            c_dict.get("valid_to") or c_dict.get("expiry_date") or c_dict.get("expires_at")
        )
        parsed_certs.append(c_dict)

    reasons: list[str] = []
    missing_certs: list[str] = []
    expired_certs: list[str] = []
    verified_certs: list[dict[str, Any]] = []

    for group in req_groups:
        matched_valid: dict[str, Any] | None = None
        group_expired_reasons: list[str] = []

        for candidate_code in group:
            rule = REGULATORY_CERT_RULES.get(candidate_code, {})
            max_days = rule.get("max_validity_days")
            standard = rule.get("standard", "")

            matching_cand_certs = [c for c in parsed_certs if c["_code"] == candidate_code]
            for cand_c in matching_cand_certs:
                st = str(cand_c.get("status") or "active").strip().lower()
                if st in ("expired", "revoked", "suspended", "inactive"):
                    exp_msg = f"{candidate_code} has status '{st}'"
                    group_expired_reasons.append(exp_msg)
                    expired_certs.append(candidate_code)
                    continue

                # Enforce max_validity_days from completion date
                issued = cand_c["_issued"]
                if max_days is not None and issued is not None:
                    days_since = (ref_date - issued).days
                    if days_since > max_days:
                        exp_msg = (
                            f"{candidate_code} completed {days_since} days ago exceeds "
                            f"maximum validity of {max_days} days under {standard}"
                        )
                        group_expired_reasons.append(exp_msg)
                        expired_certs.append(candidate_code)
                        continue

                valid_to = cand_c["_valid_to"]
                if valid_to is not None and valid_to < ref_date:
                    exp_msg = f"{candidate_code} expired on {valid_to.isoformat()}"
                    group_expired_reasons.append(exp_msg)
                    expired_certs.append(candidate_code)
                    continue

                matched_valid = cand_c
                verified_certs.append(
                    {
                        "code": candidate_code,
                        "standard": standard,
                        "cert": cand_c,
                    }
                )
                break

            if matched_valid is not None:
                break

        if matched_valid is None:
            if group_expired_reasons:
                reasons.extend(group_expired_reasons)
            else:
                desc = " or ".join(group)
                std_info = REGULATORY_CERT_RULES.get(group[0], {}).get("standard", "")
                suffix = f" ({std_info})" if std_info else ""
                reasons.append(f"Missing required certificate: {desc}{suffix}")
                missing_certs.extend(group)

    return {
        "compliant": len(reasons) == 0,
        "regulatory_scope": regulatory_scope,
        "reasons": reasons,
        "missing_certs": missing_certs,
        "expired_certs": list(dict.fromkeys(expired_certs)),
        "verified_certs": verified_certs,
    }


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
