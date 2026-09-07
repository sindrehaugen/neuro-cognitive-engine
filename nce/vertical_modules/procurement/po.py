"""
nce/vertical_modules/procurement/po.py
=======================================
``do_generate_po`` Actor — Wave 10 (generate-po).
``do_submit_po`` Actor — Wave 11 (submit-po).

``do_generate_po`` orchestrates:
  1. Supplier ranking (Wave 2 ``do_rank_suppliers``).
  2. BID price resolution (Wave 5 ``do_resolve_bids``).
  3. Draft purchase-order node creation (Wave 6 ``upsert_po_node``).

``do_submit_po`` places a draft PO through the C2 autonomy gate:
  1. Confirm-only default — no submit without explicit confirm.
  2. ``AUTONOMY_PO_CEILING`` — PO value above ceiling → forced human-confirm.
  3. Idempotency key — retry with the same key is a NO-OP (never double-orders).
  4. Kill-switch — blocks submit when the Redis kill-switch fires (fail-closed).
  5. ``rebate_override`` gate — calls Agreements compliance-audit via A2A;
     fail-closed if Agreements is unavailable (degrade to human-confirm).
  6. ``PoTransport`` adapter selection — ``NetsetPoTransport`` is a 🔴 stub
     (``NotImplementedError``) so no real auto-order is possible at launch.

§9.5 Contract B invariant
--------------------------
Without ``confirm=True`` the wrapper returns
``{"status": "pending_approval", ...}`` and the body is never executed.
Every confirmed execution is deduplicated on ``idempotency_key`` and audited
to ``event_log`` by the ``@governed`` decorator.

Dependency rule (uncle-bob inward): this module imports from ``nce.autonomy``,
``nce.db_utils``, ``nce.event_log``, and other ``nce.vertical_modules.procurement``
sub-modules only.  No web / HTTP / admin / MCP imports.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.config import cfg
from nce.event_log import append_event
from nce.vertical_modules.procurement.bids import do_resolve_bids
from nce.vertical_modules.procurement.graph import upsert_po_node
from nce.vertical_modules.procurement.po_line import (
    POLineStatus,
    update_po_line_status,
    upsert_po_line_node,
)
from nce.vertical_modules.procurement.ranking import do_rank_suppliers
from nce.vertical_modules.procurement.transports import (
    NetsetPoTransport,
    PoTransport,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.procurement.po")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_AGENT_ID = "procurement.submit_po"
_ACTION_TYPE_SUBMIT = "submit_po"

# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------


def _derive_po_idempotency_key(
    namespace_id: str,
    artnrs: list[str],
    weights_hash: str,
) -> str:
    """Stable idempotency key for a generate-PO call.

    Hash of ``(namespace_id, sorted(artnrs), weights_hash)`` — the same
    inputs always produce the same key, preventing duplicate draft PO nodes
    on replay.

    Parameters
    ----------
    namespace_id:
        Tenant namespace UUID string.
    artnrs:
        Article numbers being sourced (sorted for stability).
    weights_hash:
        Short hash or version tag of the procurement weights in use.
    """
    payload = json.dumps(
        {
            "namespace_id": namespace_id,
            "artnrs": sorted(artnrs),
            "weights_hash": weights_hash,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Core governed Actor
# ---------------------------------------------------------------------------


@governed(action_type="generate_po")
async def do_generate_po(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    engine: NCEEngine,
    po_number: str,
    bom_line: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    weights: dict[str, Any] | None = None,
    artnrs: list[str] | None = None,
    source_id: str | None = None,
    line_items: list[dict[str, Any]] | None = None,
    bom_line_ref: str | None = None,
) -> dict[str, Any]:
    """Create a draft PO node and PO_LINE nodes via the C2 ``@governed`` gate.

    Orchestration order
    -------------------
    1. Rank suppliers (Wave 2 ``do_rank_suppliers``) — pure, no DB.
    2. Resolve best BID prices (Wave 5 ``do_resolve_bids``) — reads cache.
    3. Upsert a draft ``PO`` node (Wave 6 ``upsert_po_node``) — writes graph.
    4. Upsert draft ``PO_LINE`` nodes (Wave PR-1) — writes kg_nodes, kg_edges,
       and ``procurement_po_lines`` with status=DRAFT.

    No transport is called here.  ``do_submit_po`` (Wave 11) selects the
    ``PoTransport`` adapter and places the external order.

    The ``@governed`` decorator enforces:
      1. Confirm-only default — body never runs without ``confirm=True``.
      2. Non-empty ``idempotency_key`` required.
      3. Dedup — same key → ``already_executed`` NO-OP on replay.
      4. ``event_log`` audit on first confirmed execution.

    Parameters
    ----------
    conn:
        asyncpg connection inside an active transaction (``scoped_pg_session``).
    namespace_id:
        Tenant UUID — all writes are scoped to this namespace.
    idempotency_key:
        Stable hash of the call inputs.  Derive via
        ``_derive_po_idempotency_key`` from the MCP/caller layer.
    confirm:
        ``False`` (default) → governed returns ``pending_approval`` without
        calling this body.  ``True`` → executes once.
    engine:
        Live NCEEngine instance — passed to ``do_resolve_bids`` which opens
        its own ``scoped_pg_session`` from ``engine.pg_pool``.
    po_number:
        Purchase order number for the draft PO node label ``PO:<PO_NUMBER>``.
    bom_line:
        BOM line dict (required by Wave 2 ranking).  Must contain
        ``quantity`` at minimum.
    candidates:
        Supplier candidate list (required by Wave 2 ranking).
        Each must contain ``unit_price`` (float).
    weights:
        Full procurement weights dict from ``procurement-weights.json``
        (contains ``TCO_WEIGHTS`` and ``SCORING_WEIGHTS``).
    artnrs:
        Article numbers to resolve from the BID cache (Wave 5).
    source_id:
        Optional procurement source record ID for graph provenance.
    line_items:
        Optional list of line item dicts to upsert as PO_LINE nodes.
    bom_line_ref:
        Optional BOM line label reference for single-line generation.

    Returns
    -------
    dict with:
      ``po_number``     — the PO number used for the draft node.
      ``po_label``      — the kg_nodes label (``PO:<PO_NUMBER>``).
      ``ranked_winner`` — dict of the top-ranked supplier.
      ``bid_results``   — list of best-BID rows resolved by Wave 5.
      ``rebate_override`` — bool from Wave 2 ranking.
      ``po_lines``      — list of created PO_LINE dicts.
    """
    ns_str = str(namespace_id)

    effective_bom_line = dict(bom_line) if bom_line else {"quantity": 1, "unit_price": 0.0}
    effective_candidates = (
        list(candidates)
        if candidates
        else [
            {
                "supplier_id": "DEFAULT",
                "unit_price": float(effective_bom_line.get("unit_price") or 0.0),
                "delivery_reliability": 1.0,
                "supplier_tier": 1,
                "own_stock": True,
            }
        ]
    )
    effective_artnrs = list(artnrs) if artnrs else []
    effective_weights = weights or {
        "TCO_WEIGHTS": {"freight": 0.05, "warranty": 0.03, "stock": 0.02, "delivery_risk": 0.05},
        "SCORING_WEIGHTS": {
            "tco": 0.30,
            "delivery_reliability": 0.20,
            "bid_price": 0.20,
            "tier_bundling": 0.15,
            "rebate_proximity": 0.15,
        },
    }

    # ------------------------------------------------------------------
    # Step 1: Rank suppliers (Wave 2 — pure, no I/O)
    # ------------------------------------------------------------------
    ranking = do_rank_suppliers(effective_weights, effective_bom_line, effective_candidates)
    ranked_winner: dict[str, Any] = ranking["ranked"][0]
    log.info(
        "[generate-po] ranking done: winner=%s rebate_override=%s ns=%s",
        ranked_winner.get("supplier_id", "?"),
        ranking["rebate_override"],
        ns_str[:8],
    )

    # ------------------------------------------------------------------
    # Step 2: Resolve best BID prices (Wave 5 — reads procurement_bid_prices)
    # ------------------------------------------------------------------
    bid_result = await do_resolve_bids(
        engine,
        {"namespace_id": ns_str, "artnrs": effective_artnrs},
    )
    bid_results: list[dict[str, Any]] = bid_result.get("results", [])
    log.info(
        "[generate-po] BID resolve done: %d artnr(s) resolved ns=%s",
        len(bid_results),
        ns_str[:8],
    )

    # ------------------------------------------------------------------
    # Step 3: Upsert draft PO node (Wave 6 — writes kg_nodes via ownership guard)
    # ------------------------------------------------------------------
    await upsert_po_node(
        conn,
        namespace_id,
        po_number=po_number,
        source_id=source_id,
    )
    po_label = f"PO:{po_number.upper()}"
    log.info(
        "[generate-po] draft PO node upserted: label=%s ns=%s",
        po_label,
        ns_str[:8],
    )

    # ------------------------------------------------------------------
    # Step 4: Upsert draft PO_LINE nodes (Wave PR-1 — status=DRAFT)
    # ------------------------------------------------------------------
    po_lines: list[dict[str, Any]] = []
    items_to_create = (
        line_items
        if line_items is not None
        else [
            {
                "line_ref": 1,
                "bom_line_ref": bom_line_ref
                or effective_bom_line.get("bom_line_ref")
                or effective_bom_line.get("id"),
                "sku": effective_bom_line.get("artnr")
                or effective_bom_line.get("sku")
                or (effective_artnrs[0] if effective_artnrs else None),
                "qty": effective_bom_line.get("quantity") or effective_bom_line.get("qty") or 1,
                "unit_price": effective_bom_line.get("unit_price") or 0.0,
                "total_amount": float(
                    effective_bom_line.get("quantity") or effective_bom_line.get("qty") or 1
                )
                * float(effective_bom_line.get("unit_price") or 0.0),
            }
        ]
    )
    for idx, item in enumerate(items_to_create, 1):
        l_ref = str(item.get("line_ref", idx))
        b_ref = item.get("bom_line_ref") or bom_line_ref
        sku = item.get("sku") or item.get("artnr")
        qty = item.get("qty") or item.get("quantity") or 1
        u_price = float(item.get("unit_price") or 0.0)
        tot = float(item.get("total_amount", float(qty) * u_price))
        created_line = await upsert_po_line_node(
            conn,
            namespace_id,
            po_number=po_number,
            line_ref=l_ref,
            bom_line_label=b_ref,
            artnr=sku,
            quantity=float(qty),
            unit_price=u_price,
            line_total=tot,
            status=POLineStatus.DRAFT,
            source_id=source_id,
        )
        po_lines.append(created_line)

    return {
        "po_number": po_number,
        "po_label": po_label,
        "ranked_winner": ranked_winner,
        "bid_results": bid_results,
        "rebate_override": ranking["rebate_override"],
        "po_lines": po_lines,
    }


# ---------------------------------------------------------------------------
# Wave 11 — do_submit_po helpers
# ---------------------------------------------------------------------------


def _derive_submit_idempotency_key(namespace_id: str, po_number: str) -> str:
    """Stable idempotency key for a submit-PO call.

    Derived from ``(namespace_id, po_number)`` so that any retry of the same
    PO in the same namespace produces the same key and is deduplicated by the
    ``@governed`` decorator — never a second order.

    Parameters
    ----------
    namespace_id:
        Tenant namespace UUID string.
    po_number:
        The PO number uniquely identifying the order.
    """
    payload = json.dumps(
        {"namespace_id": namespace_id, "po_number": po_number},
        separators=(",", ":"),
        sort_keys=True,
    )
    return "submit:" + hashlib.sha256(payload.encode()).hexdigest()


async def _call_agreements_compliance_audit(
    a2a_client: Any | None,
    po_number: str,
    supplier_id: str,
    rebate_amount: float,
    namespace_id: str,
    *,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Call Agreements Module 3 compliance-audit tool via engine.modules or A2A.

    Returns ``{"approved": True, ...}`` when Agreements accepts the rebate.
    Raises ``Exception`` on any error (unavailability, refusal, timeout) so
    the caller's fail-closed path triggers.

    Parameters
    ----------
    a2a_client:
        Optional A2A client with an async ``call_tool(tool_name, params)`` method.
    po_number, supplier_id, rebate_amount, namespace_id:
        PO identity and rebate details forwarded to the Agreements tool.
    engine:
        Optional NCEEngine instance providing ``engine.modules["agreements"]``.
    """
    # 1. Prefer in-process cross-engine wiring via engine.modules (Wave AG-3 / PR-5)
    if engine is not None and getattr(engine, "modules", None) is not None:
        from nce.engine_registry import EngineDisabledError, EngineUnavailableError

        try:
            scoped = (
                engine.modules.for_namespace(namespace_id)
                if hasattr(engine.modules, "for_namespace")
                else engine.modules
            )
            agreements_mod = scoped["agreements"]
        except EngineDisabledError as exc:
            log.warning(
                "[submit-po] Agreements engine disabled for namespace %s: %s",
                namespace_id,
                exc,
            )
            try:
                from nce.degradation import record_degradation

                record_degradation(
                    namespace_id=str(namespace_id),
                    engine="procurement",
                    code="agreements_module_disabled",
                    detail=f"Agreements engine is disabled for namespace {namespace_id}; rebate override audit cannot proceed.",
                    onboarding_hint="Enable the Agreements module for this namespace or remove rebate_override flag.",
                )
            except Exception:
                pass
            raise ValueError(
                f"Agreements compliance-audit failed closed: Agreements engine disabled for namespace {namespace_id}"
            ) from exc
        except (EngineUnavailableError, KeyError) as exc:
            log.warning("[submit-po] Agreements engine not found in registry: %s", exc)
            raise RuntimeError("Agreements engine not found in engine registry") from exc

        audit_fn = getattr(agreements_mod, "do_run_compliance_audit", None)
        if audit_fn is None:
            raise RuntimeError("Agreements module does not export do_run_compliance_audit")

        result = await audit_fn(
            engine,
            {
                "namespace_id": namespace_id,
                "po_number": po_number,
                "supplier_id": supplier_id,
                "rebate_amount": rebate_amount,
            },
        )
        if isinstance(result, str):
            import json

            result = json.loads(result)

        if not result.get("approved"):
            reason = (
                result.get("reason")
                or result.get("detail")
                or result.get("note")
                or "rejected by compliance policy"
            )
            raise ValueError(
                f"Agreements compliance-audit rejected rebate override: po={po_number!r} "
                f"supplier={supplier_id!r} reason={reason!r}"
            )
        return result

    # 2. Fall back to A2A client if provided
    if a2a_client is not None:
        result = await a2a_client.call_tool(
            "agreements.compliance_audit",
            {
                "po_number": po_number,
                "supplier_id": supplier_id,
                "rebate_amount": rebate_amount,
                "namespace_id": namespace_id,
            },
        )
        if isinstance(result, str):
            import json

            result = json.loads(result)
        if not result.get("approved"):
            raise ValueError(
                f"Agreements compliance-audit rejected rebate override: po={po_number!r} "
                f"supplier={supplier_id!r} result={result!r}"
            )
        return result

    # 3. Neither engine.modules nor a2a_client provided -> fail closed
    raise RuntimeError(
        "Agreements cross-engine module or A2A client not provided; cannot audit rebate override."
    )


