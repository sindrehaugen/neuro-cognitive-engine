"""
C2 ``@governed`` decorator — Contract B autonomy gate.

Every Actor/Autonomous *mutating* handler passes through this decorator.
Four non-negotiable invariants are enforced here and nowhere else:

1. **Confirm-only default** — without an explicit ``confirm=True`` the
   wrapped handler is **never** called; the decorator returns a structured
   ``{"status": "pending_approval", ...}`` result instead.

2. **Idempotency required** — the caller *must* supply a non-empty
   ``idempotency_key`` (the argument name is configurable).  Without it the
   decorator raises ``MissingIdempotencyKeyError`` before any side effect.

3. **Audit on every execution** — on first confirmed execution the decorator
   records the key in ``action_idempotency`` and appends an entry to
   ``event_log`` via ``append_event``.  A replay of the same key is a
   NO-OP (the side effect runs exactly once).

4. **Contract-B gates** (Wave 16) — when ``confirm=True`` the decorator
   enforces, in order:

   a. **Kill switch** — ``nce:tools:disabled`` Redis hash per-tool and
      global (``"*"`` field).  *Fail-closed*: Redis unreachable blocks the
      act (never allows).  Kill switch only fires when a Redis client is
      wired up; without one the gate is skipped (caller opted out).

   b. **Policy gates** — value ceiling, volume/rate cap, counterparty
      allowlist, and risk flags (``flagship | first_of_kind | regulated``
      force human-confirm *regardless* of value band).  Any gate firing
      returns ``{"status": "pending_approval", "reason": ...}``.

Dependency rule: this module imports only from the standard library,
asyncpg, and the three NCE sub-packages it directly needs (``db_utils``,
``event_log``, ``autonomy.policy``).  No HTTP / web / admin modules must
appear here (uncle-bob inward-pointing rule).

Usage::

    @governed(action_type="update_device_config")
    async def update_device(
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        *,
        idempotency_key: str,
        confirm: bool = False,
        device_id: str,
    ) -> dict[str, Any]:
        ...            # side effect only runs when confirm=True + dedup passes

    # Caller without confirm → pending_approval (no side effect):
    result = await update_device(conn, ns_id, idempotency_key="k1", device_id="d1")
    # {"status": "pending_approval", "idempotency_key": "k1", "action_type": "update_device_config"}

    # Caller with confirm → executes once, audited:
    result = await update_device(conn, ns_id, idempotency_key="k1", confirm=True, device_id="d1")
    # {"status": "executed", "result": <handler return>, "idempotency_key": "k1"}

    # Second call with same key → NO-OP (side effect not re-run):
    result = await update_device(conn, ns_id, idempotency_key="k1", confirm=True, device_id="d1")
    # {"status": "already_executed", "idempotency_key": "k1"}
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ParamSpec, TypeVar

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.policy import PolicyDecision, evaluate_policy
from nce.event_log import AppendResult, append_event

log = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class GovernanceError(Exception):
    """Base class for all @governed policy violations."""


class MissingIdempotencyKeyError(GovernanceError):
    """Raised when the handler is called without a non-empty idempotency_key."""


class KillSwitchError(GovernanceError):
    """Raised when the ``nce:tools:disabled`` kill switch blocks the action.

    Fires on:
    - per-tool key present in the hash (``hget nce:tools:disabled <action_type>``).
    - global key ``"*"`` present in the hash (all governed actions halted).
    - Redis unreachable (fail-closed — never treat an error as "enabled").
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_AGENT_ID = "governor"

#: Redis hash key used for per-tool and global disable toggles.
_DISABLED_HASH = "nce:tools:disabled"

#: Field name inside ``_DISABLED_HASH`` that disables ALL governed actions.
_GLOBAL_DISABLE_FIELD = "*"


