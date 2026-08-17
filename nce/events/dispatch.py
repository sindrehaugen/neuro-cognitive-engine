"""nce.events.dispatch — idempotent subscriber dispatch (C4 §9.6).

Responsibilities
----------------
- ``dispatch_once`` drains the outbox and routes events to registered
  handlers by delegating entirely to ``run_outbox_relay_once``.  It does
  **not** add a second polling loop, a second DLQ, or duplicate retry
  logic — all of that belongs to the relay.
- ``make_idempotent_handler`` (**deprecated**, see below) wraps a handler
  with a SAVEPOINT-guarded dedup check against ``processed_outbox_events``.

Do NOT wrap handlers registered via ``subscribe``
--------------------------------------------------
An earlier version of this docstring instructed the opposite.  That
instruction was wrong and silently disabled every handler that followed it:
``outbox_relay.deliver_one`` INSERTs the ``event_id`` into
``processed_outbox_events`` *before* invoking any handler, so a wrapper that
checks the same table for the same key in the same transaction always
observes its own row, concludes "duplicate", and returns ``None`` without
ever calling the business function.

The relay's dedup is the one and only idempotency layer.  Register plain
handlers; ``deliver_one`` guarantees they run at most once per ``event_id``
and at least once overall.  ``make_idempotent_handler`` is retained only for
callers that invoke a handler outside the relay (there are none in
production) and is slated for removal.

Design invariants (uncle-bob-craft / C4 §9.6)
----------------------------------------------
- SRP: each function does exactly one thing.
- Dependencies point inward: only ``asyncpg`` and ``nce`` internals are
  imported.  No web adapters, admin modules, or Django.
- The relay (``nce.outbox_relay``) owns poll/mark_published/retry/DLQ and
  the dedup guard.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.events.emit import mark_graph_write_processed
from nce.outbox_relay import OutboxHandler, PostCommitAction, run_outbox_relay_once

log = logging.getLogger("nce.events.dispatch")


def make_idempotent_handler(
    business_fn: Callable[
        [asyncpg.Connection, dict[str, Any]],
        Awaitable[PostCommitAction | None],
    ],
) -> OutboxHandler:
    """Wrap *business_fn* so duplicate deliveries are silent no-ops.

    .. deprecated::
       **Never pass the result to ``subscribe``.**  ``outbox_relay.deliver_one``
       already inserts the dedup row before calling handlers, so under the relay
       this wrapper always sees its own row and ``business_fn`` never runs.  Only
       use it when invoking a handler outside the relay.

    The wrapper checks ``processed_outbox_events`` before calling the
    business logic.  If ``event_id`` is already recorded the handler
    returns ``None`` immediately — no side-effect fires a second time.

    Parameters
    ----------
    business_fn:
        The real handler implementing the domain side-effect.  Must be an
        async callable matching ``OutboxHandler``.

    Returns
    -------
    OutboxHandler
        A new async callable with the same signature.
    """

    async def _idempotent(
        conn: asyncpg.Connection,
        event: dict[str, Any],
    ) -> PostCommitAction | None:
        raw_id = event.get("id")
        if raw_id is None:
            raise ValueError(
                "[dispatch] event missing required 'id' field — cannot enforce idempotency"
            )

        event_id: uuid.UUID = raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(str(raw_id))
        namespace_id = event.get("namespace_id")

        # SAVEPOINT pattern — satisfies BOTH invariants simultaneously:
        #
        # (a) NO DOUBLE-FIRE: the INSERT ... ON CONFLICT DO NOTHING inside
        #     mark_graph_write_processed returns is_new=False for a real
        #     duplicate → we return None immediately, business_fn never runs.
        #
        # (b) AT-LEAST-ONCE: if business_fn raises, the nested transaction
        #     (asyncpg SAVEPOINT) rolls back — undoing the dedup INSERT — and
        #     re-raises to the relay's outer except-block.  The relay calls
        #     mark_failed on the still-valid outer transaction and commits;
        #     because the dedup mark was rolled back the next delivery will
        #     find is_new=True and retry business_fn.
        #
        # If business_fn succeeds the savepoint commits together with the
        # outer relay transaction — dedup mark and mark_published land
        # atomically.
        async with conn.transaction():  # creates a SAVEPOINT when nested
            is_new = await mark_graph_write_processed(
                conn,
                event_id=event_id,
                namespace_id=namespace_id,
            )
            if not is_new:
                log.debug(
                    "[dispatch] duplicate delivery skipped event_id=%s event_type=%s",
                    event_id,
                    event.get("event_type"),
                )
                return None

            # business_fn runs inside the savepoint.  Any exception here
            # causes asyncpg to ROLLBACK TO SAVEPOINT, undoing the dedup
            # INSERT, and then re-raises — preserving at-least-once delivery.
            return await business_fn(conn, event)

    return _idempotent


async def dispatch_once(pool: asyncpg.Pool, *, batch_size: int = 50) -> int:
    """Drain the outbox and dispatch each event to its registered handler.

    Delegates entirely to ``run_outbox_relay_once`` — no additional polling
    loop, DLQ, or retry logic is written here.  Handlers registered via
    ``subscribe`` fire at most once per ``event_id``; the relay's dedup row
    provides that guarantee, so handlers must be registered unwrapped.

    Parameters
    ----------
    pool:       Live asyncpg connection pool.
    batch_size: Maximum events to drain per call (forwarded to the relay).

    Returns
    -------
    int
        Number of events successfully delivered in this pass.
    """
    return await run_outbox_relay_once(pool, batch_size=batch_size)
