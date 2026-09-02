"""
nce/vertical_modules/project/automation.py
==========================================
P3 tier-gated auto-tasking for the Project Engine.

Overview
--------
Subscribes to Procurement (``PO_LINE.status_changed``) and Warehouse
(``GOODS_RECEIPT.created``) graph events via the C4 outbox bus and fires an RQ
task that calls W6's ``do_sync_bom_tasks``.

🔴 **DORMANT BY DECISION (Sindre, 2026-09-01) — not broken, and not a TODO.**
Nothing in this repository emits either selector, so these handlers are
registered and never invoked. That is now an accepted state rather than a
defect awaiting a wave:

* The only ops emitted anywhere in ``nce/`` are ``upserted`` (27 sites),
  ``deleted``, ``edge_realized_as``, ``expiry`` and ``retired``.
  ``status_changed`` is emitted **nowhere**.
* Procurement's node type is ``PO`` (``procurement/graph.py``), **not**
  ``PO_LINE``, and it emits ``op="upserted"``. There is no ``PO_LINE`` node
  type, no PO status column and no PO status-write path.
* Warehouse emits ``GOODS_RECEIPT.upserted``, a different selector with a
  different payload contract — ``inventory/goods_receipt.py`` says so and says
  not to "fix" it by re-labelling.

Waking it up is a **feature** (Procurement needs a ``PO_LINE`` node with a
status model), not wiring. Was ML.md's **M0.W20c**; parked by that decision.
**Do not "repair" this module by inventing a write site or by re-pointing the
selectors at ``*.upserted``** — the base four-key payload carries no
``project_id``, so every handler below would take its ``log.warning; return``
branch, which the relay reads as SUCCESS. That is a silent permanent drop, and
it is strictly worse than dormancy.

The subscribers ARE registered at both relay-running processes as of M0.W20d,
deliberately: an unregistered ``event_type`` fast-fails to the DLQ, so
registration is what stops the first real producer manufacturing DLQ rows.

Autonomy gating (Contract B §9.5)
----------------------------------
The BOM sync is a *mutating* act.  Autonomy is **tier-gated**:

- The project's contract value is resolved against ``automation-tiers.json``
  (the *value axis*).  Tier 1 (<50 K) = autonomous self-execution; Tiers 2–4
  = confirm-first (``{"status": "pending_approval"}`` returned, human must
  approve before the side effect runs).

- The *ceiling/idempotency/kill-switch/ledger* machinery lives **exclusively**
  in ``@governed`` (``nce.autonomy.governor``).  This module NEVER re-implements
  those invariants — it resolves the tier and passes ``confirm`` and
  ``value_ceiling`` through to the decorator.

Security invariants (C2)
------------------------
1. ``confirm`` is derived *only* from the tier resolution — never from the
   incoming event payload (caller cannot self-escalate to autonomous).
2. Kill switch (``nce:tools:disabled``) is enforced inside ``@governed`` —
   fail-closed when Redis is wired; see ``register_redis_client``.
3. Every confirmed execution is appended to the WORM ``event_log``.
4. Idempotency key = ``"bom_sync:{namespace_id}:{bom_line_label}:{status}"``
   — deterministic, replay-safe.
5. ``project_value`` from an event payload is **untrusted**.  When the key is
   absent the gate defaults to Tier-4 (confirm-required) — fail-closed.
   A publisher supplying 0.0 explicitly is treated as a legitimate zero-value
   project (Tier 1).  There is no authoritative cross-check at this layer;
   that is a future concern when Module 5 (Sales) ships.

Redis client lifetime in RQ workers
-------------------------------------
Redis clients are NOT JSON-serialisable and cannot be passed through RQ's
task queue.  The canonical pattern is:

1. At worker startup call ``register_redis_client(redis_client)`` once.
2. The RQ task functions (``rq_sync_bom_on_po``,
   ``rq_sync_bom_on_goods_receipt``) call ``_get_registered_redis_client()``
   at task execution time to retrieve the live client.
3. If the worker has not called ``register_redis_client`` the registry returns
   ``None``, which causes ``@governed`` to **skip the kill-switch gate** (with
   a warning log) — this preserves backwards compatibility for worker configs
   that deliberately opt out of Redis.  Production workers MUST register a
   client; the missing-client warning is observable at runtime.

Design invariants (uncle-bob-craft)
------------------------------------
- Dependencies point inward: only stdlib, asyncpg, nce.db_utils,
  nce.events.bus, nce.autonomy.governor, and the sibling tasks module.
- SRP: ``resolve_tier`` owns the JSON parse; ``_do_governed_bom_sync`` is the
  domain core (thin, decorated); the RQ task adapters (``rq_sync_bom_on_po``,
  ``rq_sync_bom_on_goods_receipt``) are thin adapters only.
- No HTTP / web / admin imports at module level.
- WORM invariant: event_log rows are INSERT-only — done inside @governed.
- Confidence (0–1) lives on kg_edges only (never kg_nodes).
- All DB queries carry an explicit ``namespace_id`` filter — never rely on
  RLS alone.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
import threading
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.db_utils import scoped_pg_session
from nce.events.bus import PostCommitAction, subscribe
from nce.vertical_modules.project.tasks import do_sync_bom_tasks

log = logging.getLogger("nce.vertical_modules.project.automation")

# ---------------------------------------------------------------------------
# C4 bus selectors — Procurement and Warehouse event node types
# ---------------------------------------------------------------------------

_PO_LINE_NODE_TYPE: str = "PO_LINE"
_PO_LINE_OP: str = "status_changed"

_GOODS_RECEIPT_NODE_TYPE: str = "GOODS_RECEIPT"
_GOODS_RECEIPT_OP: str = "created"

# ---------------------------------------------------------------------------
# Tier constants — mirror automation-tiers.json; kept here for fast path.
# If the JSON is edited the tests will fail to match and catch the drift.
# ---------------------------------------------------------------------------

_TIER1_MAX: float = 49_999.99  # Autonomous (<50K)
_TIER2_MAX: float = 499_999.99  # Actor/Confirm (<500K)
_TIER3_MAX: float = 2_999_999.99  # Advisor + PL Review (<3M)
# Tier 4: 3M+ = Advisor Only (confirm_required=True)

# Sentinel: when a publisher does not supply project_value we resolve the
# MOST RESTRICTIVE tier (Tier 4) rather than defaulting to 0.0 (Tier 1).
# This is the fail-closed value — any amount >= 3M → Tier 4.
_TIER_FAILCLOSED_VALUE: float = 3_000_000.0

# ---------------------------------------------------------------------------
# Tier resolution — pure function, no I/O
# ---------------------------------------------------------------------------


def _load_tiers() -> list[dict[str, Any]]:
    """Load the ``automation-tiers.json`` value bands at import time."""
    try:
        pkg = importlib.resources.files("nce.config_data")
        data = json.loads((pkg / "automation-tiers.json").read_text(encoding="utf-8"))
        return data["tiers"]
    except Exception as exc:  # pragma: no cover — config file must always exist
        log.error("[automation] Failed to load automation-tiers.json: %s", exc)
        return []


_TIERS: list[dict[str, Any]] = _load_tiers()


def resolve_tier(project_value: float) -> dict[str, Any]:
    """Return the tier dict for *project_value* from automation-tiers.json.

    Falls back to the highest tier (Advisor Only) when no tier matches or
    the config is empty — fail-closed (most restrictive).

    The tier dict shape::

        {
            "tier": int,
            "label": str,
            "autonomy_level": str,
            "confirm_required": bool,
            "min_value": float,
            "max_value": float | None,
        }
    """
    for tier in sorted(_TIERS, key=lambda t: t["tier"]):
        max_val = tier.get("max_value")
        if max_val is None or project_value <= max_val:
            return tier

    # Fallback: most-restrictive tier (confirm required)
    if _TIERS:
        return max(_TIERS, key=lambda t: t["tier"])
    # No tiers configured — absolute fail-closed
    return {
        "tier": 4,
        "label": "Advisor Only",
        "autonomy_level": "advisor_only",
        "confirm_required": True,
        "min_value": 0,
        "max_value": None,
    }


# ---------------------------------------------------------------------------
# Domain core — @governed mutating handler
# ---------------------------------------------------------------------------

# The value_ceiling for Tier 1 is _TIER1_MAX.  We decorate with a generous
# ceiling and rely on runtime tier resolution to set confirm=True for larger
# projects.  The ceiling here is the Tier-1 ceiling; if the value exceeds it
# the policy gate fires even when the caller mistakenly passes confirm=True.
_AUTONOMOUS_CEILING: float = _TIER1_MAX


@governed(
    action_type="project_auto_sync_bom_tasks",
    value_ceiling=_AUTONOMOUS_CEILING,
)
async def _do_governed_bom_sync(
    conn: asyncpg.Connection,
    namespace_id: UUID,
    *,
    idempotency_key: str,
    confirm: bool = False,
    value: float | None = None,
    redis_client: Any = None,
    # --- domain params forwarded to do_sync_bom_tasks ---
    engine: Any,
    project_id: str,
    bom_line_label: str,
    status: str,
) -> dict[str, Any]:
    """Governed BOM sync: side effect runs only when confirm=True + gates pass.

    The ``conn`` and ``namespace_id`` are required by ``@governed`` for dedup
    and audit.  The actual domain work (TASK upsert + generates edge) is
    delegated to ``do_sync_bom_tasks``, which opens its own scoped session.

    Security note: ``confirm`` is NEVER derived from the event payload — the
    caller (``_run_governed_sync``) derives it *only* from the tier resolution,
    preventing a caller from self-escalating to autonomous.
    """
    return await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_id,
            "bom_line_label": bom_line_label,
            "status": status,
        },
    )


# ---------------------------------------------------------------------------
# Engine registry (mirrors tasks.py pattern)
# ---------------------------------------------------------------------------

_ENGINE_REGISTRY: dict[str, Any] = {}


def register_engine(engine: Any) -> None:
    """Register *engine* for use by automation handlers.

    Must be called at startup (after ``tasks.register_engine``).
    """
    _ENGINE_REGISTRY["engine"] = engine


def _get_registered_engine() -> Any | None:
    return _ENGINE_REGISTRY.get("engine")


# ---------------------------------------------------------------------------
# Redis client registry — kill-switch gate for RQ worker tasks
# ---------------------------------------------------------------------------
# Redis clients are NOT JSON-serialisable and cannot pass through RQ's task
# queue.  The RQ worker must call ``register_redis_client`` once at startup;
# the RQ task functions retrieve it here at execution time.

_REDIS_REGISTRY: dict[str, Any] = {}


def register_redis_client(client: Any) -> None:
    """Register *client* for use by the RQ task functions.

    Must be called once at worker startup before any RQ task executes.
    Without this call the kill-switch gate is skipped (warning logged).
    """
    _REDIS_REGISTRY["client"] = client


def _get_registered_redis_client() -> Any | None:
    """Return the registered Redis client, or None if not registered."""
    return _REDIS_REGISTRY.get("client")


# ---------------------------------------------------------------------------
# Asyncio bridge (sync RQ context → async domain core)
# ---------------------------------------------------------------------------


def _run_sync(coro: Any) -> Any:  # type: ignore[misc]
    """Run *coro* in the current or a new event loop (RQ-safe)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
    else:
        result: list[Any] = []
        errors: list[BaseException] = []

        def _worker() -> None:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result.append(new_loop.run_until_complete(coro))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                new_loop.close()

        t = threading.Thread(target=_worker)
        t.start()
        t.join()
        if errors:
            raise errors[0]
        return result[0]