async def _audit_rebate_decision(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    po_number: str,
    decision: str,
    detail: str,
) -> None:
    """Append a rebate compliance decision to event_log.

    Called for every outcome of the Agreements A2A check (approved / rejected /
    unavailable) so the decision is always ledger-audited regardless of path.

    ``decision`` should be one of ``"approved"``, ``"rejected"``, ``"unavailable"``.
    """
    await append_event(
        conn=conn,
        namespace_id=namespace_id,
        agent_id=_AGENT_ID,
        event_type="config_changed",
        params={
            "actor": _AGENT_ID,
            "changes": {
                "event": "rebate_compliance_audit",
                "po_number": po_number,
                "decision": decision,
                "detail": detail,
            },
        },
    )


# ---------------------------------------------------------------------------
# Wave 11 — submit-PO: inner governed transport executor + outer gate layer
# ---------------------------------------------------------------------------
#
# Two-layer design (uncle-bob SRP):
#
# ``_governed_place_po`` — inner @governed function.
#   Owns: confirm-only default, value ceiling, kill-switch, idempotency dedup,
#         event_log audit, transport call.  No rebate logic.
#
# ``do_submit_po`` — outer gate function (public API, same signature).
#   Owns: rebate_override → Agreements A2A compliance gate (fail-closed).
#   Delegates to ``_governed_place_po`` once the rebate gate passes.
#
# The rebate gate MUST run before ``@governed`` records the idempotency key.
# If rebate fails, the key is NOT burned and ``pending_approval`` is returned
# directly — the transport is never called, no idempotency record is written.
#
# Callers treat ``do_submit_po`` as the single entry point; the inner
# function is a private implementation detail.
# ---------------------------------------------------------------------------


