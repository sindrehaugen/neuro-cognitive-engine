"""Ratchet: enforce_mcp_tool_auth has exactly one production call site.

The tenant-auth analysis in docs/enterprise_security.md rests on one assumption: MCP is
stdio-only, so the client that spawns the server is also the process holding any key it
would be asked to present (see nce/mcp_stdio_dispatch.py). That is why the tenant branch
of enforce_mcp_tool_auth is safe to leave "self-validating" today. Nothing in the code
enforces that assumption. This test does: if a second call site to enforce_mcp_tool_auth
appears anywhere under nce/ (e.g. a new HTTP or SSE MCP transport), the tenant-auth
analysis must be re-examined before that transport ships, because the client is no longer
guaranteed to be the process parent that supplied NCE_MCP_API_KEY.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NCE_ROOT = REPO_ROOT / "nce"

EXPECTED_CALL_SITES = {"nce/mcp_stdio_dispatch.py"}


def _collect_enforce_mcp_tool_auth_call_sites() -> set[str]:
    """Walk nce/**/*.py and collect files containing a *call* to enforce_mcp_tool_auth.

    Uses the AST rather than text search so that the `def enforce_mcp_tool_auth(...)` in
    nce/auth.py and any bare `from nce.auth import enforce_mcp_tool_auth` import line are
    excluded deliberately -- neither a function definition nor an import is a call, and
    counting them would make this ratchet permanently (and vacuously) red.
    """
    sites: set[str] = set()
    for path in NCE_ROOT.rglob("*.py"):
        skip_dirs = {"__pycache__", ".git"}
        if skip_dirs.intersection(path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "enforce_mcp_tool_auth":
                continue
            # A `def enforce_mcp_tool_auth(...):` is an ast.FunctionDef, never an
            # ast.Call, so the definition in nce/auth.py cannot land here by
            # construction -- only actual call expressions are collected.
            rel = path.relative_to(REPO_ROOT).as_posix()
            sites.add(rel)
    return sites


def test_enforce_mcp_tool_auth_has_exactly_one_call_site() -> None:
    found = _collect_enforce_mcp_tool_auth_call_sites()
    assert found == EXPECTED_CALL_SITES, (
        "enforce_mcp_tool_auth call sites drifted: "
        f"found={sorted(found)} expected={sorted(EXPECTED_CALL_SITES)}. "
        "The tenant-auth trust-boundary analysis in docs/enterprise_security.md assumes "
        "MCP is stdio-only, so the client that spawns the server is the same process "
        "that supplied NCE_MCP_API_KEY -- that is why the tenant branch of "
        "enforce_mcp_tool_auth is safe to leave self-validating. A second call site "
        "means a new transport (HTTP/SSE) may be calling it from a context where the "
        "caller is NOT the process parent, which turns the tenant branch into a real "
        "fail-open. Re-examine and update docs/enterprise_security.md's trust-boundary "
        "section BEFORE shipping that transport, then update EXPECTED_CALL_SITES here."
    )
