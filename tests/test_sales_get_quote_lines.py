"""Tests for the quote-lines READ seam (Module 5, Wave 16 -- Batch 132f --
``nce/vertical_modules/sales/lines.do_get_quote_lines`` + its MCP surface + the
``system_design.from_quote`` seam it un-breaks).

What is proved here:

  * a quote's lines come back, and ONLY that quote's -- another quote in the
    same namespace is not included;
  * CROSS-NAMESPACE ISOLATION: the same ``quote_id`` seeded in two namespaces
    returns only the caller's rows. This is the step-7 positive-control target;
    the predicate under test is the ``namespace_id`` scoping in
    ``nce.bom_lines.list_bom_lines_for_quote``, driven through
    ``do_get_quote_lines`` rather than through the store call directly;
  * a missing/blank ``namespace_id`` is refused before any DB work;
  * an unknown ``quote_id`` returns ``[]`` rather than raising;
  * D37 VISIBILITY -- what the store does NOT hold, asserted rather than
    discovered downstream;
  * THE D47 PROOF -- ``do_design_from_quote`` with the seam UNPATCHED.

``@pytest.mark.integration`` on every DB-touching test; the signature,
registration and advertisement checks are pure logic and stay unmarked.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.sales.lines import do_add_quote_line, do_get_quote_lines
from nce.vertical_modules.system_design import from_quote as from_quote_mod

_TOOL = "sales_get_quote_lines"

# The four fields ``_read_quote_lines``' old docstring promised that have NO
# column in ``bom_line_content`` (migration 058). Ledger defect D37 -- filed
# separately, NOT fixed by this wave.
_D37_ABSENT_FIELDS = ("fl_path", "manufacturer", "mfr_part_no", "confidence")


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def test_the_seam_signature_is_unchanged() -> None:
    """``from_quote`` calls ``_read_quote_lines`` positionally and existing
    tests patch it by that path; changing the signature breaks both."""
    params = list(inspect.signature(from_quote_mod._read_quote_lines).parameters)
    assert params == ["engine", "namespace_id", "quote_id"]
    assert inspect.iscoroutinefunction(from_quote_mod._read_quote_lines)


def test_the_seam_no_longer_raises_notimplementederror() -> None:
    """D47: the body raised unconditionally, so every call of the mounted
    route returned 500. A source-level assertion, because the runtime proof
    below needs a database and this one does not."""
    src = inspect.getsource(from_quote_mod._read_quote_lines)
    assert "raise NotImplementedError" not in src
    assert "do_get_quote_lines" in src


def test_system_design_does_not_hard_import_sales_at_module_load() -> None:
    """The lazy import is a module rule, not a style preference: a hard import
    would couple the two verticals at load time."""
    head = inspect.getsource(from_quote_mod).split("def _read_quote_lines", 1)[0]
    assert "vertical_modules.sales" not in head


def test_the_tool_is_registered_as_a_non_cacheable_read() -> None:
    from nce.tool_registry import (
        ADMIN_ONLY_TOOLS,
        CACHEABLE_TOOLS,
        MIGRATION_TOOLS,
        MUTATION_TOOLS,
        TOOL_REGISTRY,
    )

    assert _TOOL in TOOL_REGISTRY
    # A read writes nothing, needs no admin, runs no migration -- and is
    # deliberately NOT cacheable (quote lines change as lines are added).
    assert _TOOL not in MUTATION_TOOLS
    assert _TOOL not in ADMIN_ONLY_TOOLS
    assert _TOOL not in CACHEABLE_TOOLS
    assert _TOOL not in MIGRATION_TOOLS


def test_the_tool_is_advertised_with_both_required_arguments() -> None:
    from nce.mcp_stdio_tools import TOOLS

    tool = next(t for t in TOOLS if t.name == _TOOL)
    assert sorted(tool.inputSchema["required"]) == ["namespace_id", "quote_id"]


@pytest.mark.parametrize("bad_ns", ["", "   ", None])
def test_a_missing_or_blank_namespace_id_is_refused(bad_ns: Any) -> None:
    """Refused before the connection is touched -- ``None`` as the engine is
    safe here, and proves it."""
    with pytest.raises(ValueError):
        asyncio.run(do_get_quote_lines(None, bad_ns, "QREAD01"))


def test_a_missing_quote_id_is_refused() -> None:
    with pytest.raises(ValueError):
        asyncio.run(do_get_quote_lines(None, uuid.uuid4(), "  "))


# ---------------------------------------------------------------------------
# Integration helpers -- same shape as tests/test_sales_lines_manual.py's.
# ---------------------------------------------------------------------------


class _Engine:
    """Minimal NCEEngine stand-in: the read only needs ``pg_pool``."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await seed_node_ownership_registry(conn, namespace_id)


async def _seed_line(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    **overrides: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        quote_id="QREAD01", line_ref="AMP01", qty="2", unit_price="1000.50"
    )
    kwargs.update(overrides)
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        return await do_add_quote_line(conn, namespace_id, **kwargs)


