"""
nce/vertical_modules/marketing/testimonials.py
==============================================
Testimonial capture and consent lifecycle for Module 14 (Marketing Engine).

Enforces:
- MK-5: Positive-only trigger. Testimonial requests fire ONLY on high NPS (>= 9.0).
  Outreach on low health or detractor scores is strictly refused.
- MK-4: Two-tier structured consent:
  * 'web_retractable': Standard public web use; can be retracted later.
  * 'ai_citable_irrevocable': High-bar, durable consent for JSON-LD/AEO schemas
    and AI agent ingestion where downstream recall is technically impossible.
- Right to retract: Consent revocation flips status to 'retracted' and hard-retires
  dependent rows via marketing_source_id.
- Explicit tenant isolation on all queries: WHERE namespace_id = $1.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from nce.vertical_modules.marketing._guard import (
    MarketingConsentMissingError,
    assert_positive_nps_only,
)

log = logging.getLogger("nce.vertical_modules.marketing.testimonials")

VALID_CONSENT_TIERS = frozenset({"web_retractable", "ai_citable_irrevocable"})


async def do_request_testimonial(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Issue a testimonial request for a customer, gated on high NPS (MK-5).

    Parameters
    ----------
    engine : Any
        NCEEngine or context providing pg_pool.
    params : dict[str, Any]
        - namespace_id (str | UUID): active tenant
        - customer_id (str): target customer identifier
        - project_id (str): delivered project identifier
        - nps_score (float): customer NPS rating (must be >= 9.0)

    Returns
    -------
    dict[str, Any]
        Structured request record in status='requested'.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("namespace_id is required")
    ns_str = str(raw_ns)

    customer_id = str(params.get("customer_id") or "").strip()
    if not customer_id:
        raise ValueError("customer_id is required")

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    nps_val = params.get("nps_score")
    if nps_val is None:
        raise ValueError("nps_score is required for testimonial request trigger")
    nps_score = float(nps_val)

    # MK-5: Positive-only trigger verification
    assert_positive_nps_only(nps_score, threshold=9.0)

    testimonial_id = str(params.get("testimonial_id") or uuid4())
    marketing_source_id = f"marketing:testimonial:{testimonial_id[:8]}"

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO testimonials (
                        id,
                        namespace_id,
                        customer_id,
                        project_id,
                        quote,
                        status,
                        consent,
                        consent_tier,
                        nps_at_capture,
                        marketing_source_id,
                        created_at,
                        updated_at
                    ) VALUES (
                        $1::uuid,
                        $2::uuid,
                        $3,
                        $4,
                        '',
                        'requested',
                        false,
                        'none',
                        $5,
                        $6,
                        now(),
                        now()
                    )
                    ON CONFLICT (id) DO UPDATE
                        SET status = EXCLUDED.status,
                            updated_at = now()
                    """,
                    UUID(testimonial_id),
                    UUID(ns_str),
                    customer_id,
                    project_id,
                    nps_score,
                    marketing_source_id,
                )
        except Exception as exc:
            log.warning("do_request_testimonial DB write error: %s", exc)

    from nce.vertical_modules.marketing.events import (
        EVENT_MARKETING_TESTIMONIAL_REQUESTED,
        emit_marketing_event,
    )

    await emit_marketing_event(
        engine,
        ns_str,
        EVENT_MARKETING_TESTIMONIAL_REQUESTED,
        {"customer_id": customer_id, "project_id": str(project_id or "")},
    )

    return {
        "ok": True,
        "testimonial_id": testimonial_id,
        "status": "requested",
        "customer_id": customer_id,
        "project_id": project_id,
        "nps_score": nps_score,
        "consent": False,
        "consent_tier": "none",
        "marketing_source_id": marketing_source_id,
    }


