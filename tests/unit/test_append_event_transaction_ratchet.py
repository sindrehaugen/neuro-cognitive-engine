"""Static AST ratchet verifying all append_event calls are enclosed in database transactions.

Ensures that:
1. Every ``append_event(...)`` call under ``nce/`` is lexically enclosed by an
   ``async with conn.transaction():`` or ``async with scoped_pg_session(...)`` block,
   preventing runtime EventLogError failures in production.
2. Any legitimate exception (e.g. helper functions receiving an already-transactional
   connection from their caller) is explicitly documented in a shrink-only allowlist
   with an owner, reason (>= 60 chars), and caller transaction guarantee.
3. Standing positive controls (U18) verify the scanner detects un-transactioned calls
   and passes validly enclosed calls.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_NCE_DIR: Final[Path] = _REPO_ROOT / "nce"

# Shrink-only allowlist of helper functions that accept a pre-existing transaction
# connection from their caller rather than opening one lexically.
# Every entry must have an owner, a reason >= 60 chars, and caller_guarantee >= 60 chars.
KNOWN_UNENCLOSED_APPEND_EVENT_SITES: Final[dict[str, dict[str, str]]] = {
    "nce/a2a.py::_append_a2a_event": {
        "owner": "core-a2a",
        "reason": "A2A communication audit helper receives conn from caller and is invoked exclusively inside transactions managed by a2a_server handlers.",
        "caller_guarantee": "Callers in nce/a2a_server.py open an explicit transaction or run within scoped_pg_session before invoking _append_a2a_event.",
    },
    "nce/atms.py::floor_retracted_kg_edges": {
        "owner": "core-atms",
        "reason": "ATMS retracted edge retention floor helper accepts an active transaction connection managed by maintenance transaction contexts.",
        "caller_guarantee": "Scheduled maintenance caller in cron/atms tasks acquires connection and manages outer transaction lifecycle.",
    },
    "nce/consolidation.py::_store_consolidated_memory": {
        "owner": "core-consolidation",
        "reason": "Sleep-consolidation memory storage helper is called inside the multi-statement transaction opened by trigger_consolidation.",
        "caller_guarantee": "Outer consolidation flow opens multi-statement asyncpg transaction for atomic memory consolidation and event logging.",
    },
    "nce/replay.py::_dispatch_and_apply_event": {
        "owner": "core-replay",
        "reason": "Replay event dispatch and projection loop executes event handlers inside dedicated per-event replay transaction contexts.",
        "caller_guarantee": "The replayer framework maintains an active database transaction across the event application lifecycle.",
    },
    "nce/autonomy/governor.py::_audit_execution": {
        "owner": "core-autonomy",
        "reason": "Autonomous governance execution auditor receives conn from @governed decorator which explicitly verifies conn.is_in_transaction().",
        "caller_guarantee": "@governed decorator at governor.py:271 explicitly raises GovernanceError if not conn.is_in_transaction().",
    },
    "nce/orchestrators/cognitive.py::_reopen_superseded_consolidations": {
        "owner": "core-orchestrator",
        "reason": "Cognitive orchestrator superseded consolidation reopening helper receives conn from outer scoped transaction in consolidation tasks.",
        "caller_guarantee": "Parent cognitive consolidation flow runs within an active tenant transaction session.",
    },
    "nce/orchestrators/memory.py::_insert_graph_nodes_and_edges": {
        "owner": "core-memory",
        "reason": "Memory orchestrator graph node and edge insertion helper receives an active transactional connection from the memory ingestion pipeline.",
        "caller_guarantee": "Memory pipeline wraps all vector, document, and graph writes in an enclosing atomic transaction.",
    },
    "nce/vertical_modules/dynamics365/ingestion.py::ingest_sla_breach": {
        "owner": "dynamics365",
        "reason": "Dynamics 365 SLA breach ingestion helper requires caller to supply an active asyncpg transaction connection as documented in docstring.",
        "caller_guarantee": "Docstring at ingestion.py:272 explicitly specifies caller must invoke inside an existing asyncpg transaction.",
    },
    "nce/vertical_modules/inventory/restock_po.py::_audit_restock_span": {
        "owner": "inventory",
        "reason": "Inventory restock PO correlation span auditor receives conn from restock tool handler executing inside scoped_pg_session transaction.",
        "caller_guarantee": "inventory_create_restock_po executes within scoped_pg_session which enforces conn.transaction() at db_utils.py:246.",
    },
    "nce/vertical_modules/procurement/po.py::_audit_rebate_decision": {
        "owner": "procurement",
        "reason": "Procurement PO rebate decision auditor receives active transactional connection from purchase order generation flow.",
        "caller_guarantee": "Caller in procurement purchase order mutation pipeline executes within scoped_pg_session transaction.",
    },
    "nce/vertical_modules/project/advance.py::_append_phase_transition_event": {
        "owner": "project",
        "reason": "Project phase gate transition auditor receives active transactional connection from advance_phase tool execution context.",
        "caller_guarantee": "Caller in project gate advance handler runs within an active scoped_pg_session transaction.",
    },
    "nce/vertical_modules/system_design/mcp_handlers.py::_emit_authoring_event": {
        "owner": "system_design",
        "reason": "System design device authoring event emitter receives active transactional connection from mutating device tool handlers.",
        "caller_guarantee": "Authoring handlers in system design vertical module execute within an active scoped_pg_session transaction.",
    },
}


def _get_enclosing_function_name(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> str:
    curr: ast.AST | None = node
    while curr in parent_map:
        curr = parent_map[curr]
        if isinstance(curr, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return curr.name
    return "<module>"


def _scan_append_event_calls(source_tree: ast.AST, rel_file_path: str) -> list[dict[str, Any]]:
    """Analyze all append_event call sites in an AST tree for transaction enclosures."""
    parent_map: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(source_tree):
        for child in ast.iter_child_nodes(node):
            parent_map[child] = node

    call_sites: list[dict[str, Any]] = []
    for node in ast.walk(source_tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr

            if name == "append_event":
                # Check for lexical enclosing transaction context
                curr: ast.AST | None = node
                enclosing_tx = False
                enclosing_contexts: list[str] = []
                while curr in parent_map:
                    curr = parent_map[curr]
                    if isinstance(curr, ast.AsyncWith):
                        for item in curr.items:
                            call_repr = ast.unparse(item.context_expr)
                            enclosing_contexts.append(call_repr)
                            if (
                                "transaction(" in call_repr
                                or call_repr.endswith(".transaction")
                                or "scoped_pg_session(" in call_repr
                            ):
                                enclosing_tx = True

                fn_name = _get_enclosing_function_name(node, parent_map)
                call_sites.append(
                    {
                        "file": rel_file_path,
                        "function": fn_name,
                        "site_id": f"{rel_file_path}::{fn_name}",
                        "lineno": getattr(node, "lineno", 0),
                        "enclosed_in_transaction": enclosing_tx,
                        "enclosing_contexts": enclosing_contexts,
                    }
                )
    return call_sites


def _collect_all_append_event_calls() -> list[dict[str, Any]]:
    all_calls: list[dict[str, Any]] = []
    for py_path in sorted(_NCE_DIR.rglob("*.py")):
        rel_path = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_bytes())
        except Exception:
            continue
        all_calls.extend(_scan_append_event_calls(tree, rel_path))
    return all_calls


def test_every_append_event_is_in_transaction_or_allowlisted() -> None:
    """Every append_event call must be enclosed in a transaction or allowlisted."""
    all_calls = _collect_all_append_event_calls()
    unenclosed = [c for c in all_calls if not c["enclosed_in_transaction"]]

    unaccounted: list[str] = []
    for c in unenclosed:
        if c["site_id"] not in KNOWN_UNENCLOSED_APPEND_EVENT_SITES:
            unaccounted.append(f"{c['site_id']} (line {c['lineno']})")

    assert not unaccounted, (
        f"Found {len(unaccounted)} append_event calls without enclosing transaction: {unaccounted}. "
        "Wrap calls in 'async with conn.transaction():' or 'async with scoped_pg_session(...):'."
    )


def test_append_event_allowlist_is_shrink_only_and_reasoned() -> None:
    """The allowlist must be shrink-only and every entry substantiated."""
    all_calls = _collect_all_append_event_calls()
    unenclosed_site_ids = {c["site_id"] for c in all_calls if not c["enclosed_in_transaction"]}

    # 1. Shrink-only: every allowlisted site must currently have an unenclosed call
    stale_entries = set(KNOWN_UNENCLOSED_APPEND_EVENT_SITES.keys()) - unenclosed_site_ids
    assert not stale_entries, (
        f"Allowlist contains entries that are now enclosed in transactions: {stale_entries}. "
        "Remove them to shrink the allowlist."
    )

    # 2. Substantive documentation contracts
    for site_id, meta in KNOWN_UNENCLOSED_APPEND_EVENT_SITES.items():
        assert "owner" in meta and meta["owner"], f"{site_id} missing owner"
        assert "reason" in meta and len(meta["reason"]) >= 60, (
            f"{site_id} reason must be >= 60 chars, got {len(meta.get('reason', ''))}"
        )
        assert "caller_guarantee" in meta and len(meta["caller_guarantee"]) >= 60, (
            f"{site_id} caller_guarantee must be >= 60 chars, got {len(meta.get('caller_guarantee', ''))}"
        )


def test_discovery_floor_for_append_event_scanner() -> None:
    """Guard-the-guard: verify AST discovery resolves at least 45 append_event calls."""
    all_calls = _collect_all_append_event_calls()
    assert len(all_calls) >= 45, (
        f"AST discovery floor breached: expected >= 45 calls, found {len(all_calls)}"
    )


def test_positive_control_fails_on_unenclosed_append_event() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner flags unenclosed append_event."""
    bad_code = """
async def bad_worker(pool, ns_uuid):
    async with pool.acquire() as conn:
        await append_event(
            conn=conn,
            namespace_id=ns_uuid,
            agent_id="test",
            event_type="test_event",
            params={},
        )
"""
    tree = ast.parse(bad_code)
    calls = _scan_append_event_calls(tree, "nce/synthetic_worker.py")
    assert len(calls) == 1
    assert not calls[0]["enclosed_in_transaction"]


def test_positive_control_passes_on_enclosed_append_event() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner passes transaction-enclosed append_event."""
    good_code = """
async def good_worker(pool, ns_uuid):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn=conn,
                namespace_id=ns_uuid,
                agent_id="test",
                event_type="test_event",
                params={},
            )

async def scoped_worker(pool, ns_uuid):
    async with scoped_pg_session(pool, ns_uuid) as conn:
        await append_event(
            conn=conn,
            namespace_id=ns_uuid,
            agent_id="test",
            event_type="test_event",
            params={},
        )
"""
    tree = ast.parse(good_code)
    calls = _scan_append_event_calls(tree, "nce/synthetic_worker.py")
    assert len(calls) == 2
    assert all(c["enclosed_in_transaction"] for c in calls)
