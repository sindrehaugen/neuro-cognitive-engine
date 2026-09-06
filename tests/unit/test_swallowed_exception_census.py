"""AST census pinning swallowed-exception blocks around database writes.

Ensures that:
1. Every ``try: ... except Exception:`` block enclosing database writes (INSERT,
   UPDATE, DELETE, append_event) that only logs or passes without re-raising is
   pinned in a shrink-only allowlist.
2. The population can only decline over time as vertical modules and core components
   are touched and remediated.
3. Every entry in the census is substantiated with an owner, a reason (>= 60 chars),
   and a remediation plan.
4. Standing positive controls (U18) verify the AST scanner flags new swallowed
   writes and permits correctly re-raised or non-write exception blocks.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_NCE_DIR: Final[Path] = _REPO_ROOT / "nce"

# Shrink-only allowlist of existing swallowed DB write exception sites across nce/.
# Identified by "{relative_file_path}::{enclosing_function_name}".
# Every entry must have:
#   - owner: responsible subsystem/module
#   - reason: substantive explanation (>= 60 characters)
#   - remediation: path to safe error handling or justification (>= 60 characters)
KNOWN_SWALLOWED_DB_WRITE_SITES: Final[dict[str, dict[str, str]]] = {
    "nce/admin_handlers/marketing.py::api_marketing_draft_case_study": {
        "owner": "marketing",
        "reason": "Draft case study persistence failure is caught and logged as warning while draft payload is returned to caller.",
        "remediation": "Future refactor should surface database failure through standard error response or outbox transaction pattern.",
    },
    "nce/garbage_collector.py::_clean_orphaned_cascade": {
        "owner": "core-gc",
        "reason": "Cascade cleanup connection acquisition and batched deletion failure logs error and sleeps before next GC tick.",
        "remediation": "Background GC loop intentionally isolates cascade failures to prevent terminating the long-running daemon process.",
    },
    "nce/garbage_collector.py::run_contradiction_retention": {
        "owner": "core-gc",
        "reason": "Retention contradiction purge logs error on per-namespace failure so remaining namespaces can continue purging.",
        "remediation": "Namespace loop catches exception to prevent a single corrupted namespace from blocking global GC retention passes.",
    },
    "nce/garbage_collector.py::run_partition_retention": {
        "owner": "core-gc",
        "reason": "Partition drop failure logs error and continues retention sweep over remaining candidate partitions.",
        "remediation": "Partition retention runs periodically in GC daemon; failure to drop a locked partition logs error without crashing GC.",
    },
    "nce/mtls.py::assert_server_mtls_or_acknowledged": {
        "owner": "core-security",
        "reason": "mTLS disabled posture acknowledgment event logging failure is caught so audit DB write failure does not prevent server boot.",
        "remediation": "Explicit architectural decision documented in docstring: server startup takes precedence over telemetry audit.",
    },
    "nce/orchestrators/cognitive.py::resolve_contradiction": {
        "owner": "core-cognitive",
        "reason": "ATMS cascade deletion failure inside savepoint logs exception to prevent secondary ATMS failure from aborting contradiction resolution.",
        "remediation": "Nested savepoint transaction isolates optional ATMS graph pruning from primary cognitive memory state transition.",
    },
    "nce/quotas.py::flush_quota_counters_to_postgres": {
        "owner": "core-quotas",
        "reason": "Background quota flush catches failure and logs exception to prevent scheduled periodic flush task from crashing worker.",
        "remediation": "Periodic timer task retries on subsequent tick; quota counters remain tracked in in-memory atomic structures.",
    },
    "nce/re_embedder.py::run_re_embedding_worker": {
        "owner": "core-reembedder",
        "reason": "Re-embedding worker catches single-document payload extraction errors and continues batch processing remaining memories.",
        "remediation": "Per-record try block allows worker to bypass corrupt or deleted payload records without halting whole migration batch.",
    },
    "nce/vertical_modules/assets/health.py::do_compute_health": {
        "owner": "assets",
        "reason": "Optional telemetry append to v3_cognitive_ledger logs debug message on error without aborting primary health calculation.",
        "remediation": "Assets engine computes health in memory; ledger persistence failure is treated as non-fatal secondary telemetry.",
    },
    "nce/vertical_modules/business_insights/kpi.py::do_kpi_dashboard": {
        "owner": "business_insights",
        "reason": "KPI snapshot database persistence catches write error and logs warning while returning calculated dashboard metrics.",
        "remediation": "Dashboard read path prioritizes returning live computed metrics even if historical snapshot table write fails.",
    },
    "nce/vertical_modules/hr/onboarding.py::do_build_onboarding_quest": {
        "owner": "hr",
        "reason": "Persisting generated quest state back to employees profile table catches error and logs debug message if skipped.",
        "remediation": "Onboarding quest generator returns quest plan to caller even if profile update fails in disconnected environments.",
    },
    "nce/vertical_modules/inventory/goods_receipt.py::do_record_goods_receipt": {
        "owner": "inventory",
        "reason": "Secondary advance of procurement_po_lines from ORDERED to RECEIVED catches error and logs debug message.",
        "remediation": "Goods receipt core logic completes primary inventory stock update before attempting cross-engine line status update.",
    },
    "nce/vertical_modules/marketing/advisor.py::do_audit_seo": {
        "owner": "marketing",
        "reason": "Updating content_assets with SEO audit report catches write error and logs warning while returning generated SEO report.",
        "remediation": "Advisor tool returns generated analysis to operator even if caching report to content_assets table encounters an error.",
    },
    "nce/vertical_modules/marketing/approval.py::do_approve_content": {
        "owner": "marketing",
        "reason": "Approval status updates to case_studies and cognitive ledger catch errors and log warnings while emitting approval event.",
        "remediation": "Marketing approval module was authored with fallback persistence patterns; planned for refactor in Phase 1 marketing wave.",
    },
    "nce/vertical_modules/marketing/publish.py::do_publish_content": {
        "owner": "marketing",
        "reason": "Updating case_studies status to published catches write error and logs warning while proceeding to event emission.",
        "remediation": "Future marketing wave will transactionalize publishing write and event emission inside an atomic transaction block.",
    },
    "nce/vertical_modules/marketing/testimonials.py::do_request_testimonial": {
        "owner": "marketing",
        "reason": "Inserting initial testimonial record catches write error and logs warning while returning generated testimonial token.",
        "remediation": "Testimonial generation flow planned for full transaction wrapping when marketing engine is next revisited.",
    },
    "nce/vertical_modules/marketing/testimonials.py::do_capture_testimonial": {
        "owner": "marketing",
        "reason": "Inserting captured customer feedback catches write error and logs warning while returning acknowledgement to caller.",
        "remediation": "Feedback capture flow will be enclosed in strict transaction block during next marketing engine feature wave.",
    },
    "nce/vertical_modules/marketing/testimonials.py::do_retract_testimonial": {
        "owner": "marketing",
        "reason": "Updating testimonial status to retracted catches write error and logs warning while returning retracted status dict.",
        "remediation": "Consent retraction flow will be updated to raise MCP database errors rather than returning partial dictionary status.",
    },
    "nce/vertical_modules/sales/source_adapters/d365.py::sync_entity": {
        "owner": "sales",
        "reason": "Change tracking sync failure catches Exception and logs warning before falling back to standard watermark synchronization.",
        "remediation": "Deliberate fallback design: when D365 delta tracking link expires or errors, adapter falls back to timestamp watermark sync.",
    },
    "nce/vertical_modules/business_insights/events.py::emit_business_insights_event": {
        "owner": "business_insights",
        "reason": "Business insights event emitter catches connection/emission errors and logs without failing primary analytical operations.",
        "remediation": "Emission now enclosed in active transaction; future refactor will route via transactional outbox table.",
    },
    "nce/vertical_modules/marketing/events.py::emit_marketing_event": {
        "owner": "marketing",
        "reason": "Marketing event emitter catches connection/emission errors and logs without failing primary marketing workflow operations.",
        "remediation": "Marketing event emission will be refactored with transactional outbox pattern in dedicated marketing wave.",
    },
}

_DB_WRITE_CALL_NAMES: Final[frozenset[str]] = frozenset(
    {"append_event", "_append_a2a_event", "emit_graph_write"}
)
_SQL_WRITE_PREFIXES: Final[tuple[str, ...]] = (
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "DROP TABLE",
    "CREATE TABLE",
)


def _contains_db_write(nodes: list[ast.stmt]) -> bool:
    """Check if AST statement nodes contain database write operations."""
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = None
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr

                if name in _DB_WRITE_CALL_NAMES:
                    return True

                if name in ("execute", "executemany", "fetch", "fetchrow", "fetchval"):
                    for arg in sub.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            sql = arg.value.strip().upper()
                            if any(
                                sql.startswith(kw) or f"\n{kw}" in sql or f" {kw}" in sql
                                for kw in _SQL_WRITE_PREFIXES
                            ):
                                return True
                        elif isinstance(arg, ast.JoinedStr):
                            for part in arg.values:
                                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                                    sql = part.value.strip().upper()
                                    if any(kw in sql for kw in _SQL_WRITE_PREFIXES):
                                        return True
    return False


def _handler_only_logs_or_passes(handler: ast.ExceptHandler) -> bool:
    """Verify if an exception handler only logs, passes, or returns None without re-raising."""
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Return):
            if stmt.value is None or (
                isinstance(stmt.value, ast.Constant) and stmt.value.value is None
            ):
                continue
            return False
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Call):
                call_str = ast.unparse(stmt.value.func)
                if any(log_kw in call_str for log_kw in ("log", "logger", "logging", "print")):
                    continue
            return False
        if isinstance(stmt, ast.Raise):
            return False
        return False
    return True


def _get_enclosing_function_name(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> str:
    curr: ast.AST | None = node
    while curr in parent_map:
        curr = parent_map[curr]
        if isinstance(curr, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return curr.name
    return "<module>"


def _scan_swallowed_db_writes(source_tree: ast.AST, rel_file_path: str) -> list[dict[str, Any]]:
    """Scan AST tree for try blocks containing DB writes whose broad except handlers swallow errors."""
    parent_map: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(source_tree):
        for child in ast.iter_child_nodes(node):
            parent_map[child] = node

    discovered: list[dict[str, Any]] = []
    for node in ast.walk(source_tree):
        if isinstance(node, ast.Try):
            if _contains_db_write(node.body):
                for handler in node.handlers:
                    catches_broad = False
                    if handler.type is None:
                        catches_broad = True
                    elif isinstance(handler.type, ast.Name) and handler.type.id in (
                        "Exception",
                        "BaseException",
                    ):
                        catches_broad = True
                    elif isinstance(handler.type, ast.Tuple):
                        for elt in handler.type.elts:
                            if isinstance(elt, ast.Name) and elt.id in (
                                "Exception",
                                "BaseException",
                            ):
                                catches_broad = True

                    if catches_broad and _handler_only_logs_or_passes(handler):
                        fn_name = _get_enclosing_function_name(node, parent_map)
                        discovered.append(
                            {
                                "file": rel_file_path,
                                "function": fn_name,
                                "site_id": f"{rel_file_path}::{fn_name}",
                                "try_lineno": node.lineno,
                                "handler_lineno": handler.lineno,
                            }
                        )
    return discovered


def _collect_all_swallowed_db_writes() -> list[dict[str, Any]]:
    all_sites: list[dict[str, Any]] = []
    for py_path in sorted(_NCE_DIR.rglob("*.py")):
        rel_path = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_bytes())
        except Exception:
            continue
        all_sites.extend(_scan_swallowed_db_writes(tree, rel_path))
    return all_sites


def test_swallowed_exception_census_is_shrink_only() -> None:
    """The census must be shrink-only: no unallowlisted swallowed DB writes permitted."""
    all_sites = _collect_all_swallowed_db_writes()
    discovered_site_ids = {s["site_id"] for s in all_sites}

    unallowlisted = discovered_site_ids - set(KNOWN_SWALLOWED_DB_WRITE_SITES.keys())
    assert not unallowlisted, (
        f"Found {len(unallowlisted)} new swallowed DB write exception sites: {sorted(unallowlisted)}. "
        "Do not catch Exception over DB writes without re-raising or surfacing an error."
    )

    stale = set(KNOWN_SWALLOWED_DB_WRITE_SITES.keys()) - discovered_site_ids
    assert not stale, (
        f"Census allowlist contains remediated sites that no longer swallow DB writes: {stale}. "
        "Remove them from KNOWN_SWALLOWED_DB_WRITE_SITES to ratchet down the count."
    )


def test_swallowed_exception_allowlist_is_reasoned() -> None:
    """Every allowlisted site must have an owner, a >=60 char reason, and a >=60 char remediation."""
    for site_id, meta in KNOWN_SWALLOWED_DB_WRITE_SITES.items():
        assert "owner" in meta and meta["owner"], f"{site_id} missing owner"
        assert "reason" in meta and len(meta["reason"]) >= 60, (
            f"{site_id} reason must be >= 60 chars, got {len(meta.get('reason', ''))}"
        )
        assert "remediation" in meta and len(meta["remediation"]) >= 60, (
            f"{site_id} remediation must be >= 60 chars, got {len(meta.get('remediation', ''))}"
        )


def test_discovery_floor_for_swallowed_exception_scanner() -> None:
    """Guard-the-guard: verify scanner discovers at least 15 swallowed DB write sites."""
    all_sites = _collect_all_swallowed_db_writes()
    assert len(all_sites) >= 15, (
        f"Swallowed exception discovery floor breached: expected >= 15, found {len(all_sites)}"
    )


def test_positive_control_detects_swallowed_db_write() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner flags a swallowed DB write."""
    bad_code = """
async def bad_swallower(conn, logger):
    try:
        await conn.execute("INSERT INTO audit_log (msg) VALUES ($1)", "test")
    except Exception as exc:
        logger.warning("Swallowed insert failure: %s", exc)
"""
    tree = ast.parse(bad_code)
    sites = _scan_swallowed_db_writes(tree, "nce/synthetic.py")
    assert len(sites) == 1
    assert sites[0]["site_id"] == "nce/synthetic.py::bad_swallower"


def test_positive_control_permits_reraised_or_non_db_write() -> None:
    """Standing positive control (U18 / T-5 Q1): verify scanner permits re-raised or pure-read blocks."""
    good_code = """
async def good_reraiser(conn, logger):
    try:
        await conn.execute("UPDATE audit_log SET status = 'ok' WHERE id = 1")
    except Exception as exc:
        logger.error("DB update failed: %s", exc)
        raise

async def pure_calc(data):
    try:
        val = 10 / data["factor"]
    except Exception:
        pass
"""
    tree = ast.parse(good_code)
    sites = _scan_swallowed_db_writes(tree, "nce/synthetic.py")
    assert len(sites) == 0, f"Expected 0 swallowed DB write sites, found {sites}"