# ---------------------------------------------------------------------------
# Inner async orchestrator — resolves tier + calls @governed
# ---------------------------------------------------------------------------


async def _run_governed_sync(
    *,
    namespace_id: UUID,
    project_id: str,
    bom_line_label: str,
    status: str,
    project_value: float | None,
    redis_client: Any,
) -> dict[str, Any]:
    """Resolve tier, derive confirm, then call ``_do_governed_bom_sync``.

    ``confirm`` is derived *only* from the tier resolution — the event payload
    cannot influence it.  This is the sole location where autonomous execution
    is permitted, and only for Tier-1 projects.

    ``project_value=None`` means the publisher did not supply a value.  We
    treat this as fail-closed Tier-4 (confirm-required) rather than defaulting
    to 0.0 (which would grant autonomous execution on any project without a
    declared value).
    """
    engine = _get_registered_engine()
    if engine is None:
        log.error(
            "[automation] no engine registered — cannot sync bom tasks for bom=%s",
            bom_line_label,
        )
        return {"ok": False, "error": "engine_not_registered"}

    # Fail-closed: missing project_value → Tier-4 (confirm required).
    # A publisher omitting the value cannot accidentally gain autonomous execution.
    if project_value is None:
        log.warning(
            "[automation] project_value not provided for bom=%s — defaulting to Tier-4 "
            "(confirm required, fail-closed). Publisher must supply project_value explicitly.",
            bom_line_label,
        )
        effective_value: float = _TIER_FAILCLOSED_VALUE
    else:
        effective_value = project_value

    if effective_value < 0.0:
        log.error(
            "[automation] invalid negative project_value=%.2f for bom=%s — failing closed",
            effective_value,
            bom_line_label,
        )
        return {"ok": False, "error": "invalid_project_value"}

    tier = resolve_tier(effective_value)
    # Autonomous = Tier 1 (confirm_required=False).  All other tiers → confirm-first.
    confirm: bool = not tier["confirm_required"]

    idempotency_key = f"bom_sync:{namespace_id}:{bom_line_label}:{status}"

    log.info(
        "[automation] tier=%d label=%r confirm=%s project=%s bom=%s status=%s value=%.2f",
        tier["tier"],
        tier["label"],
        confirm,
        project_id,
        bom_line_label,
        status,
        effective_value,
    )

    # Open a scoped session for @governed's dedup + audit writes.
    # do_sync_bom_tasks opens its own session for domain writes.
    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        return await _do_governed_bom_sync(
            conn,
            namespace_id,
            idempotency_key=idempotency_key,
            confirm=confirm,
            value=effective_value,
            redis_client=redis_client,
            engine=engine,
            project_id=project_id,
            bom_line_label=bom_line_label,
            status=status,
        )


