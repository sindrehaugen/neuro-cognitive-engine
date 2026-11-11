"""Procedural memory distillation and heuristic retrieval (Phase 3.4).

Synthesizes deterministic procedural rules from settled governed decisions,
decision feedback events, and resolved contradictions, and persists them into
the procedural_heuristics table with Row Level Security.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

log = logging.getLogger("nce.procedural_memory")


async def distill_procedural_heuristics(
    conn: Any,
    namespace_id: str | UUID,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Distill settled decisions, feedback, and contradictions into procedural heuristics.

    Queries settled rows in action_approval_queue, decision_feedback events in event_log,
    and resolved contradictions, synthesizes deterministic procedural rules, and upserts
    them into procedural_heuristics for the given namespace.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    effective_limit = max(1, min(int(limit), 1000))

    distilled_candidates: list[dict[str, Any]] = []

    # 1. Query settled rows in action_approval_queue
    try:
        aaq_rows = await conn.fetch(
            """
            SELECT id, namespace_id, agent_id, action_type, target_system,
                   target_entity_id, proposed_payload, status, dry_run_result,
                   created_at, resolved_at, resolved_by
            FROM action_approval_queue
            WHERE namespace_id = $1 AND status IN ('approved', 'rejected', 'executed')
            ORDER BY created_at DESC
            LIMIT $2
            """,
            ns_uuid,
            effective_limit,
        )
        for r in aaq_rows:
            action_type = str(r.get("action_type") or "action")
            target_system = str(r.get("target_system") or "system")
            status = str(r.get("status") or "")
            resolved_by = r.get("resolved_by") or "governor"
            r_id = str(r.get("id"))

            if status in ("approved", "executed"):
                rule_text = f"ALLOW: Verified execution of {action_type} on {target_system}."
                trigger_condition = (
                    f"action_type == '{action_type}' and target_system == '{target_system}'"
                )
                action_guidance = f"Proceed with {action_type} on {target_system} (settled {status} by {resolved_by})."
                conf = 0.85
            else:
                rule_text = f"REJECT: Block unapproved or unsafe {action_type} on {target_system}."
                trigger_condition = (
                    f"action_type == '{action_type}' and target_system == '{target_system}'"
                )
                action_guidance = f"Do not proceed with {action_type} on {target_system} without explicit review (rejected by {resolved_by})."
                conf = 0.90

            distilled_candidates.append(
                {
                    "rule_text": rule_text,
                    "trigger_condition": trigger_condition,
                    "action_guidance": action_guidance,
                    "source_type": "governed_decision",
                    "source_refs": [{"source": "action_approval_queue", "id": r_id}],
                    "confidence_weight": conf,
                }
            )
    except Exception as exc:
        log.warning("Failed to query action_approval_queue for procedural distillation: %s", exc)

    # 2. Query decision_feedback events from event_log
    try:
        feedback_rows = await conn.fetch(
            """
            SELECT id, namespace_id, agent_id, event_type, event_seq,
                   occurred_at, params, result_summary, parent_event_id
            FROM event_log
            WHERE namespace_id = $1 AND event_type = 'decision_feedback'
            ORDER BY occurred_at DESC
            LIMIT $2
            """,
            ns_uuid,
            effective_limit,
        )
        for r in feedback_rows:
            params = r.get("params")
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    params = {}
            if not isinstance(params, dict):
                params = {}

            decision = str(params.get("decision") or "feedback")
            engine_name = str(params.get("engine") or "general")
            context_id = str(params.get("context_id") or "")
            r_id = str(r.get("id"))

            rule_text = (
                f"FEEDBACK: Prefer learned decision '{decision}' for engine '{engine_name}'."
            )
            trigger_condition = f"engine == '{engine_name}'"
            action_guidance = (
                f"Apply calibrated decision feedback '{decision}' in context '{context_id}'."
            )

            distilled_candidates.append(
                {
                    "rule_text": rule_text,
                    "trigger_condition": trigger_condition,
                    "action_guidance": action_guidance,
                    "source_type": "decision_feedback",
                    "source_refs": [{"source": "event_log:decision_feedback", "id": r_id}],
                    "confidence_weight": 0.75,
                }
            )
    except Exception as exc:
        log.warning("Failed to query event_log for decision_feedback: %s", exc)

    # 3. Query resolved contradictions
    try:
        contra_rows = await conn.fetch(
            """
            SELECT id, namespace_id, memory_a_id, memory_b_id, agent_id,
                   detected_at, detection_path, signals, confidence,
                   resolution, resolved_at, resolved_by, note
            FROM contradictions
            WHERE namespace_id = $1 AND resolved_at IS NOT NULL
            ORDER BY resolved_at DESC
            LIMIT $2
            """,
            ns_uuid,
            effective_limit,
        )
        for r in contra_rows:
            resolution = str(r.get("resolution") or r.get("note") or "resolved")
            mem_a = str(r.get("memory_a_id"))
            mem_b = str(r.get("memory_b_id"))
            r_id = str(r.get("id"))

            rule_text = (
                f"CONTRADICTION_RESOLUTION: Adhere to resolution for memory pair ({resolution})."
            )
            trigger_condition = f"conflicting_assertions between {mem_a} and {mem_b}"
            action_guidance = f"Resolve assertion conflict according to settlement: {resolution}."

            distilled_candidates.append(
                {
                    "rule_text": rule_text,
                    "trigger_condition": trigger_condition,
                    "action_guidance": action_guidance,
                    "source_type": "contradiction_resolution",
                    "source_refs": [{"source": "contradictions", "id": r_id}],
                    "confidence_weight": 0.80,
                }
            )
    except Exception as exc:
        log.warning("Failed to query contradictions for procedural distillation: %s", exc)

    if not distilled_candidates:
        return []

    # Query active existing heuristics for namespace to deduplicate
    existing_rows = await conn.fetch(
        """
        SELECT id, rule_text, trigger_condition, action_guidance, source_type,
               source_refs, confidence_weight, support_count
        FROM procedural_heuristics
        WHERE namespace_id = $1 AND valid_from <= NOW() AND (valid_to IS NULL OR valid_to > NOW())
        """,
        ns_uuid,
    )

    existing_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_rows:
        key = (str(row["rule_text"]).strip(), str(row["trigger_condition"]).strip())
        raw_refs = row["source_refs"]
        if isinstance(raw_refs, str):
            try:
                raw_refs = json.loads(raw_refs)
            except Exception:
                raw_refs = []
        if not isinstance(raw_refs, list):
            raw_refs = []
        existing_map[key] = {
            "id": row["id"],
            "confidence_weight": float(row["confidence_weight"]),
            "support_count": int(row["support_count"]),
            "source_refs": raw_refs,
        }

    results: list[dict[str, Any]] = []

    for cand in distilled_candidates:
        key = (cand["rule_text"].strip(), cand["trigger_condition"].strip())
        if key in existing_map:
            ex = existing_map[key]
            new_support = ex["support_count"] + 1
            new_conf = min(1.0, round(ex["confidence_weight"] + 0.05, 4))
            merged_refs = list(ex["source_refs"])
            for ref in cand["source_refs"]:
                if ref not in merged_refs:
                    merged_refs.append(ref)

            await conn.execute(
                """
                UPDATE procedural_heuristics
                SET support_count = $1, confidence_weight = $2, source_refs = $3::jsonb, updated_at = NOW()
                WHERE id = $4
                """,
                new_support,
                new_conf,
                json.dumps(merged_refs, default=str),
                ex["id"],
            )
            ex["support_count"] = new_support
            ex["confidence_weight"] = new_conf
            ex["source_refs"] = merged_refs
            results.append(
                {
                    "id": ex["id"],
                    "rule_text": cand["rule_text"],
                    "trigger_condition": cand["trigger_condition"],
                    "action_guidance": cand["action_guidance"],
                    "source_type": cand["source_type"],
                    "source_refs": merged_refs,
                    "confidence_weight": new_conf,
                    "support_count": new_support,
                }
            )
        else:
            inserted = await conn.fetchrow(
                """
                INSERT INTO procedural_heuristics (
                    namespace_id, rule_text, trigger_condition, action_guidance,
                    source_type, source_refs, confidence_weight, support_count,
                    valid_from, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8, NOW(), NOW(), NOW()
                )
                RETURNING id, namespace_id, memory_id, rule_text, trigger_condition,
                          action_guidance, source_type, source_refs, confidence_weight,
                          support_count, valid_from, valid_to, created_at, updated_at
                """,
                ns_uuid,
                cand["rule_text"],
                cand["trigger_condition"],
                cand["action_guidance"],
                cand["source_type"],
                json.dumps(cand["source_refs"], default=str),
                float(cand["confidence_weight"]),
                1,
            )
            if inserted:
                inst_dict = dict(inserted)
                existing_map[key] = {
                    "id": inst_dict["id"],
                    "confidence_weight": float(inst_dict["confidence_weight"]),
                    "support_count": int(inst_dict["support_count"]),
                    "source_refs": cand["source_refs"],
                }
                results.append(inst_dict)

    return results


async def fetch_relevant_procedural_heuristics(
    conn: Any,
    namespace_id: str | UUID,
    goal: str,
    *,
    limit: int = 10,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Retrieve procedural heuristics relevant to a goal, respecting temporal cutoff."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    effective_limit = max(1, min(int(limit), 100))

    if as_of is not None and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    rows = await conn.fetch(
        """
        SELECT id, namespace_id, memory_id, rule_text, trigger_condition,
               action_guidance, source_type, source_refs, confidence_weight,
               support_count, valid_from, valid_to, created_at, updated_at
        FROM procedural_heuristics
        WHERE namespace_id = $1
          AND valid_from <= COALESCE($2, NOW())
          AND (valid_to IS NULL OR valid_to > COALESCE($2, NOW()))
        ORDER BY (
            COALESCE(ts_rank_cd(search_tsv, plainto_tsquery('english', $3)), 0.0) +
            COALESCE(ts_rank_cd(search_tsv, plainto_tsquery('simple', $3)), 0.0) +
            (confidence_weight * ln(1.0 + support_count))
        ) DESC, id ASC
        LIMIT $4
        """,
        ns_uuid,
        as_of,
        goal,
        effective_limit,
    )

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        refs = d.get("source_refs")
        if isinstance(refs, str):
            try:
                d["source_refs"] = json.loads(refs)
            except Exception:
                d["source_refs"] = []
        out.append(d)
    return out