async def do_capture_testimonial(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Capture customer quote with structured two-tier consent (MK-4).

    Parameters
    ----------
    engine : Any
        NCEEngine or context providing pg_pool.
    params : dict[str, Any]
        - namespace_id (str | UUID): active tenant
        - testimonial_id (str | UUID, optional): existing record ID
        - customer_id (str): customer identifier
        - project_id (str, optional): project identifier
        - quote (str): customer quote body
        - consent (bool): explicit consent confirmation
        - consent_tier (str): 'web_retractable' or 'ai_citable_irrevocable'
        - consent_scope (dict, optional): scope constraints
        - attribution_name (str, optional): author attribution
        - attribution_title (str, optional): author title

    Returns
    -------
    dict[str, Any]
        Captured testimonial record in status='received'.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("namespace_id is required")
    ns_str = str(raw_ns)

    quote = str(params.get("quote") or "").strip()
    if not quote:
        raise ValueError("quote text cannot be blank")

    consent = bool(params.get("consent", False))
    if not consent:
        raise MarketingConsentMissingError(
            "Testimonial capture requires explicit customer consent (MK-4 refusal)."
        )

    consent_tier = str(params.get("consent_tier") or "").strip()
    if consent_tier not in VALID_CONSENT_TIERS:
        raise ValueError(
            f"Invalid consent_tier {consent_tier!r}. Must be one of {sorted(VALID_CONSENT_TIERS)} (MK-4)."
        )

    testimonial_id = str(params.get("testimonial_id") or uuid4())
    customer_id = str(params.get("customer_id") or "CUST-UNKNOWN").strip()
    project_id = str(params.get("project_id") or "").strip() or None
    consent_scope = params.get("consent_scope") or {}
    attribution_name = params.get("attribution_name")
    attribution_title = params.get("attribution_title")
    now_iso = datetime.now(timezone.utc).isoformat()
    marketing_source_id = f"marketing:testimonial:{testimonial_id[:8]}"

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO testimonials (
                        id,
                        namespace_id,
                        customer_id,
                        project_id,
                        quote,
                        status,
                        consent,
                        consent_tier,
                        consent_scope,
                        consent_recorded_at,
                        marketing_source_id,
                        created_at,
                        updated_at
                    ) VALUES (
                        $1::uuid,
                        $2::uuid,
                        $3,
                        $4,
                        $5,
                        'received',
                        true,
                        $6,
                        $7::jsonb,
                        now(),
                        $8,
                        now(),
                        now()
                    )
                    ON CONFLICT (id) DO UPDATE
                        SET quote = EXCLUDED.quote,
                            status = 'received',
                            consent = true,
                            consent_tier = EXCLUDED.consent_tier,
                            consent_scope = EXCLUDED.consent_scope,
                            consent_recorded_at = now(),
                            updated_at = now()
                    WHERE testimonials.namespace_id = $2::uuid
                    """,
                    UUID(testimonial_id),
                    UUID(ns_str),
                    customer_id,
                    project_id,
                    quote,
                    consent_tier,
                    json.dumps(consent_scope),
                    marketing_source_id,
                )
        except Exception as exc:
            log.warning("do_capture_testimonial DB write error: %s", exc)

    from nce.vertical_modules.marketing.events import (
        EVENT_MARKETING_TESTIMONIAL_CAPTURED,
        emit_marketing_event,
    )

    await emit_marketing_event(
        engine,
        ns_str,
        EVENT_MARKETING_TESTIMONIAL_CAPTURED,
        {"testimonial_id": testimonial_id, "consent_tier": consent_tier},
    )

    return {
        "ok": True,
        "testimonial_id": testimonial_id,
        "status": "received",
        "customer_id": customer_id,
        "project_id": project_id,
        "quote": quote,
        "consent": True,
        "consent_tier": consent_tier,
        "consent_scope": consent_scope,
        "consent_recorded_at": now_iso,
        "attribution_name": attribution_name,
        "attribution_title": attribution_title,
        "marketing_source_id": marketing_source_id,
    }


async def do_retract_testimonial(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Retract consent for a testimonial and trigger source retirement.

    Flips testimonial status to 'retracted' and sets consent=false.
    Derived content assets tagged with marketing_source_id are retired.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("namespace_id is required")
    ns_str = str(raw_ns)

    testimonial_id = str(params.get("testimonial_id") or "").strip()
    if not testimonial_id:
        raise ValueError("testimonial_id is required")

    reason = str(params.get("reason") or "Customer consent retracted")

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE testimonials
                    SET    status = 'retracted',
                           consent = false,
                           consent_tier = 'none',
                           updated_at = now()
                    WHERE  namespace_id = $1::uuid
                      AND  id = $2::uuid
                    RETURNING marketing_source_id
                    """,
                    UUID(ns_str),
                    UUID(testimonial_id),
                )
                if row and row["marketing_source_id"]:
                    source_id = row["marketing_source_id"]
                    # Retire any derived content assets with the same source ID
                    await conn.execute(
                        """
                        UPDATE content_assets
                        SET    status = 'retracted'
                        WHERE  namespace_id = $1::uuid
                          AND  marketing_source_id = $2
                        """,
                        UUID(ns_str),
                        source_id,
                    )
        except Exception as exc:
            log.warning("do_retract_testimonial DB write error: %s", exc)

    from nce.vertical_modules.marketing.events import (
        EVENT_MARKETING_TESTIMONIAL_RETRACTED,
        emit_marketing_event,
    )

    await emit_marketing_event(
        engine,
        ns_str,
        EVENT_MARKETING_TESTIMONIAL_RETRACTED,
        {"testimonial_id": testimonial_id, "reason": reason},
    )

    return {
        "ok": True,
        "testimonial_id": testimonial_id,
        "status": "retracted",
        "consent": False,
        "reason": reason,
    }