# ---------------------------------------------------------------------------
# RQ task: Procurement (PO_LINE.status_changed) trigger
# ---------------------------------------------------------------------------


def rq_sync_bom_on_po(
    *,
    namespace_id: str,
    project_id: str,
    bom_line_label: str,
    status: str,
    project_value: float | None = None,
    redis_client: Any = None,
) -> dict[str, Any]:
    """RQ worker task: Procurement PO_LINE.status_changed → do_sync_bom_tasks.

    Fired by the C4 outbox relay after a Procurement graph event commits.
    Tier-gates via ``@governed`` — Tier-1 self-executes, Tiers 2–4 pend for
    human approval.

    Parameters
    ----------
    namespace_id:
        Tenant UUID (string — RQ serialises as JSON; validated here).
    project_id:
        PROJECT label (e.g. ``"PROJECT:Q001"``).
    bom_line_label:
        BOM_LINE label (e.g. ``"BOM_LINE:Q001:AMP01"``).
    status:
        New PO status forwarded as BOM_LINE status context.
    project_value:
        Estimated contract value used to resolve the autonomy tier.
        ``None`` (default) means "not provided" → fail-closed Tier-4.
        Pass ``0.0`` explicitly for a legitimate zero-value project (Tier 1).
    redis_client:
        Optional Redis client for the kill-switch gate.  When ``None`` the
        registered client (see ``register_redis_client``) is used.  When
        neither is set the gate is skipped (warning logged).
    """
    try:
        ns_uuid = UUID(namespace_id)
    except (ValueError, AttributeError) as exc:
        log.error("[automation.po] invalid namespace_id=%r: %s", namespace_id, exc)
        return {"ok": False, "error": f"invalid_namespace_id: {exc}"}

    # Resolve Redis client: caller-supplied takes precedence; fall back to registry.
    effective_redis = redis_client if redis_client is not None else _get_registered_redis_client()

    log.info(
        "[automation.po] project=%s bom=%s status=%s value=%s",
        project_id,
        bom_line_label,
        status,
        project_value,
    )

    return _run_sync(
        _run_governed_sync(
            namespace_id=ns_uuid,
            project_id=project_id,
            bom_line_label=bom_line_label,
            status=status,
            project_value=project_value,
            redis_client=effective_redis,
        )
    )


