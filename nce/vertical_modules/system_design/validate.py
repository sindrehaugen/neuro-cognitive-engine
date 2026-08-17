"""
nce/vertical_modules/system_design/validate.py
===============================================
Human validation gate for the System Design vertical module (Wave 7,
Phase 1a step 7).

Entry-point: ``do_validate_design(engine, params) -> dict``

Goal
----
Record human accept/override decisions against a DESIGN's lines, return
``{passed: bool, reasons: [...]}`` indicating whether the design passed
validation, bump the DESIGN version (by re-upsert so ``updated_at``
advances), and append the decisions to ``v3_cognitive_ledger`` for
outcome-weighting feedback (W3).

Propose-only invariant (§9.3 / Correction #5)
----------------------------------------------
No confidence threshold may auto-accept a design line.  Every decision
in the request must carry an explicit human ``"accept"`` or ``"override"``
verdict.  The threshold rises only from measured precision later (W3 loop
outcome-weighting).

Design invariants (uncle-bob-craft)
------------------------------------
- SRP per function: each private helper has one job.
- Dependencies point inward: no web/HTTP/admin imports.
- confidence on edges only (wave rule 7 — never on kg_nodes).
- No phantom payload/metadata/state column (kg_nodes has none).
- PROPOSE-ONLY — no auto-accept regardless of confidence score.
- No slow I/O inside scoped_pg_session transactions.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.system_design.graph import (
    _design_label,
    _upsert_design_node,
)

log = logging.getLogger("nce.vertical_modules.system_design.validate")

# model_version tag written to v3_cognitive_ledger rows.
_VALIDATION_MODEL_VERSION: str = "system_design/validate/v1"

# Zero-vector for empathic_tensor — validation decisions carry no affective
# signal; we write the zero tensor to satisfy the NOT NULL constraint.
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Accepted decision verbs (§9.3 — no auto-accept; human must be explicit).
_VALID_VERDICTS: frozenset[str] = frozenset({"accept", "override"})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_decisions(decisions: list[dict[str, Any]]) -> list[str]:
    """Validate the structure of each decision dict.

    Returns a list of error strings.  Empty list = all valid.
    Each decision must have ``line_id`` and a ``verdict`` of ``'accept'``
    or ``'override'``.  No auto-accept: a missing / empty verdict is an
    error, not a silent accept.
    """
    errors: list[str] = []
    for i, decision in enumerate(decisions):
        line_id = decision.get("line_id", "")
        if not line_id:
            errors.append(f"decision[{i}]: 'line_id' is required")
        verdict = decision.get("verdict", "")
        if verdict not in _VALID_VERDICTS:
            errors.append(
                f"decision[{i}] line_id={line_id!r}: "
                f"'verdict' must be one of {sorted(_VALID_VERDICTS)}; "
                f"got {verdict!r}  (no auto-accept — §9.3)"
            )
    return errors


def _derive_pass_fail(decisions: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Determine pass/fail from validated decisions.

    A design passes when every decision is explicitly accepted (no
    outstanding overrides).  Returns ``(passed, reasons)`` where
    ``reasons`` lists any override rationales.

    Parameters
    ----------
    decisions:
        Pre-validated list of decision dicts.

    Returns
    -------
    (passed, reasons):
        ``passed`` is True iff zero override decisions exist.
        ``reasons`` lists the reason/note for each override (if any).
    """
    reasons: list[str] = []
    for d in decisions:
        if d.get("verdict") == "override":
            reason = d.get("reason") or f"line {d.get('line_id', '?')} overridden by reviewer"
            reasons.append(reason)

    passed = len(reasons) == 0
    return passed, reasons


async def _bump_design_version(
    conn: Any,
    ns_uuid: UUID,
    design_lbl: str,
    source_id: str | None,
) -> None:
    """Re-upsert the DESIGN node to advance its ``updated_at`` timestamp.

    ``_upsert_design_node`` issues ``ON CONFLICT ... SET updated_at = NOW()``,
    which advances the timestamp.  The W5 ``_derive_version_number`` hashes
    ``label|updated_at``, so any advance yields a new version number — this
    is the "bump the design version" mechanic described in Correction #7.
    """
    await _upsert_design_node(conn, ns_uuid, design_lbl, source_id)


