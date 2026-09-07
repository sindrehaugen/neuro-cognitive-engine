"""
nce/vertical_modules/business_insights/ask.py
=============================================
Natural Language 'Ask Your Business' executive query surface for Module 16.

Enforces:
  - BI-1: Structural Person-Grain Barrier (EU AI Act Article 5 - never rank people).
  - BI-3: Third-Party AI Data Egress Boundary (OFF by default, recorded sign-off, audit).
  - Cognitive recall from episodic memories ('have we seen quarters like this').
  - Provenance on every claim (resolved graph nodes).
  - Every query audited to v3_cognitive_ledger.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.vertical_modules.business_insights._guard import (
    BusinessInsightsDataUnavailableError,
    ThirdPartyEgressUnauthorizedError,
    require_insights_role,
)
from nce.vertical_modules.business_insights.aggregation import (
    FORBIDDEN_PERSON_DIMENSIONS,
    enforce_aggregation_barrier,
    enforce_shape_guarantee,
)
from nce.vertical_modules.business_insights.provenance import record_ledger_audit

log = logging.getLogger("nce.vertical_modules.business_insights.ask")


async def do_ask_business(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a natural language query over the unified enterprise cognitive graph.

    Params:
      - namespace_id: str | UUID (required)
      - principal_role: str (required, exec or board)
      - question: str (required NL question)
      - actor: str (optional, e.g. email)
      - is_third_party_ai_client: bool (default False)
      - third_party_egress_enabled: bool (default False, BI-3)
      - recorded_signoff: dict | None (required if third-party AI client)
      - data_override: dict (optional)
    """
    namespace_id = params.get("namespace_id") or params.get("namespace")
    if not namespace_id:
        raise ValueError("namespace_id is required for do_ask_business")

    principal_role = params.get("principal_role") or params.get("caller_role") or "executive"
    await require_insights_role(principal_role, allow_board=True)

    question = (params.get("question") or params.get("query") or "").strip()
    if not question:
        raise ValueError("question is required for do_ask_business")

    actor = params.get("actor", "system")
    is_third_party = bool(params.get("is_third_party_ai_client", False)) or bool(
        params.get("allow_external_ai", False)
    )
    egress_enabled = bool(params.get("third_party_egress_enabled", False)) or bool(
        params.get("allow_external_ai", False)
    )
    signoff = params.get("recorded_signoff") or params.get("board_signoff_reference")
    has_signoff = False
    if isinstance(signoff, dict) and signoff.get("signed_by"):
        has_signoff = True
    elif isinstance(signoff, str) and signoff.strip():
        has_signoff = True

    # 1. BI-3 Third-Party AI Data Egress Boundary Enforcement
    if is_third_party:
        if not egress_enabled:
            raise ThirdPartyEgressUnauthorizedError(
                "Third-party AI data egress is disabled by default for this namespace. "
                "Enabling egress requires an explicit executive policy update."
            )
        if not has_signoff:
            raise ThirdPartyEgressUnauthorizedError(
                "Third-party AI data egress requires an explicit, recorded sign-off "
                "confirming that financial data may leave NCE control."
            )

    # 2. BI-1 Structural Person-Grain Barrier Enforcement
    enforce_aggregation_barrier(
        query_text=question,
        group_by=params.get("group_by"),
    )

    # 3. Real Data Query: KPI Snapshots & Cognitive Memories (Wave C-BI2)
    provenance: list[str] = []
    cognitive_recall: dict[str, Any] = {
        "prior_similar_periods": [],
        "context_note": "No prior episodic memories recorded for this namespace.",
    }
    answer: str = ""

    pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)

    data_ov = params.get("data_override")
    raw_records = (
        params.get("records")
        or params.get("raw_records")
        or (data_ov.get("records") if isinstance(data_ov, dict) else None)
    )

    selected_rows: list[dict[str, Any]] = []

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                try:
                    ns_uuid = (
                        UUID(str(namespace_id))
                        if not isinstance(namespace_id, UUID)
                        else namespace_id
                    )
                    snapshot_rows = await conn.fetch(
                        """
                        SELECT id, namespace_id, kpi_key, value, period, captured_at,
                               source_engine, business_insights_source_id
                        FROM business_insights_kpi_snapshots
                        WHERE namespace_id = $1
                        ORDER BY captured_at DESC
                        LIMIT 50;
                        """,
                        ns_uuid,
                    )
                except Exception as exc:
                    log.warning("Failed to query business_insights_kpi_snapshots: %s", exc)
                    snapshot_rows = []

                q_lower = question.lower()
                matched = []
                for r in snapshot_rows:
                    k_key = str(r["kpi_key"]).lower()
                    s_eng = str(r["source_engine"]).lower()
                    if (
                        ("margin" in q_lower and ("margin" in k_key or "economy" in s_eng))
                        or (
                            ("arr" in q_lower or "mrr" in q_lower or "revenue" in q_lower)
                            and ("revenue" in k_key or "mrr" in k_key)
                        )
                        or (
                            ("runway" in q_lower or "cash" in q_lower)
                            and ("runway" in k_key or "cash" in k_key or "economy" in s_eng)
                        )
                        or ("pipeline" in q_lower and ("pipeline" in k_key or "sales" in s_eng))
                        or (
                            ("ticket" in q_lower or "sla" in q_lower)
                            and ("sla" in k_key or "support" in s_eng)
                        )
                        or ("delivery" in q_lower and ("delivery" in k_key or "project" in s_eng))
                        or ("savings" in q_lower and ("saving" in k_key or "procurement" in s_eng))
                        or ("stock" in q_lower and ("stock" in k_key or "inventory" in s_eng))
                        or (
                            ("capacity" in q_lower or "utilization" in q_lower)
                            and (
                                "capacity" in k_key or "resource" in s_eng or "utilization" in k_key
                            )
                        )
                    ):
                        matched.append(r)

                selected_rows = matched if matched else list(snapshot_rows[:5])

                try:
                    memory_rows = await conn.fetch(
                        """
                        SELECT id, memory_type, occurred_at
                        FROM memories
                        WHERE namespace_id = $1
                        ORDER BY occurred_at DESC
                        LIMIT 5;
                        """,
                        ns_uuid,
                    )
                    if memory_rows:
                        cognitive_recall = {
                            "prior_similar_periods": [str(m["id"]) for m in memory_rows],
                            "context_note": f"Recalled {len(memory_rows)} historical episodic memories for namespace {namespace_id}.",
                        }
                except Exception as exc:
                    log.debug("No memories fetched or table unavailable: %s", exc)
        except Exception as exc:
            log.warning("Database connection error in do_ask_business: %s", exc)

    require_grounding = bool(params.get("require_grounding", False)) or bool(
        params.get("raise_if_unavailable", False)
    )
    target_engine = params.get("target_engine")

    if selected_rows:
        provenance = [
            str(
                r.get("business_insights_source_id")
                or f"kpi_snapshot:{r.get('kpi_key')}:{r.get('period')}"
            )
            for r in selected_rows
        ]
        parts = [
            f"{r['kpi_key']} is {r['value']} for period {r['period']} (source: {r['source_engine']})"
            for r in selected_rows
        ]
        answer = f"Measured business insight for '{question}': " + "; ".join(parts) + "."
    elif raw_records:
        provenance = [f"input_record:{idx}" for idx in range(len(raw_records))]
        answer = f"Synthesized insight for '{question}' from {len(raw_records)} supplied records."
    else:
        # 0 rows touched
        if require_grounding:
            missing_target = target_engine or "kpi_snapshots"
            raise BusinessInsightsDataUnavailableError(
                f"Required data for query '{question}' is unavailable: no real measured records found for {missing_target}."
            )
        provenance = []
        answer = f"No measured data available for query '{question}' in namespace '{namespace_id}'. Upstream engine data touched 0 rows."

    # 4. Audit Query to Cognitive Ledger
    try:
        if pool is not None:
            async with pool.acquire() as conn:
                await record_ledger_audit(
                    conn=conn,
                    namespace_id=namespace_id,
                    actor=actor,
                    action="ASK_BUSINESS_QUERY",
                    referenced_nodes=provenance,
                    details={
                        "question": question,
                        "is_third_party_ai": is_third_party,
                        "signoff_id": signoff.get("signoff_id")
                        if isinstance(signoff, dict)
                        else str(signoff)
                        if signoff
                        else None,
                    },
                )
    except Exception as exc:
        log.warning("Failed to record ask_business audit to v3_cognitive_ledger: %s", exc)

    group_by = params.get("group_by") or "team"
    metric_key = params.get("metric_key") or "value"
    if raw_records and metric_key == "value" and isinstance(raw_records, list) and raw_records:
        first_rec = raw_records[0]
        if isinstance(first_rec, dict) and "value" not in first_rec:
            for k, v in first_rec.items():
                if isinstance(v, (int, float)) and k.lower() not in FORBIDDEN_PERSON_DIMENSIONS:
                    metric_key = k
                    break

    res_data = {
        "status": "ok",
        "namespace_id": str(namespace_id),
        "question": question,
        "answer": answer,
        "provenance": provenance,
        "cognitive_recall": cognitive_recall,
        "egress_authorized": is_third_party and has_signoff if is_third_party else False,
        "external_ai_invoked": is_third_party and has_signoff if is_third_party else False,
    }
    if isinstance(data_ov, dict) and "breakdown" in data_ov:
        res_data["breakdown"] = data_ov["breakdown"]

    # 5. Enforce Structural Return-Shape Guarantee (EU AI Act Art. 5 / BI-1)
    return enforce_shape_guarantee(
        res_data,
        raw_records=raw_records,
        group_by=group_by,
        metric_key=metric_key,
    )