async def _check_kill_switch(redis_client: Any, action_type: str) -> None:
    """Check ``nce:tools:disabled`` for per-tool and global kill-switch fields.

    Fail-closed: any Redis exception (unreachable, timeout, etc.) raises
    ``KillSwitchError`` — an error is **never** treated as "enabled".

    Args:
        redis_client: An aioredis / redis.asyncio client with an async
                      ``hexists(key, field)`` method.  If ``None`` the
                      kill-switch gate is **skipped** (caller has not wired
                      one up).
        action_type:  The per-tool label to check (e.g. ``"submit_po"``).

    Raises:
        KillSwitchError: When the tool or the global switch is disabled, or
                         when Redis is unreachable (fail-closed).
    """
    if redis_client is None:
        # No Redis client provided — gate is not wired; skip.
        # Log a warning so misconfigured deployments are observable at runtime.
        log.warning(
            "[governor] kill-switch gate NOT wired (redis_client=None) for "
            "action_type=%r — confirm=True execution proceeds without kill-switch "
            "protection; pass a Redis client to enable the gate.",
            action_type,
        )
        return

    try:
        if await redis_client.hexists(_DISABLED_HASH, action_type):
            raise KillSwitchError(
                f"Tool '{action_type}' is disabled via kill switch "
                f"({_DISABLED_HASH}:{action_type})."
            )
        if await redis_client.hexists(_DISABLED_HASH, _GLOBAL_DISABLE_FIELD):
            raise KillSwitchError(
                f"All governed actions are disabled via global kill switch "
                f"({_DISABLED_HASH}:{_GLOBAL_DISABLE_FIELD})."
            )
    except KillSwitchError:
        raise
    except Exception as exc:
        # Redis unreachable — fail-closed: block the action.
        raise KillSwitchError(
            f"Kill-switch Redis check failed for '{action_type}' "
            f"(treating as disabled — fail-closed): {exc}"
        ) from exc


def _extract_kwarg(kwargs: dict[str, Any], name: str) -> Any:
    """Return ``kwargs[name]`` without mutating the original dict."""
    return kwargs.get(name)


async def _record_idempotency_key(
    conn: asyncpg.Connection,
    namespace_id: Any,
    idempotency_key: str,
    action_type: str,
) -> None:
    """Insert the idempotency key into ``action_idempotency``.

    Called inside an existing transaction (the ``scoped_pg_session`` transaction
    opened by the caller).  Raises ``asyncpg.UniqueViolationError`` when the key
    is already present — callers interpret this as a dedup hit.
    """
    await conn.execute(
        """
        INSERT INTO action_idempotency (idempotency_key, namespace_id, action_type)
        VALUES ($1, $2, $3)
        """,
        idempotency_key,
        namespace_id,
        action_type,
    )


async def _idempotency_key_exists(
    conn: asyncpg.Connection,
    namespace_id: Any,
    idempotency_key: str,
) -> bool:
    """Return True when ``(namespace_id, idempotency_key)`` is already recorded."""
    row = await conn.fetchrow(
        """
        SELECT 1 FROM action_idempotency
        WHERE namespace_id = $1 AND idempotency_key = $2
        """,
        namespace_id,
        idempotency_key,
    )
    return row is not None


async def _audit_execution(
    conn: asyncpg.Connection,
    namespace_id: Any,
    idempotency_key: str,
    action_type: str,
) -> AppendResult:
    """Append one audit entry to ``event_log`` via ``append_event``.

    Uses ``config_changed`` as the event type (the closest generic governance
    event in the current registry) with ``actor`` and ``changes`` params so
    the required-param contract is satisfied.  Wave 16+ may introduce a
    dedicated ``governed_action_executed`` event type.
    """
    return await append_event(
        conn=conn,
        namespace_id=namespace_id,
        agent_id=_AGENT_ID,
        event_type="config_changed",
        params={
            "actor": _AGENT_ID,
            "changes": {
                "governed_action": action_type,
                "idempotency_key": idempotency_key,
            },
        },
    )


# ---------------------------------------------------------------------------
# Core gate logic (single function — SRP)
# ---------------------------------------------------------------------------