# ---------------------------------------------------------------------------
# 1. The right lines, and only the right lines.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_read_returns_this_quotes_lines_and_not_another_quotes(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    await _seed_line(pg_pool, namespace_id, line_ref="AMP01")
    await _seed_line(pg_pool, namespace_id, line_ref="SPK01", qty="4")
    await _seed_line(pg_pool, namespace_id, quote_id="QREAD02", line_ref="OTHER01")

    rows = await do_get_quote_lines(_Engine(pg_pool), namespace_id, "QREAD01")

    assert [r["line_ref"] for r in rows] == ["AMP01", "SPK01"]
    assert all(r["quote_id"] == "QREAD01" for r in rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_quote_returns_an_empty_list_rather_than_raising(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    assert await do_get_quote_lines(_Engine(pg_pool), namespace_id, "QNOSUCH") == []


# ---------------------------------------------------------------------------
# 2. CROSS-NAMESPACE ISOLATION -- the step-7 positive-control target.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_same_quote_id_in_two_namespaces_returns_only_the_callers_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    make_namespace: Any,
) -> None:
    """Both namespaces hold a row under the SAME ``quote_id``, differing only
    in ``line_ref``. A read that dropped the tenant predicate would return two
    rows here; identical quote ids are what make that visible (a fixture that
    differs on the key under test cannot discriminate).

    Driven through ``do_get_quote_lines``, not through
    ``list_bom_lines_for_quote``, so the seam itself is what is gated.
    """
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, namespace_id)
    await _seed_ownership(pg_pool, ns_b)

    await _seed_line(pg_pool, namespace_id, quote_id="QSHARED", line_ref="MINE01")
    await _seed_line(pg_pool, ns_b, quote_id="QSHARED", line_ref="THEIRS01")

    engine = _Engine(pg_pool)
    mine = await do_get_quote_lines(engine, namespace_id, "QSHARED")
    theirs = await do_get_quote_lines(engine, ns_b, "QSHARED")

    assert [r["line_ref"] for r in mine] == ["MINE01"]
    assert [r["line_ref"] for r in theirs] == ["THEIRS01"]


# ---------------------------------------------------------------------------
# 3. D37 VISIBILITY -- a limitation with a test is documentation; a limitation
#    without one is a trap.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d37_the_store_carries_no_manufacturer_and_the_seam_invents_none(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Ledger defect **D37**: ``bom_line_content`` has no SKU, product,
    manufacturer or functional-location column. The seam therefore returns
    ``line_ref`` and ``qty`` from the store and NOTHING for the other four
    fields ``_read_quote_lines`` once documented -- ``do_design_from_quote``'s
    existing ``.get()`` defaults supply ``"UNKNOWN"``/``[]``/``1.0``.

    Fabricating any of them here would be indistinguishable downstream from an
    authored value. When D37 is fixed, this test is the one that should fail.
    """
    await _seed_ownership(pg_pool, namespace_id)
    await _seed_line(pg_pool, namespace_id, quote_id="QD37", line_ref="AMP01", qty="3")

    rows = await do_get_quote_lines(_Engine(pg_pool), namespace_id, "QD37")
    assert len(rows) == 1
    row = rows[0]

    # From the store, really there.
    assert row["line_ref"] == "AMP01"
    assert float(row["qty"]) == 3.0

    # Not there, and not invented.
    for field in _D37_ABSENT_FIELDS:
        assert field not in row, f"D37: {field} has no column; it must not be fabricated"

    # The downstream consequence, stated: design lines come out UNKNOWN.
    assert row.get("manufacturer", "UNKNOWN") == "UNKNOWN"
    assert row.get("mfr_part_no", "UNKNOWN") == "UNKNOWN"
    assert row.get("fl_path", []) == []


# ---------------------------------------------------------------------------
# 4. THE D47 PROOF -- the seam UNPATCHED. This is the wave's real acceptance
#    criterion: every existing test of this core mocks the seam, which is why
#    nothing failed when Batch 230a mounted the route on a body that raised.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_d47_design_from_quote_succeeds_with_the_seam_unmocked(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """``POST /api/system-design/from-quote`` returned 500 on EVERY call
    because ``_read_quote_lines`` raised unconditionally. Nothing is patched
    here: the core reads real ``bom_line_content`` rows through the Sales seam.
    """
    await _seed_ownership(pg_pool, namespace_id)
    await _seed_line(pg_pool, namespace_id, quote_id="QD47", line_ref="AMP01", qty="2")
    await _seed_line(pg_pool, namespace_id, quote_id="QD47", line_ref="SPK01", qty="4")

    result = await from_quote_mod.do_design_from_quote(
        _Engine(pg_pool),
        {"namespace_id": namespace_id, "quote_id": "QD47"},
    )

    assert result["quote_lines_realized"] == 2
    assert result["design_id"] == "DESIGN-QD47"
    assert result["quote_label"] == "QUOTE:QD47"
