"""
nce/read_model_subscribers.py
==============================
Outbox subscribers for downstream read-model refreshers (Wave B-R2).

Registers runtime outbox handlers for the 5 <TYPE>.upserted families that
Business Insights (BI) and Copper actually read:
- PRODUCT_SKU.upserted
- QUOTE.upserted
- INVOICE.upserted
- ASSET.upserted
- BOM_LINE.upserted

These handlers acknowledge delivery (returning None without external I/O)
so the transactional outbox relay marks the rows published rather than
dead-lettering.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.events.bus import subscribe

log = logging.getLogger("nce.read_model_subscribers")

_READ_MODEL_NODE_TYPES: tuple[str, ...] = (
    "PRODUCT_SKU",
    "QUOTE",
    "INVOICE",
    "ASSET",
    "BOM_LINE",
)

_UPSERTED_OP: str = "upserted"


async def handle_read_model_upserted(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    event: dict[str, Any],
) -> None:
    """Acknowledge one read-model refresh event (<TYPE>.upserted).

    Returning None means 'delivered, no post-commit action'. The relay then
    marks the row published.
    Must not raise.
    """
    log.debug(
        "[read_model] node upserted: type=%s id=%s ns=%s",
        event.get("aggregate_type"),
        event.get("aggregate_id"),
        event.get("namespace_id"),
    )
    return None


def register_read_model_subscribers() -> None:
    """Subscribe the read-model refresh outbox handlers.

    Idempotent: register_handler refuses duplicate registrations by function equality.
    """
    for node_type in _READ_MODEL_NODE_TYPES:
        subscribe(
            {"node_type": node_type, "op": _UPSERTED_OP},
            handle_read_model_upserted,
        )
    log.info(
        "Read model outbox subscribers registered for %d selectors.",
        len(_READ_MODEL_NODE_TYPES),
    )
