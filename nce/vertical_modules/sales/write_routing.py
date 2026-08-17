"""
nce/vertical_modules/sales/write_routing.py
===========================================
C5 write-routing dispatcher for the Sales vertical module.
Dispatches writes to external (D365) and/or native (NCE) based on source mode.
Enforces prefixing for native IDs and checks mapping existence for D365 record edits.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.orchestrator import NCEEngine
from nce.source_mode import resolve, write_route
from nce.vertical_modules.sales import graph

log = logging.getLogger("nce.vertical_modules.sales.write_routing")

_SALES_ENGINE: str = "sales"


async def do_create_deal(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Create a deal using write routing.

    Mode:
      - d365: external write only.
      - both: external + native write (write-through).
      - nce: native write only.

    In nce mode, enforces nce: prefixing on deal_id, customer_id, and quote_id.
    """
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))

    # Resolve active source mode for this operation
    mode = await resolve(
        engine.pg_pool,
        engine=_SALES_ENGINE,
        function="create_deal",
        namespace_id=ns_uuid,
    )

    deal_id = params.get("deal_id")
    customer_id = params.get("customer_id")
    quote_id = params.get("quote_id")

    if not (deal_id and customer_id and quote_id):
        raise ValueError("deal_id, customer_id, and quote_id are required")

    # 1. Enforce native prefixing in NCE-only mode
    if mode == "nce":
        if not deal_id.startswith("nce:"):
            deal_id = f"nce:{deal_id}"
        if not customer_id.startswith("nce:"):
            customer_id = f"nce:{customer_id}"
        if not quote_id.startswith("nce:"):
            quote_id = f"nce:{quote_id}"

    # 2. Define native writer callback
    async def native_writer() -> dict[str, Any]:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            return await graph.do_create_deal(
                conn,
                ns_uuid,
                deal_id=deal_id,
                customer_id=customer_id,
                quote_id=quote_id,
                opportunity_id=params.get("opportunity_id"),
                lead_id=params.get("lead_id"),
                confidence=params.get("confidence", 1.0),
                source_id=params.get("source_id"),
            )

    # 3. Define external writer callback
    async def external_writer() -> dict[str, Any]:
        # Mock/simulate Dynamics 365 create deal/opportunity write-through
        d365_id = params.get("source_id") or f"d365-deal-{uuid.uuid4().hex[:8]}"
        return {"ok": True, "d365_id": d365_id}

    # 4. Dispatch using resolver write_route
    results = await write_route(
        mode,
        native_writer=native_writer,
        external_writer=external_writer,
    )

    return {
        "ok": True,
        "mode": mode,
        "native": results.get("native"),
        "external": results.get("external"),
    }


async def do_edit_deal(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Edit a deal using write routing.

    Mode:
      - d365: external write only.
      - both: external + native write (write-through).
      - nce: native write only.

    Prevents native edits to D365 records (no nce: prefix) if no mapping
    (existence in NCE kg_nodes) exists.
    """
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))

    mode = await resolve(
        engine.pg_pool,
        engine=_SALES_ENGINE,
        function="edit_deal",
        namespace_id=ns_uuid,
    )

    deal_id = params.get("deal_id")
    if not deal_id:
        raise ValueError("deal_id is required")

    # 1. Collision-proof native edit prevention check
    is_native = deal_id.startswith("nce:")
    if mode in ("both", "nce") and not is_native:
        # If D365 ID, check if a mapping/record already exists natively in NCE
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            deal_lbl = f"DEAL:{deal_id.upper()}"
            exists = await conn.fetchval(
                """
                SELECT COUNT(*) FROM kg_nodes
                WHERE (label = $1 OR d365_source_id = $2)
                  AND namespace_id = $3
                """,
                deal_lbl,
                deal_id,
                ns_uuid,
            )
            if not exists:
                raise ValueError(
                    f"Cannot edit D365 record natively: no mapping exists for ID {deal_id}"
                )

    # 2. Define native writer callback
    async def native_writer() -> dict[str, Any]:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            return await graph.do_edit_deal(
                conn,
                ns_uuid,
                deal_id=deal_id,
                confidence=params.get("confidence"),
                source_id=params.get("source_id"),
            )

    # 3. Define external writer callback
    async def external_writer() -> dict[str, Any]:
        # Mock/simulate Dynamics 365 edit deal write-through
        return {"ok": True, "deal_id": deal_id}

    # 4. Dispatch using resolver write_route
    results = await write_route(
        mode,
        native_writer=native_writer,
        external_writer=external_writer,
    )

    return {
        "ok": True,
        "mode": mode,
        "native": results.get("native"),
        "external": results.get("external"),
    }
