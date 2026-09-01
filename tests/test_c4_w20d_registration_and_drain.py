"""
tests/test_c4_w20d_registration_and_drain.py
============================================
Acceptance tests for **M0.W20d — c4-drain-hardening**.

Module 7's reactive automation ships a subscriber half, RQ tasks, tests and
docstrings for a bridge with **no far side**. Verified by AST against this
tree, counting real call expressions rather than docstring mentions:

    register_system_design_subscribers   2 call sites   (the pattern)
    register_automation_subscribers      0
    register_bom_task_subscriber         0
    register_engine  (both definitions)  0

So three C4 selectors have handlers that no process ever registers, and the
two engine registries the handlers read at delivery time are never written.

Scope, and what is deliberately NOT done
----------------------------------------
W20d's ledger row says "restore round 2's engine-registration + post-commit RQ
enqueue **and rebuild the drain** without the cancellation window". Measured
against this tree, the third of those is already satisfied and **rebuilding it
is what failed review twice**:

* ``run_outbox_relay_once``'s post-commit drain is a plain ``for`` loop calling
  ``action()`` synchronously. asyncio can only cancel at an ``await``, so a
  synchronous drain has no cancellation window *inside* it. W20a's row records
  this as "byte-identical to HEAD, md5-verified, zero await points".
* Round 2 rebuilt that loop, added ``await`` points where main had none, and
  ``mcp_stdio_main.py`` cancels the relay task on every graceful SIGTERM — so a
  cancel mid-drain lost already-published events' actions with no DLQ row
  (measured then: ``actions_fired=1, published=5, DLQ=0``).

**Therefore this wave does not rebuild the drain. It ratchets the property that
made it safe**, so a future change cannot quietly reintroduce awaits.

One residual window is real and IS closed here: the drain sits *after*
``async with pool.acquire()``, whose ``__aexit__`` awaits. A cancel delivered
during that release skips the drain entirely while the events are already
marked published — the same loss shape, one frame earlier. Draining in a
``finally`` (gated on the transaction having committed) removes it without
adding a single await.

The post-commit mechanism itself already exists and ``handle_memory_stored``
already uses it; Module 7's two handlers simply never adopted it, and their
docstrings claim post-commit semantics they do not have.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

_CRON = pathlib.Path("nce/cron.py")
_STDIO = pathlib.Path("nce/mcp_stdio_main.py")
_RELAY = pathlib.Path("nce/outbox_relay.py")
_AUTOMATION = pathlib.Path("nce/vertical_modules/project/automation.py")

#: Every registrar a relay-running process must call, with the reason.
_REQUIRED_REGISTRARS: dict[str, str] = {
    "register_system_design_subscribers": "System Design authoring events",
    "register_automation_subscribers": "PO_LINE.status_changed + GOODS_RECEIPT.created",
    "register_bom_task_subscriber": "BOM_LINE.status_changed",
}


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_bytes().decode("utf-8"))


def _fn(tree: ast.Module, name: str) -> ast.AST:
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )
    assert node is not None, f"{name} not found"
    return node


def _call_lines(scope: ast.AST, name: str) -> list[int]:
    """Line numbers of real *call expressions* to *name* — not docstring text."""
    return [
        n.lineno
        for n in ast.walk(scope)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


# ---------------------------------------------------------------------------
# 1. Registration — both relay-running processes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("registrar", sorted(_REQUIRED_REGISTRARS))
def test_stdio_process_registers_every_c4_subscriber(registrar: str) -> None:
    """OUTBOX_HANDLERS is per-process state; the MCP process runs a relay."""
    fn = _fn(_parse(_STDIO), "run_stdio_server")
    assert _call_lines(fn, registrar), (
        f"run_stdio_server never calls {registrar}() — its selectors "
        f"({_REQUIRED_REGISTRARS[registrar]}) have no handler in the MCP process, "
        "so every such event it polls fast-fails to the DLQ"
    )


@pytest.mark.parametrize("registrar", sorted(_REQUIRED_REGISTRARS))
def test_cron_process_registers_every_c4_subscriber(registrar: str) -> None:
    """cron is the SECOND process running the relay — registering in only one
    leaves the other dead-lettering every event it happens to poll first."""
    tree = _parse(_CRON)
    hits = _call_lines(tree, registrar)
    assert hits, (
        f"nce/cron.py never calls {registrar}() — cron also runs the outbox relay, "
        "and OUTBOX_HANDLERS is per-process state"
    )


def test_both_processes_register_an_engine_for_the_handlers_that_need_one() -> None:
    """``_handle_bom_line_status_changed`` reads the engine registry at delivery.

    Without a registration it raises ``EngineNotRegisteredError`` — which is
    loud and correct, but means the handler can never do its job.
    """
    for path, fname in ((_STDIO, "run_stdio_server"), (_CRON, None)):
        tree = _parse(path)
        scope: ast.AST = tree if fname is None else _fn(tree, fname)
        assert _call_lines(scope, "register_engine"), (
            f"{path} never calls register_engine(...) — the BOM_LINE handler "
            "dead-letters every event with EngineNotRegisteredError"
        )


def test_registration_precedes_the_relay_in_the_stdio_process() -> None:
    """Ordering is load-bearing: the first poll can race registration."""
    fn = _fn(_parse(_STDIO), "run_stdio_server")
    relay = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "create_tracked_task"
        and any(
            isinstance(kw.value, ast.Constant) and kw.value.value == "outbox_relay_loop"
            for kw in n.keywords
        )
    ]
    assert relay, "the outbox_relay_loop task was not found"
    for registrar in _REQUIRED_REGISTRARS:
        lines = _call_lines(fn, registrar)
        assert lines and min(lines) < min(relay), (
            f"{registrar}() runs AFTER the relay loop task is created — the first "
            "poll can race it and dead-letter live events"
        )


def test_registration_precedes_the_relay_job_in_cron() -> None:
    tree = _parse(_CRON)
    add_job = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "add_job"
        and any(
            kw.arg == "id"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "outbox_relay"
            for kw in n.keywords
        )
    ]
    assert add_job, "the outbox_relay scheduler job was not found"
    for registrar in _REQUIRED_REGISTRARS:
        lines = _call_lines(tree, registrar)
        assert lines and min(lines) < min(add_job), (
            f"{registrar}() runs AFTER the outbox_relay job is scheduled"
        )


# ---------------------------------------------------------------------------
# 2. The drain — ratchet the property, do not rebuild the loop
# ---------------------------------------------------------------------------


def _drain_loop(tree: ast.Module) -> ast.For:
    fn = _fn(tree, "run_outbox_relay_once")
    loops = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.Name)
        and n.iter.id == "post_commit_actions"
    ]
    assert len(loops) == 1, f"expected exactly one post-commit drain loop, found {len(loops)}"
    return loops[0]


def test_the_post_commit_drain_contains_no_await() -> None:
    """The property that makes the drain uncancellable — ratcheted.

    Round 2 rebuilt this loop with ``await`` points and a graceful SIGTERM then
    lost already-published events' actions with no DLQ row. A synchronous loop
    cannot be cancelled part-way, because asyncio only cancels at an await.
    """
    loop = _drain_loop(_parse(_RELAY))
    awaits = [n for n in ast.walk(loop) if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
    assert not awaits, (
        "the post-commit drain contains an await — a cancel delivered mid-drain "
        "loses the remaining actions for events that are already marked "
        "published, with no DLQ row. Keep the drain synchronous, or shield it."
    )


def test_the_drain_runs_even_if_the_task_is_cancelled_releasing_the_connection() -> None:
    """The residual window: ``async with pool.acquire()``'s __aexit__ awaits.

    A cancel there skips a drain that sits after the ``async with``, while the
    events are already committed as published. Draining in a ``finally``
    removes it without adding an await.
    """
    # ONE parse: AST nodes from separate parses are distinct objects, so
    # identity containment across two trees can never match.
    tree = _parse(_RELAY)
    fn = _fn(tree, "run_outbox_relay_once")
    drain = _drain_loop(tree)
    enclosing_finally = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Try)
        and any(drain is d for stmt in node.finalbody for d in ast.walk(stmt))
    ]
    assert enclosing_finally, (
        "the post-commit drain is not inside a try/finally, so a CancelledError "
        "raised while releasing the pool connection skips it entirely — the "
        "events are already marked published and their actions are lost"
    )


# ---------------------------------------------------------------------------
# 3. Post-commit enqueue — Module 7's handlers must adopt the mechanism
# ---------------------------------------------------------------------------


def _event(payload: dict[str, Any]) -> dict[str, Any]:
    return {"namespace_id": "ns-1", "payload": payload, "event_type": "x", "aggregate_id": "a"}


@pytest.mark.asyncio
async def test_goods_receipt_handler_returns_a_post_commit_action() -> None:
    """It must not touch Redis inside the relay's open transaction."""
    from nce.vertical_modules.project import automation

    payload = {
        "namespace": "00000000-0000-4000-8000-000000000001",
        "project_id": "PROJECT:p1",
        "bom_line_label": "BOM_LINE:b1",
        "status": "DELIVERED",
    }
    with patch.object(automation, "_enqueue_rq_task") as enqueue:
        action = await automation._handle_goods_receipt_created(None, _event(payload))
        assert enqueue.call_count == 0, (
            "the handler enqueued to RQ inside the transaction — that is the "
            "blocking Redis call that starved the event loop (21.06s -> 0.0146s)"
        )
    assert callable(action), "handler must return a zero-arg post-commit callable"
    with patch.object(automation, "_enqueue_rq_task") as enqueue:
        action()
        assert enqueue.call_count == 1, "the returned action must do the enqueue"