# ---------------------------------------------------------------------------
# RQ task: Warehouse (GOODS_RECEIPT.created) trigger
# ---------------------------------------------------------------------------


def rq_sync_bom_on_goods_receipt(
    *,
    namespace_id: str,
    project_id: str,
    bom_line_label: str,
    status: str = "DELIVERED",
    project_value: float | None = None,
    redis_client: Any = None,
) -> dict[str, Any]:
    """RQ worker task: Warehouse GOODS_RECEIPT.created → do_sync_bom_tasks.

    A goods-receipt event signals delivery; the BOM_LINE status is advanced to
    ``DELIVERED`` by default (callers may override if a different mapping
    applies).  Tier-gated identically to the PO trigger.

    Parameters
    ----------
    project_value:
        ``None`` (default) means "not provided" → fail-closed Tier-4.
        Pass ``0.0`` explicitly for a legitimate zero-value project (Tier 1).
    redis_client:
        Optional Redis client for the kill-switch gate.  When ``None`` the
        registered client (see ``register_redis_client``) is used.  When
        neither is set the gate is skipped (warning logged).
    """
    try:
        ns_uuid = UUID(namespace_id)
    except (ValueError, AttributeError) as exc:
        log.error("[automation.goods_receipt] invalid namespace_id=%r: %s", namespace_id, exc)
        return {"ok": False, "error": f"invalid_namespace_id: {exc}"}

    # Resolve Redis client: caller-supplied takes precedence; fall back to registry.
    effective_redis = redis_client if redis_client is not None else _get_registered_redis_client()

    log.info(
        "[automation.goods_receipt] project=%s bom=%s status=%s value=%s",
        project_id,
        bom_line_label,
        status,
        project_value,
    )

    return _run_sync(
        _run_governed_sync(
            namespace_id=ns_uuid,
            project_id=project_id,
            bom_line_label=bom_line_label,
            status=status,
            project_value=project_value,
            redis_client=effective_redis,
        )
    )


