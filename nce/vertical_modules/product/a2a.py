"""
nce/vertical_modules/product/a2a.py
=====================================
A2A (Agent-to-Agent) fire-and-forget enrichment trigger — Module 2.Wave 8.

``enqueue_product_enrichment`` enqueues the W7 ``do_enrich_product`` coroutine
on the existing ``create_tracked_task`` background-task path and returns
**immediately** with the known state plus ``enrichment: "queued"``.

The caller (quote builder / design builder) is NEVER blocked on OCR or feed
round-trips.  A salesperson adding a line sees ``specs_pending`` instantly;
the enrichment fills the log asynchronously.

Fire-and-forget contract (uncle-bob SRP + dependency-inward):
  - This module imports from ``nce.background_task_manager``,
    ``nce.db_utils``, ``nce.vertical_modules.product.enrich``, and stdlib only.
  - No web / admin / HTTP modules.
  - No awaiting enrichment completion in the caller path.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.background_task_manager import create_tracked_task
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.product.enrich import _derive_idempotency_key, do_enrich_product

log = logging.getLogger("nce.vertical_modules.product.a2a")

# Stable low-cardinality task name for background-task metrics.
_TASK_NAME = "product_enrich"


async def _run_enrichment(
    pg_pool: asyncpg.Pool,
    namespace_id: str,
    product_id: str,
    trigger_context: dict[str, Any],
    idem_key: str,
) -> None:
    """Execute do_enrich_product inside a scoped session (background task body).

    This runs in a ``create_tracked_task`` background task — exceptions are
    captured by the task manager's done-callback and routed to the monitoring
    layer, so failures are never silent.
    """
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await do_enrich_product(
            conn,
            uuid.UUID(namespace_id),
            idempotency_key=idem_key,
            confirm=True,
            product_id=product_id,
            trigger_context=trigger_context,
        )


def enqueue_product_enrichment(
    pg_pool: asyncpg.Pool,
    namespace_id: str,
    product_id: str,
    trigger_context: dict[str, Any],
) -> dict[str, Any]:
    """Fire-and-forget: enqueue ``do_enrich_product`` and return immediately.

    The function is **synchronous** at the call site — it uses
    ``create_tracked_task`` (which calls ``asyncio.create_task``) and returns
    without awaiting enrichment completion.  This guarantees the quote/design
    builder is never blocked on enrichment.

    Parameters
    ----------
    pg_pool:
        The application's asyncpg connection pool.
    namespace_id:
        Tenant UUID string — all enrichment writes are scoped to this namespace.
    product_id:
        UUID string of the single product to enrich.
    trigger_context:
        ``{kind, ref_id, missing_fields, source_watermark}`` — forwarded to
        ``do_enrich_product`` unchanged.

    Returns
    -------
    dict with ``product_id``, ``enrichment`` (``"queued"``), and
    ``specs_pending`` (True) — the caller can use these to set UI state
    without waiting for the background job.
    """
    missing_fields: list[str] = list(trigger_context.get("missing_fields") or [])
    source_watermark: str = str(trigger_context.get("source_watermark") or "")
    idem_key = _derive_idempotency_key(product_id, missing_fields, source_watermark)

    create_tracked_task(
        _run_enrichment(pg_pool, namespace_id, product_id, trigger_context, idem_key),
        name=_TASK_NAME,
    )

    log.info(
        "[a2a] product_enrichment enqueued: ns=%s product=%s fields=%d",
        namespace_id[:8],
        product_id[:8],
        len(missing_fields),
    )

    return {
        "product_id": product_id,
        "enrichment": "queued",
        "specs_pending": True,
    }
