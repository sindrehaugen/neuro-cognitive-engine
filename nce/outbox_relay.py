"""Transactional Outbox Relay — ordered, at-least-once delivery with consumer idempotency.

The relay polls unpublished rows from ``outbox_events`` using
``FOR UPDATE SKIP LOCKED``, dispatches each event to a registered handler,
marks successful deliveries with ``published_at``, increments ``attempt_count``
on failure, and routes exhausted events to ``dead_letter_queue``.

Delivery semantics
------------------
The relay guarantees **at-least-once** delivery: a crash between the handler
succeeding and ``mark_published`` being committed will cause the event to be
redelivered on the next relay pass.  Consumer idempotency is enforced via the
``processed_outbox_events`` dedup table — the event_id is inserted in the same
transaction as any handler DB effect issued on the connection the relay hands
the handler.  On re-delivery the INSERT conflicts on the primary key and the
event is skipped without re-executing handler effects, making the observable
result exactly-once from the consumer's perspective — *for handlers that write
on that connection*.  A handler that opens its own pool connection is outside
the relay's transaction and only gets at-least-once; see ``deliver_one``.

Handler contract
----------------
Handlers run **inside** the open transaction, which gives them MVCC-consistent
reads and lets ``mark_published`` be atomic with the delivery.  Handlers must
NOT perform external I/O (Redis, HTTP) inside the transaction — that holds the
DB connection while waiting on external services and starves the pool.

To fire external work after the commit, return a zero-argument callable from
the handler.  The relay collects these "post-commit actions" and calls them
after the transaction closes.  The canonical example is
``handle_memory_stored`` returning ``lambda: enqueue_memory_postprocess(payload)``
so the RQ enqueue runs after the DB row is already committed.

A handler must NEVER return normally when it could not do its job.  Returning
is how the relay is told "delivered": the dedup row commits, ``mark_published``
runs, and the event is gone for good.  A misconfiguration (a dependency that
was never registered in this process) must raise — see ``OutboxDeliveryError``.

Handlers registered via ``subscribe``/``@outbox_handler`` MUST NOT be wrapped in
``nce.events.dispatch.make_idempotent_handler``.  The dedup INSERT below already
runs *before* the handler, against the same table and the same ``event_id``, in
the same transaction — a wrapper that repeats the check always observes its own
conflict and returns without ever calling the business logic.

Fan-out semantics
-----------------
A selector (``"{node_type}.{op}"``) maps to a **list** of subscribers, all of
which are invoked for every delivery, in registration order.

- **One transaction, one dedup row, all handlers.**  ``processed_outbox_events``
  is keyed by ``event_id`` alone (no subscriber column), so "handler A applied,
  handler B did not" is not a representable state.
- **Any handler raising fails the whole event** — no partial application.  The
  shared SAVEPOINT rolls back (dedup mark included, so the retry is real),
  ``mark_failed`` bumps ``attempt_count``, and the event is retried (then
  dead-lettered after ``MAX_OUTBOX_ATTEMPTS``).  On
  redelivery *every* sibling handler runs again, so handlers MUST be idempotent.
  Swallowing a sibling's exception is deliberately not offered: committing the
  dedup row anyway would silently drop that subscriber's effect forever, and not
  committing it is indistinguishable from failing the event except that the
  failure becomes invisible to ``mark_failed``, the DLQ and the alert dispatcher.
  Per-subscriber isolation would require a migration to a
  ``(event_id, subscriber_id)`` dedup key; it is out of scope here.
- **Order is deterministic but must not be depended on.**  Handlers share a
  transaction and can therefore read a sibling's uncommitted writes; doing so is
  prohibited.  Registration order exists for reproducible tests and logs, not as
  a contract.
- **Registration is idempotent per handler object** (see ``register_handler``),
  so a double bootstrap does not double-fire.

Entry point: ``run_outbox_relay_once(pool)`` — call from APScheduler or an
asyncio periodic task in the server startup.  Subscribers are process-local
state: every process that runs the relay must register the handlers it needs
before the first pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

log = logging.getLogger("nce.outbox_relay")

_ALERT_THROTTLE_CACHE: dict[str, float] = {}
_THROTTLE_WINDOW_SECONDS = 300.0


async def _dispatch_throttled_alert(key: str, title: str, message: str) -> None:
    now = time.time()
    last_sent = _ALERT_THROTTLE_CACHE.get(key, 0.0)
    if now - last_sent >= _THROTTLE_WINDOW_SECONDS:
        _ALERT_THROTTLE_CACHE[key] = now
        try:
            from nce.notifications import dispatcher

            await dispatcher.dispatch_alert(title, message)
        except Exception:
            log.exception("Failed to dispatch throttled alert for key %s", key)


# Handlers may return an optional zero-arg callable to run after the
# transaction commits (e.g. Redis enqueue, HTTP notification).
# Return None if no post-commit work is needed.
PostCommitAction = Callable[[], None]
OutboxHandler = Callable[[asyncpg.Connection, dict[str, Any]], Awaitable[PostCommitAction | None]]

MAX_OUTBOX_ATTEMPTS: int = 5


class OutboxDeliveryError(Exception):
    """A delivery cannot proceed because this process is misconfigured.

    Raised by the relay when no handler is registered for an event type.
    Handlers should raise it too, when a dependency they need was never
    registered in this process.

    ``run_outbox_relay_once`` treats this type specially: it does NOT burn five
    retries on it, because the missing wiring will not appear between two relay
    passes.  The event is dead-lettered immediately and an alert is dispatched,
    so the misconfiguration is loud while the event stays replayable.

    Handlers must raise it rather than logging and returning: a normal return is
    indistinguishable from success, and commits the dedup row that makes the
    event unrecoverable.
    """


# ---------------------------------------------------------------------------
# Decorator-based handler registry
# ---------------------------------------------------------------------------

# Public dict: selector -> ordered list of subscribers.  Tests may replace an
# entry wholesale (``OUTBOX_HANDLERS["memory.stored"] = [fake]``) without
# touching the relay loop.
OUTBOX_HANDLERS: dict[str, list[OutboxHandler]] = {}


def register_handler(event_type: str, fn: OutboxHandler) -> bool:
    """Append *fn* to the subscriber list for *event_type*.

    The single write path into ``OUTBOX_HANDLERS`` — both ``@outbox_handler``
    and ``nce.events.bus.subscribe`` funnel through here so registration
    semantics live in exactly one place.

    Idempotent by handler equality: re-registering the same function object is
    a logged no-op rather than a second subscription that fires the side effect
    twice per event.  Equality (not identity) is used deliberately — bound
    methods compare equal by ``(__self__, __func__)`` but are never identical,
    since attribute access mints a fresh bound-method object every time.

    Known limitation: a freshly-constructed wrapper (a closure, a
    ``functools.partial``) is a different object on every call and therefore
    cannot be deduplicated.  Bootstraps MUST register module-level function
    objects.

    Returns True when newly registered, False when already present.
    """
    if not asyncio.iscoroutinefunction(fn):
        raise TypeError(
            f"register_handler: {getattr(fn, '__qualname__', fn)!r} must be an async def "
            "(no Redis/sync I/O inside the transaction)."
        )
    handlers = OUTBOX_HANDLERS.setdefault(event_type, [])
    if fn in handlers:
        log.warning(
            "[outbox] duplicate registration ignored: %s for event_type=%r",
            getattr(fn, "__qualname__", fn),
            event_type,
        )
        return False
    handlers.append(fn)
    return True


def snapshot_handlers() -> dict[str, list[OutboxHandler]]:
    """Deep-enough copy of the registry for save/restore in tests.

    A plain ``dict(OUTBOX_HANDLERS)`` would alias the live subscriber lists, so
    a subscription made after the snapshot would also mutate the "restore"
    target.  Copy each list.
    """
    return {key: list(handlers) for key, handlers in OUTBOX_HANDLERS.items()}


def restore_handlers(snapshot: dict[str, list[OutboxHandler]]) -> None:
    """Replace the registry contents with *snapshot* (taken by ``snapshot_handlers``)."""
    OUTBOX_HANDLERS.clear()
    OUTBOX_HANDLERS.update({key: list(handlers) for key, handlers in snapshot.items()})


def outbox_handler(event_type: str) -> Callable[[OutboxHandler], OutboxHandler]:
    """Register an async handler for *event_type* in the ``OUTBOX_HANDLERS`` registry.

    Usage::

        @outbox_handler("my.event")
        async def handle_my_event(conn: asyncpg.Connection, event: dict) -> PostCommitAction | None:
            ...

    Constraints enforced at decoration time:
    - The decorated function must be a coroutine function (no sync I/O inside
      the transaction; post-commit actions are returned as callables instead).

    Registering a second event type requires only a new ``@outbox_handler(...)``
    decorated function — zero relay-loop edits needed.  Several handlers may
    share one *event_type*; all of them are invoked on delivery.
    """

    def decorator(fn: OutboxHandler) -> OutboxHandler:
        register_handler(event_type, fn)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@outbox_handler("memory.stored")
async def handle_memory_stored(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> PostCommitAction:
    """Prepare the RQ enqueue action for 'memory.stored'.

    Returns a post-commit callable instead of enqueuing inside the transaction.
    This keeps Redis I/O out of the open DB transaction and prevents pool
    starvation when Redis is slow or briefly unreachable.
    """
    from nce.tasks import enqueue_memory_postprocess

    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    # Capture payload in closure — enqueue runs after transaction commits.
    captured = dict(payload)
    return lambda: enqueue_memory_postprocess(captured)


# ---------------------------------------------------------------------------
# Core relay operations
# ---------------------------------------------------------------------------


async def poll_outbox(
    conn: asyncpg.Connection,
    *,
    batch_size: int = 50,
) -> list[dict[str, Any]]:
    """Select unpublished rows within an open transaction (FOR UPDATE SKIP LOCKED)."""
    rows = await conn.fetch(
        """
        SELECT
            id,
            namespace_id,
            aggregate_type,
            aggregate_id,
            event_type,
            payload,
            headers,
            attempt_count,
            created_at
        FROM outbox_events
        WHERE published_at IS NULL
          AND attempt_count < $1
        ORDER BY created_at ASC
        LIMIT $2
        FOR UPDATE SKIP LOCKED
        """,
        MAX_OUTBOX_ATTEMPTS,
        batch_size,
    )
    return [dict(row) for row in rows]


async def mark_published(conn: asyncpg.Connection, event_id: Any) -> None:
    await conn.execute(
        "UPDATE outbox_events SET published_at = now() WHERE id = $1",
        event_id,
    )


async def mark_failed(
    conn: asyncpg.Connection,
    event_id: Any,
    error_message: str,
) -> None:
    await conn.execute(
        """
        UPDATE outbox_events
        SET attempt_count = attempt_count + 1,
            error_message = left($2, 2048)
        WHERE id = $1
        """,
        event_id,
        error_message,
    )


async def move_to_dead_letter_if_exhausted(
    conn: asyncpg.Connection,
    event: dict[str, Any],
    error_message: str,
) -> None:
    """Write a DLQ row when attempt_count has reached MAX_OUTBOX_ATTEMPTS."""
    if int(event["attempt_count"]) + 1 < MAX_OUTBOX_ATTEMPTS:
        return

    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    await conn.execute(
        """
        INSERT INTO dead_letter_queue (
            namespace_id,
            task_name,
            job_id,
            kwargs,
            error_message,
            attempt_count,
            status
        )
        VALUES ($1, $2, $3, $4::jsonb, left($5, 1024), $6, 'pending')
        """,
        event["namespace_id"],
        f"outbox:{event['event_type']}",
        str(event["id"]),
        json.dumps(
            {
                "outbox_event_id": str(event["id"]),
                "event_type": event["event_type"],
                "aggregate_type": event["aggregate_type"],
                "aggregate_id": str(event["aggregate_id"]),
                "payload": payload,
            },
            default=str,
            sort_keys=True,
        ),
        error_message,
        int(event["attempt_count"]) + 1,
    )
    log.warning(
        "[outbox] event_id=%s event_type=%s exhausted %d attempts — moved to DLQ",
        event["id"],
        event["event_type"],
        MAX_OUTBOX_ATTEMPTS,
    )
    await _dispatch_throttled_alert(
        f"outbox.dlq.{event['event_type']}",
        f"Outbox Event Dead-Lettered: outbox:{event['event_type']}",
        f"Outbox event '{event['event_type']}' (id {event['id']}) failed: {error_message}",
    )


class _AlreadyProcessed:
    """Singleton sentinel returned by ``deliver_one`` when the dedup table shows the event
    was already handled in a previous delivery attempt (crash between handler and
    mark_published)."""


_ALREADY_PROCESSED = _AlreadyProcessed()


async def deliver_one(
    conn: asyncpg.Connection,
    event: dict[str, Any],
) -> list[PostCommitAction] | _AlreadyProcessed:
    """Dispatch a single outbox event to every subscriber registered for its type.

    Idempotency: before invoking any handler, attempt to INSERT the event_id
    into ``processed_outbox_events``.  If the row already exists (primary-key
    conflict), the event was successfully processed in a previous delivery
    attempt whose ``mark_published`` was lost to a crash.  In that case the
    sentinel ``_ALREADY_PROCESSED`` is returned and the caller skips both the
    handlers and a second ``mark_published`` without counting it as a failure.

    The dedup INSERT and every statement a handler issues **on the ``conn``
    passed to it** share one SAVEPOINT inside the relay's transaction: those are
    atomically committed or rolled back together.  Consequently a raising
    handler fails the whole event — dedup mark included, so the event really is
    retried — and its siblings are re-run on redelivery.  See "Fan-out
    semantics" in the module docstring.

    That guarantee stops at the connection boundary, and this is not
    hypothetical.  A handler that opens its **own** pool connection — as
    ``project.tasks._handle_bom_line_status_changed`` does via
    ``scoped_pg_session(engine.pg_pool, ...)``, because RLS scoping needs a
    session-scoped ``SET`` the relay's polling connection must not carry — is
    running a second, independent transaction.  The relay cannot roll that back.
    So for such a handler:

    - a later sibling raising, or the relay transaction failing after the
      handler returned, leaves the handler's writes **committed** while the
      dedup row vanishes → the event is redelivered and the writes are applied
      again;
    - the handler crashing mid-way can leave its own work **partially applied**
      relative to the dedup row.

    Both are survivable only because those writes are idempotent upserts.  A
    handler doing non-idempotent work on its own connection would corrupt state
    here, and must not be registered.

    The "no subscriber" check runs BEFORE the dedup INSERT on purpose: a missing
    subscriber is a configuration error, and burning the dedup row would suppress
    the event permanently even after the subscriber is correctly wired.

    Returns the subscribers' post-commit actions in registration order (possibly
    empty) for the caller to fire after the surrounding transaction commits, or
    ``_ALREADY_PROCESSED`` when the event was already handled.
    """
    event_type = event["event_type"]
    handlers = OUTBOX_HANDLERS.get(event_type) or []
    if not handlers:
        # An empty list is semantically identical to a missing key (e.g. every
        # subscriber was removed), so it takes the same fast-fail-to-DLQ path.
        raise OutboxDeliveryError(f"No outbox handler registered for event_type={event_type!r}")

    # SAVEPOINT around the dedup mark AND every handler, so the two either
    # commit together or vanish together.  Two things depend on it:
    #
    # (a) AT-LEAST-ONCE ON FAILURE.  Without it a raising handler still leaves
    #     the dedup row committed (the relay catches the exception *inside* its
    #     transaction and then commits), so the redelivery finds the row, reports
    #     "already processed" and marks the event published — the effect is lost
    #     for good.  Rolling back to the savepoint restores the retry.
    # (b) OUTER-TRANSACTION SURVIVAL.  A handler raising a *database* error would
    #     otherwise leave the relay's transaction aborted, so mark_failed and the
    #     DLQ insert would fail too.  ROLLBACK TO SAVEPOINT recovers it.
    async with conn.transaction():
        # Dedup check: INSERT ... ON CONFLICT DO NOTHING returns the inserted row count.
        # A zero row-count means the event was already processed; skip handler effects.
        inserted = await conn.fetchval(
            """
            INSERT INTO processed_outbox_events (event_id, namespace_id)
            VALUES ($1, $2)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            event["id"],
            event["namespace_id"],
        )
        if inserted is None:
            log.info(
                "[outbox] event_id=%s already in processed_outbox_events — skipping (at-least-once replay)",
                event["id"],
            )
            return _ALREADY_PROCESSED

        actions: list[PostCommitAction] = []
        for handler in handlers:
            # No per-handler try/except: an exception must fail the whole event so
            # the relay records the failure and redelivers.  See module docstring.
            action = await handler(conn, event)
            if action is not None:
                actions.append(action)
        return actions


