"""tests/test_mcp_transport_is_stdio_only.py

Structural ratchet protecting the conclusion of the MCP-redaction
measurement (dispatched after #337, the principal-tier-hardening PR):
MCP redaction was declined as "not a gap" because the transport itself is
the authentication boundary -- exactly one caller per running process, by
construction, never many callers sharing one instance the way admin_app's
HTTP surface does. `_validate_scope("tenant", ...)` (nce/auth.py) checks
one shared secret against itself and `_bind_mcp_tenant_namespace` pins one
namespace per process; neither is a per-caller credential, and there is no
per-caller identity anywhere on this path today (docs/enterprise_security.md
S2, PR #137 / B67P / "D30").

That conclusion holds only as long as the MCP server's one and only
transport is ``mcp.server.stdio.stdio_server()``. Introduce an HTTP or SSE
(or WebSocket) transport on either MCP entry point and the "one caller per
process" guarantee is gone -- at which point MCP redaction stops being "not
a gap" and starts being a real, unbuilt one. This test is the enforcement
of a fact a document currently only asserts; documents rot (six examples of
prose outliving the behaviour it described were found in one day on this
estate -- see K-H11 in the charter), enforced facts do not.

Re-derives its own scope rather than hardcoding it: greps the whole ``nce/``
tree plus ``server.py`` for anything referencing the MCP transport, and
fails loudly if that set of files ever changes, so a new entry point cannot
silently go unchecked.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact two files this ratchet protects, re-verified (not just
# hardcoded) by _test_mcp_entry_points_are_exhaustive below.
_KNOWN_MCP_ENTRY_POINTS = frozenset(
    {
        _REPO_ROOT / "server.py",
        _REPO_ROOT / "nce" / "mcp_stdio_main.py",
    }
)

# Every network-reachable transport the installed mcp SDK ships (checked
# directly against mcp.server's real submodules, not guessed):
# mcp.server.sse.SseServerTransport, mcp.server.streamable_http's
# StreamableHTTPServerTransport, mcp.server.websocket's websocket_server.
# All three are HTTP- or socket-backed; none is a local pipe.
_NON_STDIO_TRANSPORT_MODULES = (
    "mcp.server.sse",
    "mcp.server.streamable_http",
    "mcp.server.streamable_http_manager",
    "mcp.server.websocket",
)

_TRANSPORT_REFERENCE_MARKERS = (
    "mcp.server.stdio",
    "stdio_server(",
    "from mcp.server import Server",
)


def _files_referencing_mcp_transport() -> set[Path]:
    """Every .py file under nce/ or the repo root mentioning the MCP
    transport machinery, found by re-deriving the search rather than
    trusting a fixed list (K-0: validate the instrument before the count)."""
    hits: set[Path] = set()
    candidates = list((_REPO_ROOT / "nce").rglob("*.py")) + [_REPO_ROOT / "server.py"]
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in _TRANSPORT_REFERENCE_MARKERS):
            hits.add(path)
    return hits


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _stdio_server_is_called(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "stdio_server":
                return True
    return False


def _non_stdio_transport_imports(source: str) -> set[str]:
    imported = _imported_module_names(source)
    return {
        mod
        for mod in imported
        if mod in _NON_STDIO_TRANSPORT_MODULES
        or any(mod.startswith(bad + ".") for bad in _NON_STDIO_TRANSPORT_MODULES)
    }


def test_mcp_entry_points_are_exhaustive():
    """If a third file starts referencing the MCP transport, this ratchet's
    own scope is stale -- fail loudly here rather than silently missing it
    in the check below."""
    found = _files_referencing_mcp_transport()
    assert found == _KNOWN_MCP_ENTRY_POINTS, (
        f"The set of files referencing the MCP transport changed -- expected exactly "
        f"{sorted(str(p) for p in _KNOWN_MCP_ENTRY_POINTS)}, found "
        f"{sorted(str(p) for p in found)}. Update _KNOWN_MCP_ENTRY_POINTS in this test "
        f"AND re-examine whether the new file changes the 'one caller per process' "
        f"conclusion before doing so."
    )


def test_mcp_server_uses_only_the_stdio_transport():
    """The real check: neither known MCP entry point imports a
    network-reachable transport, and the one that must be present
    (stdio_server) actually is -- proving this isn't vacuously passing
    because nothing imports mcp.server.* at all."""
    stdio_call_found = False
    for path in _KNOWN_MCP_ENTRY_POINTS:
        source = path.read_text(encoding="utf-8")
        bad = _non_stdio_transport_imports(source)
        assert not bad, (
            f"{path} imports a network-reachable MCP transport: {sorted(bad)}. "
            f"MCP redaction was skipped because stdio guarantees one caller per "
            f"process -- that guarantee no longer holds. Before wiring this "
            f"transport in, resolve how principal tier is derived per-caller on "
            f"it (see nce.resource_surface.rest.resolve_principal_tier for the "
            f"admin_app precedent) or this surface silently exposes every field "
            f"to every caller, the exact shape #337 closed on admin_app."
        )
        if _stdio_server_is_called(source):
            stdio_call_found = True

    assert stdio_call_found, (
        "Neither known MCP entry point calls stdio_server() anymore -- either "
        "the transport moved somewhere this ratchet doesn't look, or it was "
        "removed. Either way this check is no longer proving what it claims to."
    )


def test_positive_control_a_non_stdio_import_is_actually_caught():
    """U18-style positive control against a synthetic file, so the real
    check above isn't trusted to be non-vacuous just because it currently
    passes -- the third test G's finding taught this estate to always
    include: prove the valid case stays silent, separately from proving the
    bad case is caught."""
    clean_source = textwrap.dedent(
        """
        from mcp.server import Server
        from mcp.server.stdio import stdio_server

        async def run():
            async with stdio_server() as (r, w):
                pass
        """
    )
    assert _non_stdio_transport_imports(clean_source) == set()
    assert _stdio_server_is_called(clean_source) is True

    poisoned_source = clean_source + "\nfrom mcp.server.sse import SseServerTransport\n"
    assert _non_stdio_transport_imports(poisoned_source) == {"mcp.server.sse"}

    poisoned_streamable = clean_source + "\nimport mcp.server.streamable_http\n"
    assert _non_stdio_transport_imports(poisoned_streamable) == {"mcp.server.streamable_http"}

    poisoned_websocket = clean_source + "\nfrom mcp.server.websocket import websocket_server\n"
    assert _non_stdio_transport_imports(poisoned_websocket) == {"mcp.server.websocket"}