@governed(
    action_type=_ACTION_TYPE_SUBMIT,
    value_arg="po_value",
    value_ceiling=cfg.NCE_PROCUREMENT_AUTONOMY_PO_CEILING,
)
async def _governed_place_po(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    po_number: str,
    supplier_id: str,
    line_items: list[dict[str, Any]],
    po_value: float = 0.0,
    transport: PoTransport | None = None,
    redis_client: Any = None,
) -> dict[str, Any]:
    """Inner governed executor: confirm/ceiling/kill-switch/idempotency + transport.

    Called only by ``do_submit_po`` after the rebate gate passes.
    Do NOT call this directly — use ``do_submit_po``.
    """
    ns_str = str(namespace_id)
    _transport: PoTransport = transport if transport is not None else NetsetPoTransport()

    transport_result = await _transport.place_order(
        po_number,
        supplier_id,
        line_items,
        namespace_id=ns_str,
        idempotency_key=idempotency_key,
    )

    log.info(
        "[submit-po] order placed: po=%s supplier=%s ns=%s transport=%s",
        po_number,
        supplier_id,
        ns_str[:8],
        type(_transport).__name__,
    )

    # Transition PO_LINE nodes to ORDERED (Wave PR-1)
    ns_uuid = namespace_id if isinstance(namespace_id, UUID) else UUID(ns_str)
    rows = await conn.fetch(
        """
        SELECT line_ref, project_id, bom_line_label
        FROM procurement_po_lines
        WHERE namespace_id = $1 AND po_number = $2
        ORDER BY line_ref ASC
        """,
        ns_uuid,
        po_number,
    )
    if not rows and line_items:
        for idx, item in enumerate(line_items, 1):
            l_ref = str(item.get("line_ref", idx))
            b_ref = item.get("bom_line_ref") or item.get("bom_line_label")
            sku = item.get("sku") or item.get("artnr")
            qty = item.get("qty") or item.get("quantity") or 1
            u_price = float(item.get("unit_price") or 0.0)
            await upsert_po_line_node(
                conn,
                namespace_id,
                po_number=po_number,
                line_ref=l_ref,
                bom_line_label=b_ref,
                artnr=sku,
                quantity=float(qty),
                unit_price=u_price,
                line_total=float(qty) * u_price,
                status=POLineStatus.DRAFT,
            )
        rows = await conn.fetch(
            """
            SELECT line_ref, project_id, bom_line_label
            FROM procurement_po_lines
            WHERE namespace_id = $1 AND po_number = $2
            ORDER BY line_ref ASC
            """,
            ns_uuid,
            po_number,
        )

    ordered_lines: list[dict[str, Any]] = []
    for r in rows:
        updated = await update_po_line_status(
            conn,
            namespace_id,
            po_number=po_number,
            line_ref=r["line_ref"],
            new_status=POLineStatus.ORDERED,
            project_id=r["project_id"],
            bom_line_label=r["bom_line_label"],
            project_value=po_value,
        )
        ordered_lines.append(updated)

    return {
        "status": "submitted",
        "po_number": po_number,
        "supplier_id": supplier_id,
        "transport_result": transport_result,
        "ordered_lines": ordered_lines,
    }


