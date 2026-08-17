"""nce.events.bus — subscribe/publish interface over the transactional outbox.

Design invariants (uncle-bob-craft / C4 §9.6)
----------------------------------------------
- ``publish``   has one job: insert a row into ``outbox_events``.
- ``subscribe`` has one job: register a handler keyed by selector in the relay registry.
  Registration semantics (append, idempotent, coroutine-only) belong to the relay's
  ``register_handler``; this module only builds the key.
- No polling loop, no DLQ, no dedup — the relay (``nce.outbox_relay``) owns all of that.
- Dependencies point inward: this module imports only ``asyncpg`` (stdlib types) and
  ``nce.outbox_relay.register_handler`` — never web adapters, admin modules, or Django.
- Post-commit delivery semantics are preserved: the relay fires handlers *after* the
  caller's transaction commits; ``publish`` only inserts the row.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.outbox_relay import (
    OutboxDeliveryError,
    OutboxHandler,
    PostCommitAction,
    register_handler,
)

# ``OutboxDeliveryError`` and ``PostCommitAction`` are re-exported deliberately:
# they are the two halves of the subscriber-side contract — raise the first to
# fail a delivery loudly (misconfiguration → DLQ + alert, never a silent
# return), return the second to defer external work past the commit.  A
# subscriber should depend on the bus interface it subscribes through rather
# than reaching into the relay implementation.
__all__ = ["OutboxDeliveryError", "PostCommitAction", "publish", "subscribe"]


def _selector_key(node_type: str, op: str) -> str:
    """Build the ``OUTBOX_HANDLERS`` registry key from a C4 event selector.

    Mirrors the key convention already used in the relay (e.g. ``"memory.stored"``).
    """
    return f"{node_type}.{op}"


async def publish(
    conn: asyncpg.Connection,
    *,
    namespace_id: Any,
    node_type: str,
    op: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> None:
    """Insert one event row into ``outbox_events``.

    The relay picks it up after the surrounding transaction commits and dispatches
    it to any handler registered via ``subscribe``.  Callers must call this inside
    an open transaction so post-commit semantics are preserved.

    Parameters
    ----------
    conn:         Open asyncpg connection (must be inside an active transaction).
    namespace_id: Tenant UUID — written verbatim to ``namespace_id``.
    node_type:    Aggregate / entity type (stored as ``aggregate_type``).
    op:           Operation label (combined with ``node_type`` as ``event_type``).
    aggregate_id: Domain identity of the affected aggregate.
    payload:      Arbitrary JSON-serialisable event body.
    """
    event_type = _selector_key(node_type, op)
    await conn.execute(
        """
        INSERT INTO outbox_events
            (namespace_id, aggregate_type, aggregate_id, event_type, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        namespace_id,
        node_type,
        aggregate_id,
        event_type,
        json.dumps(payload),
    )


def subscribe(selector: dict[str, str], handler: OutboxHandler) -> None:
    """Register *handler* in the relay's ``OUTBOX_HANDLERS`` registry.

    The selector must contain ``"node_type"`` and ``"op"`` keys; the combined key
    ``"{node_type}.{op}"`` is the registry slot the relay queries on delivery.

    A selector may have **many** subscribers: this appends rather than replaces,
    and every subscriber is invoked on delivery.  Re-subscribing the same handler
    object is a no-op, so a double bootstrap does not double-fire.

    Handlers must NOT be wrapped in ``nce.events.dispatch.make_idempotent_handler``
    — the relay already dedups on ``event_id`` before invoking any handler, and a
    second check inside the handler always sees its own conflict.

    A handler that cannot do its job must **raise** (``OutboxDeliveryError`` for a
    misconfiguration such as an unregistered dependency).  Logging an error and
    returning tells the relay the delivery succeeded: the dedup row commits, the
    outbox row is marked published, and the event is unrecoverable.

    Parameters
    ----------
    selector: ``{"node_type": str, "op": str}`` — identifies the event class.
    handler:  Async callable matching ``OutboxHandler`` — receives ``(conn, event)``;
              may return a zero-arg post-commit callable or ``None``.
    """
    node_type = selector["node_type"]
    op = selector["op"]
    key = _selector_key(node_type, op)
    register_handler(key, handler)