# ---------------------------------------------------------------------------
# C4 subscribers — fire RQ tasks on Procurement / Warehouse events
# ---------------------------------------------------------------------------


async def _handle_po_status_changed(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    event: dict[str, Any],
) -> PostCommitAction | None:
    """Outbox handler: PO_LINE.status_changed → enqueue RQ task.

    We enqueue rather than execute inline so the relay's transaction commits
    first (at-least-once post-commit semantics).  The enqueued RQ task runs
    the full tier-gated ``@governed`` flow.

    If RQ is unavailable we log and return gracefully (degraded mode — the
    C4 relay's at-least-once retry will re-deliver).
    """
    payload = _extract_payload(event)
    namespace_id = str(event.get("namespace_id") or payload.get("namespace", ""))
    project_id: str = payload.get("project_id", "")
    bom_line_label: str = payload.get("bom_line_label", "") or payload.get("id", "")
    status: str = payload.get("status", "")
    # Fail-closed: absent project_value key → None → Tier-4 in the RQ task.
    # float(payload.get("project_value", 0.0)) was the prior bug: a publisher
    # omitting the key defaulted to 0.0 (Tier-1 autonomous).
    raw_value = payload.get("project_value")
    project_value: float | None = float(raw_value) if raw_value is not None else None

    if not (namespace_id and project_id and bom_line_label and status):
        log.warning(
            "[automation.po] incomplete event — skipping. ns=%s project=%s bom=%s status=%s",
            namespace_id,
            project_id,
            bom_line_label,
            status,
        )
        return None

    # Captured in the closure and fired by the relay AFTER its transaction
    # commits (M0.W20d). Enqueuing here would do blocking Redis I/O while
    # the relay still holds its DB connection open -- measured at 21.06s of
    # event-loop starvation before round 2 moved it off the transaction.
    return lambda: _enqueue_rq_task(
        rq_sync_bom_on_po,
        namespace_id=namespace_id,
        project_id=project_id,
        bom_line_label=bom_line_label,
        status=status,
        project_value=project_value,
    )


