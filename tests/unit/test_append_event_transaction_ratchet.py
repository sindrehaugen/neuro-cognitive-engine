"""Static AST ratchet verifying event append and publish transaction atomicity and chokepoint integrity.

Ensures that:
1. Every ``append_event(...)`` call under ``nce/`` is lexically enclosed by an
   ``async with conn.transaction():`` or ``async with scoped_pg_session(...)`` block,
   preventing runtime EventLogError failures in production.
2. Every ``publish(...)`` call from ``nce.events.bus`` under ``nce/`` is lexically
   enclosed by an active database transaction context, preserving the outbox
   transactional atomicity contract documented in ``nce/events/bus.py:47``.
3. Direct ``INSERT INTO event_log`` writes outside the designated owner
   ``nce/event_log.py::_insert_event`` are strictly prohibited (the chokepoint
   ratchet), preventing sequence allocation and Merkle signature bypasses.
4. Legitimate exceptions (helper functions receiving an already-transactional
   connection from their caller) are explicitly documented in shrink-only
   allowlists with an owner, a reason (>= 60 chars), and a caller transaction
   guarantee (>= 60 chars).
5. Standing positive controls (U18) verify the scanners detect un-transactioned
   calls and chokepoint violations, pass validly enclosed calls, and correctly
   discriminate event-bus publish calls from unrelated pub/sub interfaces.

Census Metrics & Discovery Floors:
----------------------------------
- ``append_event`` call sites: 68 measured across nce/ (discovery floor >= 45).
- ``publish`` call sites: 9 measured across nce/ (discovery floor >= 6 with slack):
  * 6 transaction-enclosed outbox publishers in vertical modules.
  * 3 unenclosed outbox helper functions deferring transaction to caller (allowlisted).
  * Unrelated non-bus publish calls (e.g. Redis pub/sub cache invalidation) are
    scoped out by AST callee and import analysis.
- ``INSERT INTO event_log`` statements: 1 owning statement discovered in
  ``nce/event_log.py::_insert_event`` (line 1108); 0 offenders outside.
  Note: ``nce/event_log.py:1148`` contains an exception message string
  (``raise EventLogError("INSERT INTO event_log returned no RETURNING row...")``)
  which the scanner correctly ignores as a non-SQL string literal.
  Discovery floor asserts >= 1 owning statement found.
"""

from __future__ import annotations

import ast
import re
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

# Shrink-only allowlist of outbox publish call sites that are not lexically enclosed in
# transactions. These consist of outbox helpers deferring transaction to callers.
KNOWN_UNENCLOSED_PUBLISH_SITES: Final[dict[str, dict[str, str]]] = {
    "nce/events/emit.py::emit_graph_write": {
        "owner": "core-events",
        "reason": "Graph-write event emitter receives active transactional connection from callers across vertical modules to emit aggregate mutation events to outbox.",
        "caller_guarantee": "Callers in vertical modules (e.g. assets, procurement, support) manage the outer database transaction via scoped_pg_session or conn.transaction().",
    },
    "nce/events/emit.py::emit_status_change": {
        "owner": "core-events",
        "reason": "Status-change graph-write event emitter receives active transactional connection from callers to emit aggregate status transitions to outbox.",
        "caller_guarantee": "Domain callers advancing entity status lifecycle manage outer transaction lifecycle via scoped_pg_session or conn.transaction() before invoking.",
    },
    "nce/vertical_modules/procurement/po_line.py::update_po_line_status": {
        "owner": "procurement",
        "reason": "PO line status transition helper performs relational updates and emits status_changed outbox event using connection supplied by calling workflow.",
        "caller_guarantee": "Invoked by purchase order workflow handlers and goods receipt processors that maintain an active asyncpg transaction across the state change.",
    },
}

_EVENT_LOG_INSERT_SQL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bINSERT\s+INTO\s+(?:public\.)?event_log\b\s*(\(|VALUES\b|SELECT\b)",
    re.IGNORECASE,
)


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent_map: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[child] = node
    return parent_map