async def do_submit_po(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    po_number: str,
    supplier_id: str,
    line_items: list[dict[str, Any]],
    po_value: float = 0.0,
    rebate_override: bool = False,
    rebate_amount: float = 0.0,
    transport: PoTransport | None = None,
    a2a_client: Any | None = None,
    redis_client: Any = None,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Submit a draft PO through the C2 autonomy gate (Wave 11 — sharpest blast radius).

    Gate order (all must pass for a real order to be placed):

    1. **Confirm-only default** — ``_governed_place_po`` returns
       ``pending_approval`` when ``confirm=False``; body never runs.
    2. **Value ceiling** — ``po_value > NCE_PROCUREMENT_AUTONOMY_PO_CEILING``
       trips ``_governed_place_po``'s policy gate → ``pending_approval``.
       Ceiling defaults to 0 (everything requires human-confirm).
    3. **rebate_override gate** — when ``rebate_override=True`` (flag from Wave 2
       ranking), Agreements compliance-audit is called via engine.modules (Wave AG-3 / PR-5)
       or A2A **before** ``_governed_place_po`` records the idempotency key.  Fail-closed:
       if neither ``engine.modules`` nor ``a2a_client`` is provided, the call errors, or Agreements rejects
       → ``pending_approval`` is returned immediately; no key is burned, no
       transport is called.  Every decision (approved/rejected/unavailable)
       is appended to ``event_log``.
    4. **Kill-switch** — ``_governed_place_po`` checks ``nce:tools:disabled``;
       fail-closed when Redis is unreachable.
    5. **Idempotency** — ``_governed_place_po`` records the idempotency key in
       ``action_idempotency`` before calling the transport; a retry with the same
       key returns ``already_executed`` — the transport is NEVER called twice.
    6. **No real auto-submit at launch** — ``transport`` defaults to
       ``NetsetPoTransport`` (🔴 stub, always raises ``NotImplementedError``),
       so a real external order is impossible by construction.

    Parameters
    ----------
    conn:
        asyncpg connection inside an active transaction (``scoped_pg_session``).
    namespace_id:
        Tenant UUID — all writes are scoped to this namespace.
    idempotency_key:
        Stable key for this submit call; derive via
        ``_derive_submit_idempotency_key(str(namespace_id), po_number)``.
    confirm:
        ``False`` (default) → ``pending_approval`` (confirm-only default).
        ``True`` → attempts execution if all gates pass.
    po_number:
        PO number identifying the draft PO node (``PO:<PO_NUMBER>`` label).
    supplier_id:
        Supplier identifier forwarded to the transport.
    line_items:
        Order line items; each entry must have ``artnr`` + ``quantity``.
    po_value:
        Monetary value of the PO checked against the ceiling gate.
    rebate_override:
        ``True`` (from Wave 2 ranking) triggers Agreements compliance audit.
    rebate_amount:
        Rebate amount forwarded to the Agreements compliance check.
    transport:
        ``PoTransport`` adapter; defaults to ``NetsetPoTransport`` (🔴 stub).
    a2a_client:
        Optional A2A client with ``call_tool(tool_name, params)`` for reaching Agreements.
    redis_client:
        Redis client for the kill-switch gate inside ``_governed_place_po``.
    engine:
        Optional NCEEngine instance providing ``engine.modules["agreements"]``.

    Returns
    -------
    Status dict. Possible shapes:
    - ``{"status": "pending_approval", "action_type": "submit_po", ...}`` —
      no confirm, over-ceiling, or rebate gate blocked.
    - ``{"status": "executed", "result": {"status": "submitted", ...}, ...}`` —
      first confirmed execution (all gates passed).
    - ``{"status": "already_executed", "idempotency_key": ...}`` —
      retry of a previously executed key (NO-OP).
    """
    ns_str = str(namespace_id)

    # ------------------------------------------------------------------
    # Gate: rebate_override → Agreements compliance-audit (fail-closed)
    #
    # This gate runs BEFORE _governed_place_po so that:
    #   a) A failed rebate check does NOT burn the idempotency key.
    #   b) The transport is NEVER called when Agreements is unavailable.
    #
    # The check only applies when confirm=True (no point auditing a dry-run).
    # ------------------------------------------------------------------
    if rebate_override and confirm:
        try:
            has_engine_modules = engine is not None and getattr(engine, "modules", None) is not None
            if a2a_client is None and not has_engine_modules:
                raise RuntimeError(
                    "Agreements cross-engine module or A2A client not provided; cannot audit rebate override."
                )
            await _call_agreements_compliance_audit(
                a2a_client,
                po_number=po_number,
                supplier_id=supplier_id,
                rebate_amount=rebate_amount,
                namespace_id=ns_str,
                engine=engine,
            )
            # Approved — audit the pass decision before delegating to the governed executor.
            await _audit_rebate_decision(
                conn,
                namespace_id,
                po_number=po_number,
                decision="approved",
                detail="Agreements compliance-audit approved rebate override.",
            )
            log.info(
                "[submit-po] rebate compliance approved: po=%s supplier=%s ns=%s",
                po_number,
                supplier_id,
                ns_str[:8],
            )
        except Exception as exc:
            # Fail-closed: unavailable OR rejected → human-confirm.
            detail = str(exc)
            decision = "rejected" if "rejected" in detail.lower() else "unavailable"
            await _audit_rebate_decision(
                conn,
                namespace_id,
                po_number=po_number,
                decision=decision,
                detail=detail,
            )
            log.warning(
                "[submit-po] rebate compliance FAIL-CLOSED (%s): po=%s ns=%s exc=%s",
                decision,
                po_number,
                ns_str[:8],
                exc,
            )
            return {
                "status": "pending_approval",
                "reason": f"rebate_override compliance check failed ({decision}): {detail}",
                "po_number": po_number,
                "idempotency_key": idempotency_key,
                "action_type": _ACTION_TYPE_SUBMIT,
            }

    # ------------------------------------------------------------------
    # Delegate to the inner @governed executor.
    # All remaining gates (confirm-only, ceiling, kill-switch, idempotency,
    # transport) are enforced inside _governed_place_po.
    # ------------------------------------------------------------------
    return await _governed_place_po(
        conn,
        namespace_id,
        idempotency_key=idempotency_key,
        confirm=confirm,
        po_number=po_number,
        supplier_id=supplier_id,
        line_items=line_items,
        po_value=po_value,
        transport=transport,
        redis_client=redis_client,
    )