async def _handle_goods_receipt_created(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    event: dict[str, Any],
) -> PostCommitAction | None:
    """Outbox handler: GOODS_RECEIPT.created → enqueue RQ task."""
    payload = _extract_payload(event)
    namespace_id = str(event.get("namespace_id") or payload.get("namespace", ""))
    project_id: str = payload.get("project_id", "")
    bom_line_label: str = payload.get("bom_line_label", "") or payload.get("id", "")
    status: str = payload.get("status", "DELIVERED")
    # Fail-closed: absent project_value key → None → Tier-4 in the RQ task.
    raw_value_gr = payload.get("project_value")
    project_value: float | None = float(raw_value_gr) if raw_value_gr is not None else None

    if not (namespace_id and project_id and bom_line_label):
        log.warning(
            "[automation.goods_receipt] incomplete event — skipping. ns=%s project=%s bom=%s",
            namespace_id,
            project_id,
            bom_line_label,
        )
        return None

    # Post-commit, for the reason in _handle_po_status_changed above.
    return lambda: _enqueue_rq_task(
        rq_sync_bom_on_goods_receipt,
        namespace_id=namespace_id,
        project_id=project_id,
        bom_line_label=bom_line_label,
        status=status,
        project_value=project_value,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Return a dict from the event payload, parsing JSON strings."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            payload = {}
    return payload  # type: ignore[return-value]


def _enqueue_rq_task(fn: Any, **kwargs: Any) -> None:
    """Enqueue *fn* on the default RQ queue.

    Gracefully degrades when RQ / Redis is unavailable: logs the error and
    returns.

    🔴 It does NOT get retried. This used to claim "the relay's at-least-once
    delivery will retry via the outbox", which is false: since M0.W20d the
    enqueue runs as a POST-COMMIT action, so by the time it can fail the event
    is already marked published and the outbox will never re-deliver it. It was
    misleading before that too — this function swallows the exception, so the
    handler returned success and the dedup row committed either way. A dropped
    enqueue is logged and lost; treat the log line as the only signal.
    """
    try:
        from rq import Queue  # type: ignore[import-untyped]

        from nce.config import cfg

        redis_url = getattr(cfg, "REDIS_URL", None) or "redis://localhost:6379"
        from redis import Redis  # type: ignore[import-untyped]

        redis_conn = Redis.from_url(redis_url)
        q = Queue(connection=redis_conn)
        q.enqueue(fn, **kwargs)
        log.debug("[automation] enqueued %s kwargs=%s", fn.__name__, list(kwargs))
    except Exception as exc:
        log.error(
            "[automation] Failed to enqueue %s (degraded mode): %s",
            fn.__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# Subscriber registration
# ---------------------------------------------------------------------------


def register_automation_subscribers() -> None:
    """Register Procurement and Warehouse C4 bus handlers.

    Call once at startup (after ``register_engine``).  Idempotent.
    """
    subscribe(
        {"node_type": _PO_LINE_NODE_TYPE, "op": _PO_LINE_OP},
        _handle_po_status_changed,
    )
    subscribe(
        {"node_type": _GOODS_RECEIPT_NODE_TYPE, "op": _GOODS_RECEIPT_OP},
        _handle_goods_receipt_created,
    )
    log.info(
        "[automation] subscribed to %s.%s and %s.%s via C4 bus",
        _PO_LINE_NODE_TYPE,
        _PO_LINE_OP,
        _GOODS_RECEIPT_NODE_TYPE,
        _GOODS_RECEIPT_OP,
    )
