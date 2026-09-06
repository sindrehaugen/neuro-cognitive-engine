"""
nce/vertical_modules/assets/health.py
=====================================
Asset Health Computation and HealthScore Writer (Module 9, Phase 2 — B3).

Per ``docs/vertical_engines/09-assets-engine.md``'s ``do_compute_health`` spec:
  - Fuses latest telemetry + NetBox MTBF (netbox/mtbf.py) + open tickets + age-vs-lifespan
    into a 0–100 score.
  - Flags DEGRADED transition past threshold (ACTIVE -> DEGRADED).
  - Appends health evaluations and degraded alerts to ``v3_cognitive_ledger``.
  - Enforces RS-3: Declares input coverage explicitly (e.g. "age-only, no telemetry").
  - Enforces RS-3: Predictive-failure Watcher stays strictly SILENT on mock telemetry.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.assets.lifecycle import advance

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.assets.health")

_CONFIG_FILENAME = "asset-health-weights.json"
_CONFIG_PATH = Path(__file__).parent / _CONFIG_FILENAME

_DEFAULT_LIFESPAN_DAYS = 1825.0  # 5 years
_DEFAULT_DEGRADED_THRESHOLD = 60.0
_DEFAULT_PREDICTIVE_THRESHOLD = 0.60
_ZERO_TENSOR = [0.0] * 8
_MODEL_VERSION = "assets-v1"


def load_health_weights() -> dict[str, Any]:
    """Load config-as-IP health scoring weights and thresholds.

    Returns
    -------
    dict
        Parsed JSON from ``asset-health-weights.json``.
    """
    if not _CONFIG_PATH.exists():
        # Fallback defaults if config file is missing
        return {
            "weights": {
                "telemetry_weight": 0.35,
                "mtbf_weight": 0.25,
                "tickets_weight": 0.20,
                "age_weight": 0.20,
            },
            "thresholds": {
                "degraded_threshold": _DEFAULT_DEGRADED_THRESHOLD,
                "predictive_failure_threshold": _DEFAULT_PREDICTIVE_THRESHOLD,
            },
            "defaults": {
                "expected_lifespan_days": _DEFAULT_LIFESPAN_DAYS,
                "baseline_mtbf_years": 10.0,
            },
        }

    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _as_uuid(raw: Any, field_name: str) -> UUID:
    if not raw:
        raise ValueError(f"do_compute_health: '{field_name}' is required")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


# ---------------------------------------------------------------------------
# Pure health computation & RS-3 reducer
# ---------------------------------------------------------------------------


def compute_asset_health(
    *,
    created_at: datetime,
    current_state: str,
    telemetry_samples: list[dict[str, Any]],
    mtbf_prob_fail: float | None = None,
    open_tickets: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    weights_config: dict[str, Any] | None = None,
    degraded_threshold_override: float | None = None,
) -> dict[str, Any]:
    """Pure reducer for asset health calculation. Zero DB, zero side effects.

    Parameters
    ----------
    created_at:
        When the asset was seeded/installed.
    current_state:
        The asset's current 14-state lifecycle position (e.g. "ACTIVE").
    telemetry_samples:
        List of recent telemetry readings (each with "metric", "value", and optional "raw").
    mtbf_prob_fail:
        Estimated failure probability (0.0 to 1.0) from NetBox MTBF forecaster, or None.
    open_tickets:
        List of open service tickets for this asset (each with "priority"), or None.
    now:
        Reference datetime. Defaults to current UTC time.
    weights_config:
        Parsed weights config dict, or None to load default.
    degraded_threshold_override:
        Optional threshold override for DEGRADED transition.

    Returns
    -------
    dict
        Structured health calculation results including RS-3 coverage declaration
        and predictive failure status.
    """
    cfg = weights_config or load_health_weights()
    weights = cfg.get("weights", {})
    thresholds = cfg.get("thresholds", {})
    defaults = cfg.get("defaults", {})

    w_telem = float(weights.get("telemetry_weight", 0.35))
    w_mtbf = float(weights.get("mtbf_weight", 0.25))
    w_tix = float(weights.get("tickets_weight", 0.20))
    w_age = float(weights.get("age_weight", 0.20))

    degraded_thresh = (
        degraded_threshold_override
        if degraded_threshold_override is not None
        else float(thresholds.get("degraded_threshold", _DEFAULT_DEGRADED_THRESHOLD))
    )
    predictive_thresh = float(
        thresholds.get("predictive_failure_threshold", _DEFAULT_PREDICTIVE_THRESHOLD)
    )
    lifespan_days = float(defaults.get("expected_lifespan_days", _DEFAULT_LIFESPAN_DAYS))

    ref_now = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    # 1. Age subscore (0 - 100)
    age_seconds = max(0.0, (ref_now - created_at).total_seconds())
    age_days = age_seconds / 86400.0
    age_ratio = min(3.0, age_days / lifespan_days) if lifespan_days > 0 else 1.0
    # Wear curve: 100 at age 0, ~50 at expected lifespan, down towards 10 if way past lifespan
    age_score = max(0.0, min(100.0, 100.0 * (1.0 - (age_ratio * 0.5))))

    # 2. Telemetry subscore (0 - 100) & source check
    has_telemetry = bool(telemetry_samples)
    telemetry_score: float | None = None
    is_mock_telemetry = False
    telemetry_source = "none"

    if has_telemetry:
        # Check if source is mock
        sources: set[str] = set()
        for sample in telemetry_samples:
            raw = sample.get("raw") or {}
            src = raw.get("source") if isinstance(raw, dict) else None
            if src:
                sources.add(str(src).lower())
            elif sample.get("platform"):
                sources.add(str(sample["platform"]).lower())

        is_mock_telemetry = ("mock" in sources) or (not sources)
        telemetry_source = "mock" if is_mock_telemetry else "real"

        # Evaluate metrics
        metric_penalties = 0.0
        for s in telemetry_samples:
            m = str(s.get("metric") or "").lower()
            val = float(s.get("value") or 0.0)
            if "temp" in m:
                if val > 80.0:
                    metric_penalties += 30.0
                elif val > 65.0:
                    metric_penalties += 15.0
            elif "packet" in m or "loss" in m:
                if val > 10.0:
                    metric_penalties += 35.0
                elif val > 2.0:
                    metric_penalties += 10.0
            elif "uptime" in m:
                if val < 60.0:  # recently rebooted / flapping
                    metric_penalties += 20.0

        telemetry_score = max(0.0, min(100.0, 100.0 - metric_penalties))

    # 3. NetBox MTBF subscore (0 - 100)
    has_mtbf = mtbf_prob_fail is not None
    mtbf_score: float | None = None
    if has_mtbf and mtbf_prob_fail is not None:
        clamped_prob = max(0.0, min(1.0, float(mtbf_prob_fail)))
        mtbf_score = max(0.0, min(100.0, 100.0 * (1.0 - clamped_prob)))

    # 4. Open tickets subscore (0 - 100)
    has_tickets = bool(open_tickets)
    ticket_score: float | None = None
    if has_tickets and open_tickets is not None:
        tix_penalty = 0.0
        for t in open_tickets:
            prio = str(t.get("priority") or "medium").lower()
            if prio == "critical":
                tix_penalty += 45.0
            elif prio == "high":
                tix_penalty += 30.0
            elif prio == "medium":
                tix_penalty += 15.0
            else:
                tix_penalty += 5.0
        ticket_score = max(0.0, min(100.0, 100.0 - tix_penalty))

    # 5. Sparse-input weight fusion & RS-3 coverage declaration
    active_weights: list[tuple[float, float]] = []
    # Age is always available
    active_weights.append((age_score, w_age))

    if telemetry_score is not None:
        active_weights.append((telemetry_score, w_telem))
    if mtbf_score is not None:
        active_weights.append((mtbf_score, w_mtbf))
    if ticket_score is not None:
        active_weights.append((ticket_score, w_tix))

    total_active_w = sum(w for _, w in active_weights)
    fused_score = (
        sum(score * w for score, w in active_weights) / total_active_w
        if total_active_w > 0
        else age_score
    )
    health_score = round(max(0.0, min(100.0, fused_score)), 2)

    # Coverage declaration (RS-3)
    if not has_telemetry and not has_mtbf and not has_tickets:
        coverage = "age-only, no telemetry"
    elif has_telemetry and not has_mtbf and not has_tickets:
        coverage = f"partial (age, {telemetry_source} telemetry; no mtbf, no tickets)"
    elif not has_telemetry and (has_mtbf or has_tickets):
        coverage = "partial (age, without telemetry)"
    else:
        coverage = f"fused (age, {telemetry_source} telemetry"
        if has_mtbf:
            coverage += ", mtbf"
        if has_tickets:
            coverage += ", tickets"
        coverage += ")"

    # 6. RS-3 Predictive-failure Watcher
    # Rule: Predictive failure must NOT fire when telemetry is from the mock adapter.
    predictive_failure = False
    predictive_failure_alert: dict[str, Any] | None = None
    predictive_failure_suppressed = False
    suppression_reason: str | None = None

    # Determine if failure risk is high
    high_failure_risk = False
    failure_reasons: list[str] = []

    if mtbf_prob_fail is not None and mtbf_prob_fail >= predictive_thresh:
        high_failure_risk = True
        failure_reasons.append(
            f"MTBF failure probability {mtbf_prob_fail:.2f} >= threshold {predictive_thresh:.2f}"
        )

    if telemetry_score is not None and telemetry_score < 40.0:
        high_failure_risk = True
        failure_reasons.append(
            f"telemetry subscore {telemetry_score:.1f} indicates critical degradation"
        )

    if high_failure_risk:
        if is_mock_telemetry:
            # RS-3 ENFORCEMENT: SILENT ON MOCK TELEMETRY
            predictive_failure = False
            predictive_failure_alert = None
            predictive_failure_suppressed = True
            suppression_reason = (
                "RS-3: predictive failure watcher silenced on mock telemetry adapter"
            )
        else:
            # REAL TELEMETRY: Predictive failure alert fires!
            predictive_failure = True
            predictive_failure_suppressed = False
            predictive_failure_alert = {
                "alert": "predictive_failure_warning",
                "severity": "critical" if health_score < 40.0 else "high",
                "health_score": health_score,
                "reasons": failure_reasons,
                "telemetry_source": telemetry_source,
                "raised_at": ref_now.isoformat(),
            }

    # 7. Lifecycle state transition: ACTIVE -> DEGRADED if below threshold
    transitioned_to_degraded = False
    new_lifecycle_state = current_state

    if health_score < degraded_thresh and current_state == "ACTIVE":
        adv_res = advance({"lifecycle_state": current_state}, "DEGRADED")
        if adv_res.get("ok") and adv_res.get("changed"):
            transitioned_to_degraded = True
            new_lifecycle_state = "DEGRADED"

    return {
        "health_score": health_score,
        "coverage": coverage,
        "summary": f"health {round(health_score)} — {coverage}",
        "coverage_details": {
            "age": True,
            "telemetry": has_telemetry,
            "telemetry_source": telemetry_source,
            "mtbf": has_mtbf,
            "tickets": has_tickets,
        },
        "input_scores": {
            "age_score": round(age_score, 2),
            "telemetry_score": round(telemetry_score, 2) if telemetry_score is not None else None,
            "mtbf_score": round(mtbf_score, 2) if mtbf_score is not None else None,
            "ticket_score": round(ticket_score, 2) if ticket_score is not None else None,
        },
        "lifecycle_state": current_state,
        "new_lifecycle_state": new_lifecycle_state,
        "degraded_threshold": degraded_thresh,
        "transitioned_to_degraded": transitioned_to_degraded,
        "predictive_failure": predictive_failure,
        "predictive_failure_alert": predictive_failure_alert,
        "predictive_failure_suppressed": predictive_failure_suppressed,
        "suppression_reason": suppression_reason,
    }


# ---------------------------------------------------------------------------
# Async Core: do_compute_health
# ---------------------------------------------------------------------------


async def do_compute_health(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Compute and persist health score for an asset (Module 9, Phase 2 — B3).

    Fuses latest telemetry + NetBox MTBF + open tickets + age.
    Persists DEGRADED transition to assets table and appends event to v3_cognitive_ledger.

    Parameters
    ----------
    engine:
        Connected NCEEngine instance.
    params:
        ``{"namespace_id": str | UUID, "asset_id": str | UUID, ...}``

    Returns
    -------
    dict
        ``{"ok": True, "asset_id": str, "health_score": float, "coverage": str, ...}``
    """
    ns_uuid = _as_uuid(params.get("namespace_id"), "namespace_id")
    asset_id = _as_uuid(params.get("asset_id"), "asset_id")
    thresh_override = (
        float(params["degraded_threshold"])
        if params.get("degraded_threshold") is not None
        else None
    )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # 1. Fetch asset with explicit namespace_id predicate
        asset_row = await conn.fetchrow(
            """
            SELECT id, namespace_id, serial, lifecycle_state, created_at
            FROM assets
            WHERE id = $1::uuid AND namespace_id = $2::uuid
            """,
            str(asset_id),
            str(ns_uuid),
        )
        if not asset_row:
            raise ValueError(
                f"do_compute_health: asset {asset_id} not found in namespace {ns_uuid}"
            )

        current_state = str(asset_row["lifecycle_state"])
        created_at = asset_row["created_at"]

        # 2. Fetch latest telemetry samples
        samples_rows = await conn.fetch(
            """
            SELECT metric, value, sampled_at, raw
            FROM telemetry_samples
            WHERE namespace_id = $1::uuid AND asset_id = $2::uuid
            ORDER BY sampled_at DESC
            LIMIT 50
            """,
            str(ns_uuid),
            str(asset_id),
        )
        telemetry_samples = [
            {
                "metric": r["metric"],
                "value": r["value"],
                "sampled_at": r["sampled_at"],
                "raw": json.loads(r["raw"]) if isinstance(r["raw"], str) else (r["raw"] or {}),
            }
            for r in samples_rows
        ]

        # 3. Fetch open service tickets if table exists
        open_tickets: list[dict[str, Any]] | None = None
        try:
            ticket_rows = await conn.fetch(
                """
                SELECT priority, status
                FROM service_tickets
                WHERE namespace_id = $1::uuid
                  AND asset_id = $2::uuid
                  AND status IN ('open', 'in_progress', 'waiting_customer', 'waiting_parts')
                """,
                str(ns_uuid),
                str(asset_id),
            )
            open_tickets = [dict(r) for r in ticket_rows]
        except Exception:
            # Table may not exist in current migration state or query failed
            open_tickets = None

        # 4. Optional NetBox MTBF failure probability
        mtbf_prob_fail = (
            float(params["mtbf_prob_fail"]) if params.get("mtbf_prob_fail") is not None else None
        )

        # 5. Compute health
        result = compute_asset_health(
            created_at=created_at,
            current_state=current_state,
            telemetry_samples=telemetry_samples,
            mtbf_prob_fail=mtbf_prob_fail,
            open_tickets=open_tickets,
            degraded_threshold_override=thresh_override,
        )

        # 6. If DEGRADED transition triggered, persist update
        if result["transitioned_to_degraded"]:
            await conn.execute(
                """
                UPDATE assets
                SET lifecycle_state = 'DEGRADED', updated_at = NOW()
                WHERE id = $1::uuid AND namespace_id = $2::uuid
                """,
                str(asset_id),
                str(ns_uuid),
            )
            log.info(
                "do_compute_health asset=%s transitioned ACTIVE -> DEGRADED (health_score=%.2f)",
                asset_id,
                result["health_score"],
            )

        # 7. Append audit event to v3_cognitive_ledger
        ledger_id = uuid4()
        tlx_payload = {
            "event": "asset_health_computed",
            "asset_id": str(asset_id),
            "health_score": result["health_score"],
            "coverage": result["coverage"],
            "previous_state": current_state,
            "new_state": result["new_lifecycle_state"],
            "transitioned_to_degraded": result["transitioned_to_degraded"],
            "predictive_failure": result["predictive_failure"],
        }
        try:
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (
                    id, namespace_id, memory_id,
                    empathic_tensor, tlx_scores, vad_scores,
                    model_version, created_at
                ) VALUES (
                    $1::uuid, $2::uuid, NULL,
                    $3::float[], $4::jsonb, '{}'::jsonb,
                    $5, NOW()
                )
                """,
                ledger_id,
                str(ns_uuid),
                _ZERO_TENSOR,
                json.dumps(tlx_payload),
                _MODEL_VERSION,
            )
        except Exception as exc:
            log.debug("Failed appending health computation to v3_cognitive_ledger: %s", exc)

    return {
        "ok": True,
        "asset_id": str(asset_id),
        **result,
    }