@pytest.mark.asyncio
async def test_po_status_handler_returns_a_post_commit_action() -> None:
    from nce.vertical_modules.project import automation

    payload = {
        "namespace": "00000000-0000-4000-8000-000000000001",
        "project_id": "PROJECT:p1",
        "bom_line_label": "BOM_LINE:b1",
        "status": "ORDERED",
    }
    with patch.object(automation, "_enqueue_rq_task") as enqueue:
        action = await automation._handle_po_status_changed(None, _event(payload))
        assert enqueue.call_count == 0
    assert callable(action)


@pytest.mark.asyncio
async def test_an_incomplete_event_still_returns_none_rather_than_an_action() -> None:
    """The skip branch is unchanged: nothing to enqueue means no action."""
    from nce.vertical_modules.project import automation

    action = await automation._handle_goods_receipt_created(
        None, _event({"namespace": "ns", "project_id": "", "bom_line_label": ""})
    )
    assert action is None


def test_neither_handler_enqueues_inline_any_more() -> None:
    """Ratchet: a future edit may not move the enqueue back into the handler."""
    tree = _parse(_AUTOMATION)
    for name in ("_handle_po_status_changed", "_handle_goods_receipt_created"):
        fn = _fn(tree, name)
        direct = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_enqueue_rq_task"
        ]
        # A call is allowed ONLY inside the returned closure (a Lambda/FunctionDef
        # nested in the handler), never in the handler's own body.
        nested = {
            id(n)
            for sub in ast.walk(fn)
            if isinstance(sub, (ast.Lambda, ast.FunctionDef))
            for n in ast.walk(sub)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_enqueue_rq_task"
        }
        inline = [n for n in direct if id(n) not in nested]
        assert not inline, (
            f"{name} calls _enqueue_rq_task() directly in its body — blocking "
            "Redis I/O inside the relay's open transaction"
        )
