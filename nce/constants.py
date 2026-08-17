"""
nce/constants.py — Single source of truth for shared NCE constants.

Decision log
------------
ALLOWED_LANGUAGES
    ``code_mcp_handlers.py``'s 40-lang frozenset is the canonical definition.
    The 5-lang stubs that existed in ``orchestrator.py`` and
    ``orchestrators/graph.py`` were stale subsets; the handler-layer
    validation is closest to the API contract.

MCP_CACHE_TTL_S
    The dispatch loop hardcoded ``300`` at three call sites.
    ``mcp_args.py`` had a stale ``60`` constant that never matched runtime
    behaviour.  300 wins; the old constant is replaced with an import here.

SAFE_ID_RE
    ``nce/models.py`` is already the canonical home (``orchestrators/memory.py``
    imports from there).  Promoted here so ``orchestrators/graph.py`` can drop
    its duplicate.  ``models.py`` keeps a local alias for backward compatibility.

MAX_TOP_K / MAX_GRAPH_DEPTH
    Were private module-level constants in ``orchestrator.py``; centralised here
    so any future caller can import them without touching orchestrator internals.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Language allowlist
# ---------------------------------------------------------------------------

ALLOWED_LANGUAGES: frozenset[str] = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
        "c",
        "cpp",
        "csharp",
        "ruby",
        "php",
        "swift",
        "kotlin",
        "scala",
        "shell",
        "bash",
        "sql",
        "yaml",
        "json",
        "toml",
        "dockerfile",
        "markdown",
        "html",
        "css",
        "lua",
        "r",
        "julia",
        "haskell",
        "elixir",
        "erlang",
        "dart",
        "perl",
        "objectivec",
        "zig",
        "nim",
        "ocaml",
        "clojure",
        "groovy",
        "terraform",
    }
)

# ---------------------------------------------------------------------------
# Identifier safety regex
# ---------------------------------------------------------------------------

# Alphanumeric, hyphens, underscores; 1–128 characters.
# Matches agent_id / user_id / session_id validation in models + orchestrators.
SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[\w\-]{1,128}$")

# ---------------------------------------------------------------------------
# MCP response cache TTL
# ---------------------------------------------------------------------------

# Seconds; used by the dispatch loop and mcp_args cache helpers.
# (Old value in mcp_args.py was 60 — that was stale and never matched dispatch.)
MCP_CACHE_TTL_S: int = 300

# ---------------------------------------------------------------------------
# Query / graph limits
# ---------------------------------------------------------------------------

MAX_TOP_K: int = 100
MAX_GRAPH_DEPTH: int = 3