# ---------------------------------------------------------------------------
# Public relay loop
# ---------------------------------------------------------------------------


async def run_outbox_relay_once(
    pool: asyncpg.Pool,
    *,
    batch_size: int = 50,
) -> int:
    """
    Run one relay pass: poll → deliver → mark_published or mark_failed → DLQ.

    Returns the number of events successfully delivered in this pass.

    The transaction covers only DB operations (poll, mark_published/failed,
    DLQ insert).  Any post-commit actions returned by handlers (e.g. Redis
    enqueues) are collected during the transaction and fired **after** the
    transaction commits so external I/O never holds the DB connection open.
    """
    delivered = 0
    post_commit_actions: list[PostCommitAction] = []

    committed = False

    try:
        async with pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                events = await poll_outbox(conn, batch_size=batch_size)

                for event in events:
                    try:
                        actions = await deliver_one(conn, event)
                        if isinstance(actions, _AlreadyProcessed):
                            # Dedup: event was already successfully processed.
                            # Mark published now so the relay won't re-poll it.
                            await mark_published(conn, event["id"])
                            continue
                    except Exception as exc:
                        error_message = f"{type(exc).__name__}: {exc}"
                        log.exception(
                            "[outbox] delivery failed event_id=%s event_type=%s",
                            event["id"],
                            event["event_type"],
                        )
                        await _dispatch_throttled_alert(
                            f"outbox.delivery_failed.{event['event_type']}",
                            f"Outbox Delivery Failed: {event['event_type']}",
                            f"Outbox event delivery failed for event_id {event['id']}: {error_message}",
                        )
                        try:
                            from nce.observability import OUTBOX_DELIVERY_FAILURES_TOTAL

                            OUTBOX_DELIVERY_FAILURES_TOTAL.labels(
                                event_type=str(event["event_type"])
                            ).inc()
                        except Exception:
                            pass
                        if isinstance(exc, OutboxDeliveryError):
                            await conn.execute(
                                """
                                UPDATE outbox_events
                                SET attempt_count = $1,
                                    error_message = left($2, 2048)
                                WHERE id = $3
                                """,
                                MAX_OUTBOX_ATTEMPTS,
                                error_message,
                                event["id"],
                            )
                            event_copy = dict(event)
                            event_copy["attempt_count"] = MAX_OUTBOX_ATTEMPTS - 1
                            await move_to_dead_letter_if_exhausted(conn, event_copy, error_message)
                            try:
                                from nce.observability import OUTBOX_DLQ_TOTAL

                                OUTBOX_DLQ_TOTAL.labels(event_type=str(event["event_type"])).inc()
                            except Exception:
                                pass
                        else:
                            await mark_failed(conn, event["id"], error_message)
                            prior_attempts = int(event["attempt_count"])
                            await move_to_dead_letter_if_exhausted(conn, event, error_message)
                            if prior_attempts + 1 >= MAX_OUTBOX_ATTEMPTS:
                                try:
                                    from nce.observability import OUTBOX_DLQ_TOTAL

                                    OUTBOX_DLQ_TOTAL.labels(
                                        event_type=str(event["event_type"])
                                    ).inc()
                                except Exception:
                                    pass
                        continue

                    await mark_published(conn, event["id"])
                    delivered += 1
                    post_commit_actions.extend(actions)
                    try:
                        from nce.observability import OUTBOX_DELIVERED_TOTAL

                        OUTBOX_DELIVERED_TOTAL.labels(event_type=str(event["event_type"])).inc()
                    except Exception:
                        pass
            # Transaction committed.  Fire external work outside the DB connection.
            committed = True
    finally:
        # Synchronous, and in a finally, DELIBERATELY (M0.W20d).
        #
        # Synchronous: asyncio only cancels at an await, so a drain with no
        # await points cannot be cancelled part-way. Round 2 of this wave
        # rebuilt this loop WITH awaits, and because mcp_stdio_main cancels
        # the relay task on every graceful SIGTERM, a cancel mid-drain lost
        # already-published events' actions with no DLQ row (measured then:
        # actions_fired=1, published=5, DLQ=0). Do not add an await here.
        #
        # In a finally: the drain used to sit after the `async with`, whose
        # __aexit__ awaits while releasing the connection. A cancel delivered
        # there skipped the drain while the events were already marked
        # published -- the same loss, one frame earlier.
        #
        # Gated on `committed`: if the transaction rolled back, the
        # mark_published rows went with it and the events will be re-polled,
        # so firing their actions here would double-enqueue.
        if committed:
            for action in post_commit_actions:
                try:
                    action()
                except Exception as exc:
                    log.warning("[outbox] post-commit action failed: %s", exc)

    log.debug("[outbox] relay pass complete: delivered=%d", delivered)
    return delivered
