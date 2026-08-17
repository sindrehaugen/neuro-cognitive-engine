"""Surface test: the ``sales_get_signed_baseline`` MCP tool is registered
with the correct dispatch flags.

Pure registry assertions — no DB, no engine.  Guards the cross-engine A2A read
seam that Project (``project.baseline._read_signed_baseline``) resolves to at
runtime.  The DB-backed behaviour lives in ``test_sales_signed_baseline.py`` and
``test_sales_sign_to_project.py``.
"""

from __future__ import annotations

from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)

_TOOL = "sales_get_signed_baseline"


def test_signed_baseline_tool_registered() -> None:
    assert _TOOL in TOOL_REGISTRY
    assert TOOL_REGISTRY[_TOOL].handler.__name__ == "handle_sales_get_signed_baseline"


def test_signed_baseline_tool_flags() -> None:
    """Read seam: not a mutation, not admin-only, and NOT cacheable.

    Not cacheable because the freeze happens via the signing callback (not a
    registered MCP mutation), so a stale pre-freeze ``null`` would never be
    invalidated by a cache-generation bump.
    """
    spec = TOOL_REGISTRY[_TOOL]
    assert spec.mutation is False
    assert spec.admin_only is False
    assert spec.cacheable is False

    assert _TOOL not in MUTATION_TOOLS
    assert _TOOL not in ADMIN_ONLY_TOOLS
    assert _TOOL not in CACHEABLE_TOOLS