def _get_enclosing_function_name(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> str:
    curr: ast.AST | None = node
    while curr in parent_map:
        curr = parent_map[curr]
        if isinstance(curr, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return curr.name
    return "<module>"


def _is_docstring(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> bool:
    parent = parent_map.get(node)
    if isinstance(parent, ast.Expr):
        grandparent = parent_map.get(parent)
        if isinstance(
            grandparent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if getattr(grandparent, "body", None) and grandparent.body[0] is parent:
                return True
    return False


def _has_events_bus_publish_import(tree: ast.AST) -> bool:
    """Check if an AST module imports publish from nce.events.bus."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "nce.events.bus":
                for alias in node.names:
                    if alias.name == "publish":
                        return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "nce.events.bus":
                    return True
    return False


def _scan_append_event_calls(source_tree: ast.AST, rel_file_path: str) -> list[dict[str, Any]]:
    """Analyze all append_event call sites in an AST tree for transaction enclosures."""
    parent_map = _build_parent_map(source_tree)

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


def _scan_publish_calls(source_tree: ast.AST, rel_file_path: str) -> list[dict[str, Any]]:
    """Analyze all outbox publish call sites in an AST tree for transaction enclosures.

    Scopes strictly to nce.events.bus.publish by requiring a verified import from
    nce.events.bus for bare publish() calls, or receiver gating on bus/event_bus
    for attribute calls. Unrelated interfaces (e.g. active_redis.publish) are ignored.
    """
    if rel_file_path == "nce/events/bus.py":
        return []

    has_bus_import = _has_events_bus_publish_import(source_tree)
    parent_map = _build_parent_map(source_tree)

    call_sites: list[dict[str, Any]] = []
    for node in ast.walk(source_tree):
        if isinstance(node, ast.Call):
            fn = node.func
            is_bus_publish = False
            if isinstance(fn, ast.Name) and fn.id == "publish":
                if has_bus_import:
                    is_bus_publish = True
            elif isinstance(fn, ast.Attribute) and fn.attr == "publish":
                if isinstance(fn.value, ast.Name) and fn.value.id in ("bus", "event_bus"):
                    is_bus_publish = True

            if is_bus_publish:
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


def _scan_event_log_chokepoint_insert_statements(
    source_tree: ast.AST, rel_file_path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scan AST tree for SQL statements executing direct INSERT INTO event_log.

    Exempts the sanctioned owner: nce/event_log.py::_insert_event.
    Ignores non-SQL string literals such as exception messages.
    """
    parent_map = _build_parent_map(source_tree)
    owning: list[dict[str, Any]] = []
    offenders: list[dict[str, Any]] = []

    for node in ast.walk(source_tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_docstring(node, parent_map):
                continue
            if _EVENT_LOG_INSERT_SQL_PATTERN.search(node.value):
                fn_name = _get_enclosing_function_name(node, parent_map)
                stmt_info = {
                    "file": rel_file_path,
                    "function": fn_name,
                    "site_id": f"{rel_file_path}::{fn_name}",
                    "lineno": getattr(node, "lineno", 0),
                    "snippet": node.value.strip()[:80],
                }
                if rel_file_path == "nce/event_log.py" and fn_name == "_insert_event":
                    owning.append(stmt_info)
                else:
                    offenders.append(stmt_info)

    return owning, offenders


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


def _collect_all_publish_calls() -> list[dict[str, Any]]:
    all_calls: list[dict[str, Any]] = []
    for py_path in sorted(_NCE_DIR.rglob("*.py")):
        rel_path = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_bytes())
        except Exception:
            continue
        all_calls.extend(_scan_publish_calls(tree, rel_path))
    return all_calls


def _collect_all_event_log_chokepoint_statements() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    all_owning: list[dict[str, Any]] = []
    all_offenders: list[dict[str, Any]] = []
    for py_path in sorted(_NCE_DIR.rglob("*.py")):
        rel_path = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_bytes())
        except Exception:
            continue
        owning, offenders = _scan_event_log_chokepoint_insert_statements(tree, rel_path)
        all_owning.extend(owning)
        all_offenders.extend(offenders)
    return all_owning, all_offenders


# ---------------------------------------------------------------------------
# Tests: append_event Transaction Ratchet
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tests: publish Transaction Ratchet
# ---------------------------------------------------------------------------


def test_every_publish_is_in_transaction_or_allowlisted() -> None:
    """Every outbox publish call must be enclosed in a transaction or allowlisted."""
    all_calls = _collect_all_publish_calls()
    unenclosed = [c for c in all_calls if not c["enclosed_in_transaction"]]

    unaccounted: list[str] = []
    for c in unenclosed:
        if c["site_id"] not in KNOWN_UNENCLOSED_PUBLISH_SITES:
            unaccounted.append(f"{c['site_id']} (line {c['lineno']})")

    assert not unaccounted, (
        f"Found {len(unaccounted)} publish calls without enclosing transaction: {unaccounted}. "
        "Wrap outbox publish calls in 'async with conn.transaction():' or 'async with scoped_pg_session(...):'."
    )


def test_publish_allowlist_is_shrink_only_and_reasoned() -> None:
    """The publish allowlist must be shrink-only and every entry substantiated."""
    all_calls = _collect_all_publish_calls()
    unenclosed_site_ids = {c["site_id"] for c in all_calls if not c["enclosed_in_transaction"]}

    # 1. Shrink-only: every allowlisted site must currently have an unenclosed call
    stale_entries = set(KNOWN_UNENCLOSED_PUBLISH_SITES.keys()) - unenclosed_site_ids
    assert not stale_entries, (
        f"Allowlist contains publish entries that are now enclosed in transactions: {stale_entries}. "
        "Remove them to shrink the allowlist."
    )

    # 2. Substantive documentation contracts
    for site_id, meta in KNOWN_UNENCLOSED_PUBLISH_SITES.items():
        assert "owner" in meta and meta["owner"], f"{site_id} missing owner"
        assert "reason" in meta and len(meta["reason"]) >= 60, (
            f"{site_id} reason must be >= 60 chars, got {len(meta.get('reason', ''))}"
        )
        assert "caller_guarantee" in meta and len(meta["caller_guarantee"]) >= 60, (
            f"{site_id} caller_guarantee must be >= 60 chars, got {len(meta.get('caller_guarantee', ''))}"
        )


def test_discovery_floor_for_publish_scanner() -> None:
    """Guard-the-guard: verify AST discovery resolves at least 6 outbox publish calls (9 measured across nce/)."""
    all_calls = _collect_all_publish_calls()
    assert len(all_calls) >= 6, (
        f"AST discovery floor breached: expected >= 6 calls, found {len(all_calls)}"
    )


def test_positive_control_ignores_non_bus_publish_calls() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner ignores Redis and other non-bus publish calls."""
    code = """
async def redis_invalidation(active_redis, key):
    await active_redis.publish("nce:settings:invalidate", key)

async def other_publisher(emitter, event):
    await emitter.publish(event)
"""
    tree = ast.parse(code)
    calls = _scan_publish_calls(tree, "nce/synthetic_redis.py")
    assert len(calls) == 0, f"Expected 0 outbox publish calls, found {calls}"


def test_positive_control_detects_bus_publish_when_imported() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner detects bare publish when imported from nce.events.bus."""
    code = """
from nce.events.bus import publish

async def bus_emitter(conn, ns_uuid):
    await publish(
        conn,
        namespace_id=ns_uuid,
        node_type="TICKET",
        op="created",
        aggregate_id="t-1",
        payload={},
    )
"""
    tree = ast.parse(code)
    calls = _scan_publish_calls(tree, "nce/synthetic_bus.py")
    assert len(calls) == 1
    assert calls[0]["site_id"] == "nce/synthetic_bus.py::bus_emitter"
    assert not calls[0]["enclosed_in_transaction"]


def test_positive_control_fails_on_unenclosed_publish() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner flags unenclosed outbox publish."""
    bad_code = """
from nce.events.bus import publish

async def bad_publisher(conn, ns_uuid):
    await publish(
        conn,
        namespace_id=ns_uuid,
        node_type="TICKET",
        op="created",
        aggregate_id="t-1",
        payload={},
    )
"""
    tree = ast.parse(bad_code)
    calls = _scan_publish_calls(tree, "nce/synthetic_publisher.py")
    assert len(calls) == 1
    assert not calls[0]["enclosed_in_transaction"]


def test_positive_control_passes_on_enclosed_publish() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner passes transaction-enclosed outbox publish."""
    good_code = """
from nce.events.bus import publish

async def good_publisher(pool, ns_uuid):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await publish(
                conn,
                namespace_id=ns_uuid,
                node_type="TICKET",
                op="created",
                aggregate_id="t-1",
                payload={},
            )

async def scoped_publisher(pool, ns_uuid):
    async with scoped_pg_session(pool, ns_uuid) as conn:
        await publish(
            conn,
            namespace_id=ns_uuid,
            node_type="TICKET",
            op="created",
            aggregate_id="t-1",
            payload={},
        )
"""
    tree = ast.parse(good_code)
    calls = _scan_publish_calls(tree, "nce/synthetic_publisher.py")
    assert len(calls) == 2
    assert all(c["enclosed_in_transaction"] for c in calls)


# ---------------------------------------------------------------------------
# Tests: event_log Chokepoint Prohibition Ratchet
# ---------------------------------------------------------------------------


def test_event_log_chokepoint_prohibits_direct_insert() -> None:
    """Direct INSERT INTO event_log statements outside nce/event_log.py::_insert_event are prohibited."""
    owning, offenders = _collect_all_event_log_chokepoint_statements()
    assert not offenders, (
        f"Found {len(offenders)} unauthorized direct INSERT INTO event_log statements outside "
        f"designated chokepoint owner 'nce/event_log.py::_insert_event': {offenders}. "
        "All event emissions must route through nce.event_log.append_event."
    )


def test_discovery_floor_for_event_log_chokepoint_scanner() -> None:
    """Guard-the-guard: verify scanner discovers the sanctioned owning INSERT in nce/event_log.py."""
    owning, offenders = _collect_all_event_log_chokepoint_statements()
    assert len(owning) >= 1, (
        f"Event log chokepoint discovery floor breached: expected >= 1 owning statement, found {len(owning)}. "
        "Scanner must find the authorized INSERT INTO event_log in nce/event_log.py::_insert_event."
    )
    assert any(
        s["file"] == "nce/event_log.py" and s["function"] == "_insert_event" for s in owning
    ), f"Expected nce/event_log.py::_insert_event in owning statements, got {owning}"


def test_positive_control_detects_direct_event_log_insert() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner flags direct INSERT INTO event_log."""
    bad_code = """
async def rogue_writer(conn, event_id, ns_uuid):
    await conn.execute(
        "INSERT INTO event_log (id, namespace_id) VALUES ($1, $2)",
        event_id,
        ns_uuid,
    )
"""
    tree = ast.parse(bad_code)
    owning, offenders = _scan_event_log_chokepoint_insert_statements(tree, "nce/rogue_module.py")
    assert len(owning) == 0
    assert len(offenders) == 1
    assert offenders[0]["site_id"] == "nce/rogue_module.py::rogue_writer"
