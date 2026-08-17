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

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.config import cfg
from nce.event_log import append_event
from nce.vertical_modules.procurement.bids import do_resolve_bids
from nce.vertical_modules.procurement.graph import upsert_po_node
from nce.vertical_modules.procurement.ranking import do_rank_suppliers
from nce.vertical_modules.procurement.transports import NetsetPoTransport, PoTransport

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
    bom_line: dict[str, Any],
    candidates: list[dict[str, Any]],
    weights: dict[str, Any],
    artnrs: list[str],
    source_id: str | None = None,
) -> dict[str, Any]:
    """Create a draft PO node via the C2 ``@governed`` gate.

    Orchestration order
    -------------------
    1. Rank suppliers (Wave 2 ``do_rank_suppliers``) — pure, no DB.
    2. Resolve best BID prices (Wave 5 ``do_resolve_bids``) — reads cache.
    3. Upsert a draft ``PO`` node (Wave 6 ``upsert_po_node``) — writes graph.

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

    Returns
    -------
    dict with:
      ``po_number``     — the PO number used for the draft node.
      ``po_label``      — the kg_nodes label (``PO:<PO_NUMBER>``).
      ``ranked_winner`` — dict of the top-ranked supplier.
      ``bid_results``   — list of best-BID rows resolved by Wave 5.
      ``rebate_override`` — bool from Wave 2 ranking.
    """
    ns_str = str(namespace_id)

    # ------------------------------------------------------------------
    # Step 1: Rank suppliers (Wave 2 — pure, no I/O)
    # ------------------------------------------------------------------
    ranking = do_rank_suppliers(weights, bom_line, candidates)
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
        {"namespace_id": ns_str, "artnrs": artnrs},
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

    return {
        "po_number": po_number,
        "po_label": po_label,
        "ranked_winner": ranked_winner,
        "bid_results": bid_results,
        "rebate_override": ranking["rebate_override"],
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
    a2a_client: Any,
    po_number: str,
    supplier_id: str,
    rebate_amount: float,
    namespace_id: str,
) -> dict[str, Any]:
    """Call Agreements Module 3 compliance-audit tool via A2A.

    Returns ``{"approved": True, ...}`` when Agreements accepts the rebate.
    Raises ``Exception`` on any error (unavailability, refusal, timeout) so
    the caller's fail-closed path triggers.

    Since Agreements (Module 3) is not yet built, this call will always fail
    in production — the fail-closed path (human-confirm) correctly fires.

    Parameters
    ----------
    a2a_client:
        An A2A client with an async ``call_tool(tool_name, params)`` method.
        The client must implement the Agreements compliance-audit tool interface.
    po_number, supplier_id, rebate_amount, namespace_id:
        PO identity and rebate details forwarded to the Agreements tool.
    """
    result: dict[str, Any] = await a2a_client.call_tool(
        "agreements.compliance_audit",
        {
            "po_number": po_number,
            "supplier_id": supplier_id,
            "rebate_amount": rebate_amount,
            "namespace_id": namespace_id,
        },
    )
    if not result.get("approved"):
        raise ValueError(
            f"Agreements compliance-audit rejected rebate override: po={po_number!r} "
            f"supplier={supplier_id!r} result={result!r}"
        )
    return result


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

    return {
        "status": "submitted",
        "po_number": po_number,
        "supplier_id": supplier_id,
        "transport_result": transport_result,
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
) -> dict[str, Any]:
    """Submit a draft PO through the C2 autonomy gate (Wave 11 — sharpest blast radius).

    Gate order (all must pass for a real order to be placed):

    1. **Confirm-only default** — ``_governed_place_po`` returns
       ``pending_approval`` when ``confirm=False``; body never runs.
    2. **Value ceiling** — ``po_value > NCE_PROCUREMENT_AUTONOMY_PO_CEILING``
       trips ``_governed_place_po``'s policy gate → ``pending_approval``.
       Ceiling defaults to 0 (everything requires human-confirm).
    3. **rebate_override gate** — when ``rebate_override=True`` (flag from Wave 2
       ranking), Agreements compliance-audit is called via A2A **before**
       ``_governed_place_po`` records the idempotency key.  Fail-closed:
       if ``a2a_client`` is ``None``, the call errors, or Agreements rejects
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
        ``True`` (from Wave 2 ranking) triggers Agreements A2A compliance audit.
    rebate_amount:
        Rebate amount forwarded to the Agreements compliance check.
    transport:
        ``PoTransport`` adapter; defaults to ``NetsetPoTransport`` (🔴 stub).
    a2a_client:
        A2A client with ``call_tool(tool_name, params)`` for reaching Agreements.
        ``None`` → rebate gate fails closed immediately.
    redis_client:
        Redis client for the kill-switch gate inside ``_governed_place_po``.

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
            if a2a_client is None:
                raise RuntimeError(
                    "Agreements A2A client not provided; cannot audit rebate override."
                )
            await _call_agreements_compliance_audit(
                a2a_client,
                po_number=po_number,
                supplier_id=supplier_id,
                rebate_amount=rebate_amount,
                namespace_id=ns_str,
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