async def _execute_governed(
    fn: Callable[..., Awaitable[Any]],
    *,
    conn: asyncpg.Connection,
    namespace_id: Any,
    idempotency_key: str,
    action_type: str,
    fn_args: tuple[Any, ...],
    fn_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Check dedup, execute once, and audit — all inside the caller's transaction.

    Returns a status dict:
    - ``{"status": "already_executed", "idempotency_key": ...}`` when the key
      is already in ``action_idempotency`` (NO-OP path).
    - ``{"status": "executed", "result": ..., "idempotency_key": ...}`` on first
      execution (side effect runs, key recorded, event audited).

    The outer ``scoped_pg_session`` transaction is already open on ``conn``; we
    do NOT open a nested transaction here.  If the INSERT or ``append_event``
    fails the whole outer transaction rolls back — atomicity is preserved.
    """
    # --- Transaction guard (defense-in-depth) ---
    # If conn is not inside an active transaction the idempotency INSERT would
    # auto-commit, then append_event would raise (it requires a live transaction),
    # leaving a POISON KEY that suppresses the action forever with no audit trail.
    if not conn.is_in_transaction():
        raise GovernanceError(
            f"@governed handler '{fn.__name__}': conn is not inside an active "
            "transaction — call inside scoped_pg_session to prevent a poison "
            "idempotency key on audit failure."
        )

    already_done = await _idempotency_key_exists(conn, namespace_id, idempotency_key)
    if already_done:
        log.info(
            "[governor] NO-OP: idempotency_key=%r action_type=%s already_executed",
            idempotency_key,
            action_type,
        )
        return {"status": "already_executed", "idempotency_key": idempotency_key}

    # First execution: record key before calling the handler so a crash inside
    # the handler rolls back the key alongside the rest of the transaction
    # (the transaction is owned by the caller — we never commit here).
    # A concurrent caller racing through the same key hits UniqueViolationError
    # here; we catch it and return already_executed rather than propagating.
    try:
        await _record_idempotency_key(conn, namespace_id, idempotency_key, action_type)
    except asyncpg.UniqueViolationError:
        log.info(
            "[governor] NO-OP (race): idempotency_key=%r action_type=%s already_executed",
            idempotency_key,
            action_type,
        )
        return {"status": "already_executed", "idempotency_key": idempotency_key}

    result = await fn(*fn_args, **fn_kwargs)

    await _audit_execution(conn, namespace_id, idempotency_key, action_type)

    log.info(
        "[governor] EXECUTED: idempotency_key=%r action_type=%s",
        idempotency_key,
        action_type,
    )
    return {"status": "executed", "result": result, "idempotency_key": idempotency_key}


# ---------------------------------------------------------------------------
# Public decorator
# ---------------------------------------------------------------------------


def governed(
    *,
    action_type: str,
    idempotency_key_arg: str = "idempotency_key",
    confirm_arg: str = "confirm",
    conn_arg: str = "conn",
    namespace_id_arg: str = "namespace_id",
    # --- Contract-B gate parameters (Wave 16) ---
    redis_client_arg: str = "redis_client",
    value_arg: str = "value",
    value_ceiling: float | None = None,
    volume_state_arg: str = "volume_state",
    volume_rate_cap: float | None = None,
    counterparty_arg: str = "counterparty",
    allowlist: Sequence[str] | None = None,
    risk_flags_arg: str = "risk_flags",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[dict[str, Any]]]]:
    """Wrap an async mutating handler with the C2 autonomy gate.

    The decorator inspects keyword arguments at call-time (not decoration-time)
    so it works with any async handler signature that carries the required
    arguments by name.

    Args:
        action_type:          Stable label for this action (stored in the audit log).
        idempotency_key_arg:  Name of the kwarg carrying the idempotency key.
        confirm_arg:          Name of the kwarg that opts in to execution (default ``"confirm"``).
        conn_arg:             Name of the kwarg carrying the asyncpg connection.
        namespace_id_arg:     Name of the kwarg carrying the namespace UUID.

        redis_client_arg:  Name of the kwarg carrying a Redis client for the
                           kill-switch check.  When the resolved value is
                           ``None`` the kill-switch gate is skipped (caller has
                           not wired one up).  When provided and Redis raises,
                           the act is **blocked** (fail-closed).
        value_arg:         Name of the kwarg carrying the act's monetary/scalar
                           value for the ceiling gate.
        value_ceiling:     Maximum value for autonomous execution.  ``None``
                           disables the value gate.
        volume_state_arg:  Name of the kwarg carrying the current volume / rate
                           accumulator.
        volume_rate_cap:   Maximum allowed volume / rate.  ``None`` disables.
        counterparty_arg:  Name of the kwarg carrying the counterparty name/ID.
        allowlist:         Sequence of permitted counterparties.  ``None`` or
                           empty disables the allowlist gate.
        risk_flags_arg:    Name of the kwarg carrying a sequence of risk labels.

    The wrapped handler **must** be called with:
    - a non-empty ``idempotency_key`` (raises ``MissingIdempotencyKeyError`` otherwise)
    - ``conn`` and ``namespace_id`` present in kwargs (required for dedup + audit)

    Without ``confirm=True`` the handler returns ``{"status": "pending_approval", ...}``.

    When ``confirm=True``, gates fire in this order:
    1. Kill switch (``nce:tools:disabled`` Redis hash — fail-closed when wired).
    2. Policy gates (risk flags, value ceiling, volume cap, allowlist).
       Any gate firing returns ``{"status": "pending_approval", "reason": ...}``.
    3. Dedup + execute + audit (Wave 15 path, unchanged).
    """

    def decorator(
        fn: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[dict[str, Any]]]:
        # Bind the wrapped function's signature once at decoration time so the
        # wrapper can resolve positional-or-keyword args by name at call time.
        _sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            # Resolve all arguments (positional + keyword) into a single mapping
            # so the decorator can look up any parameter by name regardless of
            # how the caller passed it.
            try:
                bound = _sig.bind(*args, **kwargs)
                bound.apply_defaults()
            except TypeError:
                # Binding failure — let the handler raise its own TypeError.
                bound = None  # type: ignore[assignment]

            def _get_arg(name: str) -> Any:
                if bound is not None and name in bound.arguments:
                    return bound.arguments[name]
                return kwargs.get(name)

            # --- 1. Require a non-empty idempotency key ---
            idempotency_key: Any = _get_arg(idempotency_key_arg)
            if not idempotency_key or not str(idempotency_key).strip():
                raise MissingIdempotencyKeyError(
                    f"@governed handler '{fn.__name__}' requires a non-empty "
                    f"'{idempotency_key_arg}' kwarg — autonomous actions must be "
                    "idempotency-keyed to prevent double-execution."
                )
            idempotency_key = str(idempotency_key).strip()

            # --- 2. Confirm-only default: no side effect without explicit confirm ---
            confirm: bool = bool(_get_arg(confirm_arg))
            if not confirm:
                log.info(
                    "[governor] PENDING: idempotency_key=%r action_type=%s (no confirm)",
                    idempotency_key,
                    action_type,
                )
                return {
                    "status": "pending_approval",
                    "idempotency_key": idempotency_key,
                    "action_type": action_type,
                }

            # --- 3. Kill switch (fail-closed when Redis is wired up) ---
            redis_client: Any = _get_arg(redis_client_arg)
            await _check_kill_switch(redis_client, action_type)

            # --- 4. Contract-B policy gates ---
            policy: PolicyDecision = evaluate_policy(
                value=_get_arg(value_arg),
                value_ceiling=value_ceiling,
                volume_state=_get_arg(volume_state_arg),
                volume_rate_cap=volume_rate_cap,
                counterparty=_get_arg(counterparty_arg),
                allowlist=allowlist,
                risk_flags=_get_arg(risk_flags_arg),
            )
            if policy.requires_confirm:
                log.info(
                    "[governor] POLICY_GATE: idempotency_key=%r action_type=%s reason=%r",
                    idempotency_key,
                    action_type,
                    policy.reason,
                )
                return {
                    "status": "pending_approval",
                    "idempotency_key": idempotency_key,
                    "action_type": action_type,
                    "reason": policy.reason,
                }

            # --- 5. Resolve conn + namespace_id for dedup/audit ---
            # These may be positional args (e.g. handler(conn, ns_id, ...)) or
            # keyword args — _get_arg handles both via the bound-arguments map.
            conn: asyncpg.Connection | None = _get_arg(conn_arg)
            namespace_id: Any = _get_arg(namespace_id_arg)
            if conn is None or namespace_id is None:
                raise GovernanceError(
                    f"@governed handler '{fn.__name__}' called with confirm=True "
                    f"but '{conn_arg}' or '{namespace_id_arg}' is missing — "
                    "cannot enforce idempotency or write audit log."
                )

            # --- 6. Dedup + execute + audit (inside the caller's transaction) ---
            return await _execute_governed(
                fn,
                conn=conn,
                namespace_id=namespace_id,
                idempotency_key=idempotency_key,
                action_type=action_type,
                fn_args=args,
                fn_kwargs=kwargs,
            )

        return wrapper  # type: ignore[return-value]

    return decorator
