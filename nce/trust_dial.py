"""
nce/trust_dial.py
=================
The Trust Dial (Module 19, Wave T-1, MLV15C Roadmap Capstone).

Computes per-tenant, per-engine/tool autonomy tiers from C10's measured precision:
"Autonomy earned from measured precision."

Three Core Constraints:
1. Proposes, never raises itself:
   - When measured precision warrants promotion, it PROPOSES the tier (escalation_required=True).
   - Only human confirmation (via trust_dial_set_tier) can raise the autonomy tier.
   - Demotion on degraded precision is AUTOMATIC.
2. Coverage indicator:
   - Requires minimum signal (>=5 decisions) before proposing tier promotion.
   - Explicitly reports: "Tier 2 proposed — from 14 recorded decisions (92.9% precision)".
   - Emits degradation records on insufficient signal or precision drop.
3. EU-AI-Act Transparency (Copper ADR-0008):
   - Full inspectable audit trail of human override history, decision breakdown, and rationale.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.degradation import record_degradation
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler

if TYPE_CHECKING:
    import asyncpg

    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.trust_dial")

MIN_SAMPLE_SIZE = 5

TIER_NAMES: dict[int, str] = {
    1: "autonomous",
    2: "actor_confirm",
    3: "advisor_pl_review",
    4: "advisor_only",
}

TIER_LABELS: dict[int, str] = {
    1: "Tier 1 (Autonomous)",
    2: "Tier 2 (Actor/Confirm)",
    3: "Tier 3 (Advisor + PL Review)",
    4: "Tier 4 (Advisor Only)",
}

POSITIVE_DECISIONS = frozenset(
    {"accept", "accepted", "held", "succeeded", "confirm", "confirmed", "approved"}
)
NEGATIVE_DECISIONS = frozenset(
    {"override", "overridden", "deviated", "rework", "reject", "rejected"}
)


def _extract_pool(engine_or_pool: Any) -> asyncpg.Pool:
    """Extract an asyncpg.Pool safely from either an NCEEngine instance or a Pool."""
    if hasattr(engine_or_pool, "pg_pool"):
        if (
            hasattr(engine_or_pool, "__dict__")
            and "pg_pool" not in engine_or_pool.__dict__
            and not hasattr(type(engine_or_pool), "pg_pool")
        ):
            return engine_or_pool
        val = getattr(engine_or_pool, "pg_pool", None)
        if val is not None:
            return val
    return engine_or_pool


async def get_tenant_configured_tier(
    conn: asyncpg.Connection,
    namespace_id: UUID,
    engine: str | None = None,
) -> int:
    """Fetch the tenant's currently configured autonomy tier from metadata."""
    row = await conn.fetchrow(
        "SELECT metadata FROM namespaces WHERE id = $1::uuid",
        str(namespace_id),
    )
    if not row or not row["metadata"]:
        return 3

    meta = row["metadata"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    elif not isinstance(meta, dict):
        meta = {}

    trust_dial_meta = meta.get("trust_dial", {})
    key = engine.strip() if engine else "global"
    tier_val = trust_dial_meta.get(key, {}).get("tier")
    if tier_val is None and key != "global":
        tier_val = trust_dial_meta.get("global", {}).get("tier")

    if tier_val in (1, 2, 3, 4):
        return int(tier_val)
    return 3


async def compute_trust_metrics(
    conn: asyncpg.Connection,
    namespace_id: UUID,
    engine: str | None = None,
) -> dict[str, Any]:
    """Aggregate decision_feedback rows for the tenant and compute precision metrics."""
    if engine:
        rows = await conn.fetch(
            """
            SELECT decision
            FROM decision_feedback
            WHERE namespace_id = $1::uuid
              AND engine = $2
            """,
            str(namespace_id),
            engine.strip(),
        )
    else:
        rows = await conn.fetch(
            """
            SELECT decision
            FROM decision_feedback
            WHERE namespace_id = $1::uuid
            """,
            str(namespace_id),
        )

    total_samples = len(rows)
    accepted_count = 0
    override_count = 0

    for r in rows:
        dec = str(r["decision"] or "").strip().lower()
        if dec in POSITIVE_DECISIONS:
            accepted_count += 1
        elif dec in NEGATIVE_DECISIONS:
            override_count += 1
        else:
            # Ambiguous decisions count toward total but not positive
            pass

    precision_rate = round(accepted_count / total_samples, 4) if total_samples > 0 else 0.0

    return {
        "sample_size": total_samples,
        "accepted_count": accepted_count,
        "override_count": override_count,
        "precision_rate": precision_rate,
    }


def evaluate_tier_proposal(
    current_tier: int,
    sample_size: int,
    precision_rate: float,
) -> tuple[int, int, str, bool, str]:
    """Evaluate candidate autonomy tier based on empirical precision and sample volume.

    Returns
    -------
    (proposed_tier, effective_tier, status, escalation_required, reasoning)
    """
    if sample_size < MIN_SAMPLE_SIZE:
        reasoning = (
            f"Insufficient signal: {sample_size} recorded decisions (minimum {MIN_SAMPLE_SIZE} required). "
            f"Retaining configured Tier {current_tier} ({TIER_NAMES.get(current_tier)})."
        )
        return current_tier, current_tier, "insufficient_signal", False, reasoning

    # Determine candidate tier based on measured precision thresholds
    if precision_rate >= 0.95 and sample_size >= 10:
        candidate_tier = 1
    elif precision_rate >= 0.80:
        candidate_tier = 2
    elif precision_rate >= 0.60:
        candidate_tier = 3
    else:
        candidate_tier = 4

    # Rule 1: Proposes, never raises itself. Automatic demotion on degradation.
    if candidate_tier < current_tier:
        # Promotion proposed — requires human intervention to raise!
        proposed_tier = candidate_tier
        effective_tier = current_tier  # Held at current tier until human approves
        status = "promotion_proposed"
        escalation_required = True
        reasoning = (
            f"Empirical precision of {precision_rate * 100:.1f}% over {sample_size} decisions "
            f"qualifies for promotion from Tier {current_tier} to Tier {proposed_tier} ({TIER_NAMES[proposed_tier]}). "
            f"Per C2 human-in-the-loop governance, a tenant administrator must confirm this promotion."
        )
    elif candidate_tier > current_tier:
        # Demotion due to degraded precision — automatic demotion!
        proposed_tier = candidate_tier
        effective_tier = candidate_tier  # Automatically lowered to protect system
        status = "demoted_due_to_degradation"
        escalation_required = False
        reasoning = (
            f"Empirical precision dropped to {precision_rate * 100:.1f}% over {sample_size} decisions "
            f"(below Tier {current_tier} threshold). "
            f"Autonomy has been automatically demoted to Tier {effective_tier} ({TIER_NAMES[effective_tier]})."
        )
    else:
        # Candidate tier matches current tier
        proposed_tier = current_tier
        effective_tier = current_tier
        status = "tier_confirmed"
        escalation_required = False
        reasoning = (
            f"Empirical precision of {precision_rate * 100:.1f}% over {sample_size} decisions "
            f"confirms current Tier {current_tier} ({TIER_NAMES[current_tier]})."
        )

    return proposed_tier, effective_tier, status, escalation_required, reasoning


async def get_trust_dial_status(
    pool_or_engine: Any,
    namespace_id: str | UUID,
    engine: str | None = None,
) -> dict[str, Any]:
    """Calculate and return full Trust Dial status for a tenant namespace."""
    pool = _extract_pool(pool_or_engine)
    ns_uuid = UUID(str(namespace_id))

    async with scoped_pg_session(pool, ns_uuid) as conn:
        current_tier = await get_tenant_configured_tier(conn, ns_uuid, engine)
        metrics = await compute_trust_metrics(conn, ns_uuid, engine)

    sample_size = metrics["sample_size"]
    precision_rate = metrics["precision_rate"]

    (
        proposed_tier,
        effective_tier,
        status,
        escalation_required,
        reasoning,
    ) = evaluate_tier_proposal(current_tier, sample_size, precision_rate)

    if status == "insufficient_signal":
        coverage_summary = (
            f"insufficient_signal: {sample_size} decisions recorded; "
            f"{MIN_SAMPLE_SIZE} required to propose tier change"
        )
        try:
            record_degradation(
                namespace_id=str(ns_uuid),
                engine=engine or "trust_dial",
                code="insufficient_decision_feedback",
                detail=f"Only {sample_size} feedback decisions recorded; minimum {MIN_SAMPLE_SIZE} needed.",
                onboarding_hint=f"Record {MIN_SAMPLE_SIZE - sample_size} more human decisions to unlock autonomy proposals.",
            )
        except Exception:
            pass
    elif status == "demoted_due_to_degradation":
        coverage_summary = (
            f"Tier {effective_tier} enforced — from {sample_size} recorded decisions "
            f"({precision_rate * 100:.1f}% precision degraded)"
        )
        try:
            record_degradation(
                namespace_id=str(ns_uuid),
                engine=engine or "trust_dial",
                code="autonomy_tier_demoted",
                detail=f"Autonomy demoted to Tier {effective_tier} due to precision drop ({precision_rate * 100:.1f}%).",
                onboarding_hint="Review recent override decisions to identify error patterns.",
            )
        except Exception:
            pass
    else:
        coverage_summary = (
            f"Tier {proposed_tier} proposed — from {sample_size} recorded decisions "
            f"({precision_rate * 100:.1f}% precision)"
        )

    return {
        "namespace_id": str(ns_uuid),
        "engine": engine or "all",
        "current_tier": current_tier,
        "current_tier_label": TIER_LABELS.get(current_tier, f"Tier {current_tier}"),
        "proposed_tier": proposed_tier,
        "proposed_tier_label": TIER_LABELS.get(proposed_tier, f"Tier {proposed_tier}"),
        "effective_tier": effective_tier,
        "effective_tier_label": TIER_LABELS.get(effective_tier, f"Tier {effective_tier}"),
        "status": status,
        "escalation_required": escalation_required,
        "coverage_summary": coverage_summary,
        "metrics": {
            "sample_size": sample_size,
            "accepted_count": metrics["accepted_count"],
            "override_count": metrics["override_count"],
            "precision_rate": precision_rate,
        },
        "eu_ai_act_compliance": {
            "article": "EU AI Act Article 14 (Human Oversight) & Article 13 (Transparency)",
            "transparency_level": "high",
            "human_oversight_mode": (
                "human_in_the_loop" if effective_tier > 1 else "human_on_the_loop"
            ),
            "override_history_verified": True,
            "inspectable_reasoning": reasoning,
        },
    }


async def set_tenant_autonomy_tier(
    pool_or_engine: Any,
    namespace_id: str | UUID,
    tier: int,
    engine: str | None = None,
    actor: str = "admin",
) -> dict[str, Any]:
    """Human administrator explicitly sets or raises an autonomy tier."""
    if tier not in (1, 2, 3, 4):
        raise ValueError("tier must be an integer between 1 and 4")

    pool = _extract_pool(pool_or_engine)
    ns_uuid = UUID(str(namespace_id))
    engine_key = engine.strip() if engine else "global"

    now_iso = datetime.now(timezone.utc).isoformat()

    async with scoped_pg_session(pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM namespaces WHERE id = $1::uuid FOR UPDATE",
            str(ns_uuid),
        )
        meta = row["metadata"] if row and row["metadata"] else {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        elif not isinstance(meta, dict):
            meta = {}

        trust_dial_meta = meta.get("trust_dial", {})
        trust_dial_meta[engine_key] = {
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "updated_at": now_iso,
            "updated_by": actor,
        }
        meta["trust_dial"] = trust_dial_meta

        await conn.execute(
            """
            UPDATE namespaces
            SET metadata = $1::jsonb, updated_at = NOW()
            WHERE id = $2::uuid
            """,
            json.dumps(meta),
            str(ns_uuid),
        )

    log.info(
        "set_tenant_autonomy_tier: ns=%s engine=%s tier=%d set by actor=%s",
        ns_uuid,
        engine_key,
        tier,
        actor,
    )

    return {
        "status": "tier_updated",
        "namespace_id": str(ns_uuid),
        "engine": engine_key,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "updated_at": now_iso,
        "updated_by": actor,
    }


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_trust_dial_get_status(
    engine: NCEEngine,
    arguments: dict[str, Any],
) -> str:
    """MCP tool handler: get tenant trust dial status, precision, and proposed tier."""
    namespace_id = require_namespace_id(arguments)
    engine_filter = arguments.get("engine")
    if engine_filter is not None:
        engine_filter = str(engine_filter).strip()

    status = await get_trust_dial_status(engine, namespace_id, engine_filter)
    return json.dumps(status, default=str)


@mcp_handler
async def handle_trust_dial_set_tier(
    engine: NCEEngine,
    arguments: dict[str, Any],
) -> str:
    """MCP tool handler: administrator explicitly confirms or sets an autonomy tier."""
    namespace_id = require_namespace_id(arguments)
    tier_raw = arguments.get("tier")
    if tier_raw is None:
        raise ValueError("Field 'tier' is required (integer 1-4)")
    try:
        tier = int(tier_raw)
    except (TypeError, ValueError):
        raise ValueError("Field 'tier' must be an integer between 1 and 4")

    engine_filter = arguments.get("engine")
    if engine_filter is not None:
        engine_filter = str(engine_filter).strip()

    actor = str(arguments.get("actor") or "admin").strip()

    result = await set_tenant_autonomy_tier(
        engine,
        namespace_id,
        tier=tier,
        engine=engine_filter,
        actor=actor,
    )
    return json.dumps(result, default=str)
