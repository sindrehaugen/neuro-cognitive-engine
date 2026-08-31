"""
nce/vertical_modules/inventory/restock_po.py
==============================================
``do_create_restock_po`` Actor -- Module 11, Wave 9 (``restock-po``), Batch 137.

Turns a Wave 6 (``replenishment.py``) recommendation into a real Procurement
purchase order, through the C2 ``@governed`` autonomy gate, then forwards the
order to Procurement's own ``do_submit_po`` Actor (Module 1, Wave 11).

Compounding autonomy across the Inventory -> Procurement boundary (roadmap
S9.5)
--------------------------------------------------------------------------
An auto-restock retry must never create a duplicate PO request OR a
duplicate order. That is enforced by TWO independent, deterministic
idempotency layers rather than one:

1. **Inventory-side gate** -- this module's own ``@governed`` wrapper records
   ``idempotency_key`` (derived from ``namespace_id``, ``sku``, ``po_number``
   and ``location``) in ``action_idempotency`` before the body ever runs. A
   retry with the same key short-circuits to ``already_executed`` here and
   never reaches ``do_submit_po`` at all -- no duplicate PO *request*.
2. **Procurement-side gate** -- the body derives the submit-po idempotency
   key via Procurement's OWN ``_derive_submit_idempotency_key(namespace_id,
   po_number)`` (the exact function a direct ``do_submit_po`` caller would
   use), instead of inventing a second, unrelated key. Because the key is a
   pure function of ``(namespace_id, po_number)``, any call path that ends up
   submitting the SAME PO number for the SAME tenant -- whether it arrives
   through this Actor or, hypothetically, straight through Procurement --
   collides on the SAME row in ``action_idempotency`` and is deduplicated
   there too -- no duplicate *order*, even if the Inventory-side gate were
   somehow bypassed.

Neither layer is a threshold to widen: both are the existing C2 primitives
from Module 0.W15 (``@governed`` dedup + audit) and Module 0.W16 (ceilings +
kill-switch), applied exactly as written -- this module adds no new gate
logic of its own.

Dependency rule (uncle-bob inward): this module imports from
``nce.autonomy``, ``nce.event_log``, and
``nce.vertical_modules.procurement.po`` (the Actor it calls across the
module boundary) only. No web / HTTP / admin / MCP imports -- this wave
registers no MCP tool and adds no REST route.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.config import cfg
from nce.event_log import append_event
from nce.vertical_modules.procurement.po import (
    _derive_submit_idempotency_key,
    do_submit_po,
)
from nce.vertical_modules.procurement.transports import PoTransport

log = logging.getLogger("nce.vertical_modules.inventory.restock_po")

_AGENT_ID = "inventory.create_restock_po"
_ACTION_TYPE = "create_restock_po"

# Reuse Procurement's PO value ceiling -- a restock PO IS a Procurement PO by
# the time it reaches do_submit_po, so it must never get a laxer autonomy
# ceiling than a manually-created one. This wave's `Files:` excludes
# nce/config.py, so no new config surface is introduced; the ceiling is
# named here for readability at the call site.
AUTONOMY_RESTOCK_CEILING = cfg.NCE_PROCUREMENT_AUTONOMY_PO_CEILING


# ---------------------------------------------------------------------------
# Idempotency key derivation (Inventory-side)
# ---------------------------------------------------------------------------


def _derive_restock_idempotency_key(
    namespace_id: str,
    sku: str,
    po_number: str,
    location: str | None,
) -> str:
    """Stable idempotency key for a create-restock-PO call.

    Hash of ``(namespace_id, sku, po_number, location)`` -- the same restock
    decision always produces the same key, so a retry (e.g. a scheduler
    re-firing the same recommendation) is deduplicated at this Actor's own
    ``@governed`` layer before Procurement is ever called.
    """
    payload = json.dumps(
        {
            "namespace_id": namespace_id,
            "sku": sku,
            "po_number": po_number,
            "location": location,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "restock:" + hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Spanning audit trail
# ---------------------------------------------------------------------------


async def _audit_restock_span(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    sku: str,
    po_number: str,
    restock_idempotency_key: str,
    submit_idempotency_key: str,
    submit_status: str,
) -> None:
    """Append the ONE event_log row correlating the two boundary hops.

    Ties the Inventory-side ``create_restock_po`` idempotency key to the
    Procurement-side ``submit_po`` idempotency key it propagated, so the
    two Actors' own (independent) ``@governed`` audit rows can be joined
    into a single end-to-end trail for this restock decision.
    """
    await append_event(
        conn=conn,
        namespace_id=namespace_id,
        agent_id=_AGENT_ID,
        event_type="config_changed",
        params={
            "actor": _AGENT_ID,
            "changes": {
                "event": "restock_po_span",
                "sku": sku,
                "po_number": po_number,
                "restock_idempotency_key": restock_idempotency_key,
                "submit_idempotency_key": submit_idempotency_key,
                "submit_status": submit_status,
            },
        },
    )


# ---------------------------------------------------------------------------
# Core governed Actor
# ---------------------------------------------------------------------------


@governed(
    action_type=_ACTION_TYPE,
    value_arg="po_value",
    value_ceiling=AUTONOMY_RESTOCK_CEILING,
)
async def do_create_restock_po(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    sku: str,
    po_number: str,
    supplier_id: str,
    line_items: list[dict[str, Any]],
    po_value: float = 0.0,
    location: str | None = None,
    transport: PoTransport | None = None,
    redis_client: Any = None,
) -> dict[str, Any]:
    """Create a restock PO through the C2 gate, then submit it via Procurement.

    Gate order (all enforced by the ``@governed`` decorator above, unchanged
    from Module 0.W15/W16 -- this function adds no gate logic of its own):

    1. **Confirm-only default** -- without ``confirm=True`` returns
       ``{"status": "pending_approval", ...}``; the body below never runs.
    2. **Value ceiling** -- ``po_value > AUTONOMY_RESTOCK_CEILING`` trips the
       policy gate -> ``pending_approval``.
    3. **Kill-switch** -- blocks when ``nce:tools:disabled`` fires for
       ``create_restock_po`` (fail-closed when ``redis_client`` is wired).
    4. **Idempotency** -- the Inventory-side key is recorded in
       ``action_idempotency`` before this body runs; a retry with the same
       key short-circuits to ``already_executed`` and never reaches
       ``do_submit_po``.

    Parameters
    ----------
    conn:
        asyncpg connection inside an active transaction (``scoped_pg_session``).
    namespace_id:
        Tenant UUID -- all writes are scoped to this namespace.
    idempotency_key:
        Stable key for this restock decision; derive via
        ``_derive_restock_idempotency_key(str(namespace_id), sku, po_number, location)``.
    confirm:
        ``False`` (default) -> ``pending_approval``. ``True`` -> attempts
        execution if all gates pass.
    sku:
        Article number being restocked (identity field for the spanning audit).
    po_number:
        PO number forwarded to Procurement's ``do_submit_po``.
    supplier_id, line_items, po_value:
        Forwarded verbatim to ``do_submit_po``.
    location:
        Stock location the restock applies to, if location-scoped (mirrors
        Wave 6's ``do_recommend_restock`` scope parameter). Audit-only here.
    transport, redis_client:
        Forwarded to ``do_submit_po`` -- see that Actor's own docstring.

    Returns
    -------
    Status dict from the ``@governed`` wrapper. On ``"executed"`` the
    ``result`` key holds this function's own return dict, which nests
    Procurement's ``submit_result`` and both boundary idempotency keys.
    """
    ns_str = str(namespace_id)

    # Procurement's OWN derivation, not a second hand-rolled hash: any call
    # that reaches do_submit_po for this (namespace, po_number) -- through
    # this Actor or otherwise -- lands on the SAME key.
    submit_key = _derive_submit_idempotency_key(ns_str, po_number)

    submit_result = await do_submit_po(
        conn,
        namespace_id,
        idempotency_key=submit_key,
        # confirm is always True here: the @governed wrapper above already
        # enforced the confirm-only default for THIS Actor before calling
        # this body, so a real submit attempt is exactly what is intended.
        confirm=True,
        po_number=po_number,
        supplier_id=supplier_id,
        line_items=line_items,
        po_value=po_value,
        transport=transport,
        redis_client=redis_client,
    )

    await _audit_restock_span(
        conn,
        namespace_id,
        sku=sku,
        po_number=po_number,
        restock_idempotency_key=idempotency_key,
        submit_idempotency_key=submit_key,
        submit_status=str(submit_result.get("status", "unknown")),
    )

    log.info(
        "[restock-po] sku=%s po=%s ns=%s submit_status=%s",
        sku,
        po_number,
        ns_str[:8],
        submit_result.get("status"),
    )

    return {
        "sku": sku,
        "po_number": po_number,
        "location": location,
        "submit_result": submit_result,
        "idempotency_key": idempotency_key,
        "submit_idempotency_key": submit_key,
    }