async def _append_validation_ledger(
    conn: Any,
    ns_uuid: UUID,
    design_id: str,
    decisions: list[dict[str, Any]],
    passed: bool,
    reasons: list[str],
) -> None:
    """Append accept/override decisions to ``v3_cognitive_ledger``.

    Stores the full decision record in ``tlx_scores`` JSONB (the structured
    payload column — same pattern as survivorship.py).  ``empathic_tensor``
    is a required NOT NULL vector(6); we write a zero tensor because
    validation decisions carry no affective signal.

    This is a plain INSERT (no RETURNING needed) — the wave only requires
    that feedback be appended, not that we track the row id.
    """
    payload: dict[str, Any] = {
        "event": "design_validation",
        "design_id": design_id,
        "passed": passed,
        "reasons": reasons,
        "decisions": [
            {
                "line_id": d.get("line_id", ""),
                "verdict": d.get("verdict", ""),
                "reason": d.get("reason", ""),
            }
            for d in decisions
        ],
    }
    await conn.execute(
        """
        INSERT INTO v3_cognitive_ledger (
            namespace_id,
            memory_id,
            empathic_tensor,
            tlx_scores,
            vad_scores,
            model_version
        ) VALUES (
            $1::uuid,
            NULL,
            $2::float[],
            $3::jsonb,
            $4::jsonb,
            $5
        )
        """,
        ns_uuid,
        _ZERO_TENSOR,
        json.dumps(payload),
        json.dumps({}),
        _VALIDATION_MODEL_VERSION,
    )


# ---------------------------------------------------------------------------
# Public: do_validate_design
# ---------------------------------------------------------------------------


async def do_validate_design(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Record human validation decisions for a DESIGN.

    Validates input decisions (propose-only — no auto-accept), derives
    pass/fail, bumps the DESIGN version, and appends the decisions to
    ``v3_cognitive_ledger`` for outcome-weighting feedback (W3).

    **Propose-only (§9.3):** No confidence threshold may trigger an
    automatic acceptance.  Every line must carry an explicit human
    ``"accept"`` or ``"override"`` verdict.  The gate refuses any request
    that omits a verdict.

    Parameters
    ----------
    engine:
        NCEEngine instance.  Must have a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "design_id": str,             # required — the DESIGN node id
            "decisions": [               # required — list of line decisions
                {
                    "line_id": str,          # DESIGN_LINE label or ref
                    "verdict": "accept" | "override",
                    "reason": str,           # optional rationale for overrides
                },
                ...
            ],
            "source_id": str | None,     # optional — system_design source id
        }``

    Returns
    -------
    dict
        ``{
            "passed": bool,          # True iff zero overrides
            "reasons": list[str],    # override rationales (empty if passed)
            "decisions_recorded": int,  # number of decisions written to ledger
            "design_version_bumped": bool,  # always True on success
        }``

    Raises
    ------
    ValueError
        When required params are missing, or any decision is malformed
        (propose-only invariant — no auto-accept).
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_validate_design: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    design_id_raw: str = params.get("design_id", "")
    if not design_id_raw:
        raise ValueError("do_validate_design: 'design_id' is required in params")

    decisions: list[dict[str, Any]] = params.get("decisions", [])
    if not decisions:
        raise ValueError("do_validate_design: 'decisions' must be a non-empty list")

    source_id: str | None = params.get("source_id")
    design_lbl = _design_label(design_id_raw)

    # 1. Validate decision structure (propose-only — no auto-accept).
    errors = _validate_decisions(decisions)
    if errors:
        raise ValueError(
            "do_validate_design: malformed decisions (propose-only, §9.3): " + "; ".join(errors)
        )

    # 2. Derive pass/fail from the validated decisions.
    passed, reasons = _derive_pass_fail(decisions)

    # 3. Write: bump design version + append ledger — single scoped session.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Bump the DESIGN version by re-upsert (advances updated_at).
        await _bump_design_version(conn, ns_uuid, design_lbl, source_id)

        # Append accept/override decisions to v3_cognitive_ledger.
        await _append_validation_ledger(conn, ns_uuid, design_id_raw, decisions, passed, reasons)

    log.info(
        "do_validate_design: ns=%s design=%s passed=%s overrides=%d decisions=%d",
        ns_uuid,
        design_id_raw,
        passed,
        len(reasons),
        len(decisions),
    )

    return {
        "passed": passed,
        "reasons": reasons,
        "decisions_recorded": len(decisions),
        "design_version_bumped": True,
    }
