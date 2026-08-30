"""
nce/vertical_modules/system_design/subscribers.py
=================================================
Outbox subscribers for the System Design vertical module (M6.W13b).

Why this module exists
----------------------
``graph.py`` and ``devices.py`` call ``emit_graph_write`` after every owned-node
write, which publishes one ``<TYPE>.upserted`` row into ``outbox_events``.  The
relay treats an event with **no registered handler** as a hard delivery failure:
``deliver_one`` raises ``OutboxDeliveryError``, the row is stamped
``attempt_count = MAX_OUTBOX_ATTEMPTS`` and copied into ``dead_letter_queue``,
and — the part that compounds — ``published_at`` is never set, so the row also
stays in ``outbox_events`` forever.

Both tables are unretained, and the surviving row stays inside the
``idx_outbox_unpublished`` partial index that **every tenant's** relay poll
reads oldest-first.  So the cost is not confined to System Design: dead rows
degrade the shared relay for the whole deployment.

Until this wave those cores were unreachable from any surface, so the events had
never fired in production.  W13b put them on the wire, which is exactly why the
sink has to land in the same wave.

What these handlers do
----------------------
Nothing but acknowledge.  A design-graph node upsert has no downstream reactive
work defined yet — the read surface queries ``kg_nodes``/``kg_edges`` directly.
The handler exists so the event has a legitimate terminus: the relay marks it
published and moves on.  That is a real behaviour, not a stub — "delivered, no
further action" is a valid outcome, and it is the correct one here.

When a later wave gives a node type real reactive work (W17's removal path is
the first candidate), it subscribes its own handler to the same selector:
``subscribe`` **appends**, so many subscribers may share one selector and all of
them run.  Nothing here has to be unpicked first.

Two mechanical constraints, both learned the hard way
-----------------------------------------------------
1. **The handler is a module-level coroutine, not a closure or a ``partial``.**
   ``register_handler`` dedups with ``if fn in handlers``, i.e. by equality.  A
   freshly-built closure or ``functools.partial`` is a new object every call and
   compares unequal, so a double bootstrap would append it once per call and the
   relay would invoke it N times per event.  One module-level function is the
   only shape that deduplicates.
2. **Registration must be called from every process that runs a relay.**  This
   module being importable is not enough — a ``register_*`` function nobody
   calls is precisely the defect that left ``register_automation_subscribers``
   dead on main.  ``register_system_design_subscribers()`` is invoked from
   ``nce/mcp_stdio_main.py`` (before the relay loop task is created) and from
   ``nce/cron.py`` (at scheduler startup).  Those are the two relay-running
   processes; a third would need the same call.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.events.bus import subscribe

log = logging.getLogger("nce.vertical_modules.system_design.subscribers")

#: Every node type the two authoring cores upsert, and therefore every selector
#: they emit.  ``devices.py`` writes DEVICE / PORT / RACK / CABLE (SIGNAL_CHAIN
#: is declared there but no code path authors one yet); ``graph.py`` writes
#: FUNCTIONAL_LOCATION / DESIGN / DESIGN_LINE.  Adding a node type to either
#: core without adding it here reintroduces the dead-letter leak for that type,
#: which is why ``tests/test_system_design_author_surface.py`` asserts the
#: emitted selector set against the registered one rather than against a list.
_OWNED_NODE_TYPES: tuple[str, ...] = (
    "DESIGN",
    "DESIGN_LINE",
    "FUNCTIONAL_LOCATION",
    "DEVICE",
    "PORT",
    "RACK",
    "CABLE",
)

_UPSERTED_OP: str = "upserted"


async def handle_system_design_node_upserted(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    event: dict[str, Any],
) -> None:
    """Acknowledge one System Design ``<TYPE>.upserted`` event.

    Returning ``None`` means "delivered, no post-commit action".  The relay then
    marks the row published, which is the entire point: without a registered
    handler the same row dead-letters and is never published.

    Must not raise.  A handler that raises tells the relay the delivery failed,
    and after ``MAX_OUTBOX_ATTEMPTS`` the event lands in the DLQ anyway — so an
    exception here would recreate the very leak this module closes.  There is no
    I/O to fail on, and the ``conn`` is deliberately untouched.
    """
    log.debug(
        "[system_design] node upserted: type=%s id=%s ns=%s",
        event.get("aggregate_type"),
        event.get("aggregate_id"),
        event.get("namespace_id"),
    )
    return None


def register_system_design_subscribers() -> None:
    """Subscribe the System Design outbox handlers.

    Idempotent: ``register_handler`` refuses a duplicate by equality, and
    :func:`handle_system_design_node_upserted` is a single module-level object,
    so calling this twice (both relay processes in one test run, a re-bootstrap)
    registers each selector exactly once.

    Call this **before** a relay loop starts polling in the calling process.
    """
    for node_type in _OWNED_NODE_TYPES:
        subscribe(
            {"node_type": node_type, "op": _UPSERTED_OP},
            handle_system_design_node_upserted,
        )
    log.info(
        "System Design outbox subscribers registered for %d selectors.",
        len(_OWNED_NODE_TYPES),
    )
