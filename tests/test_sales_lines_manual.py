"""Tests for the MANUAL-PICK BOM_LINE origination path (Module 5, Wave 15 --
Batch 132d -- ``nce/vertical_modules/sales/lines.py`` + its MCP surface).

Origination path 1 of 5. What is proved here:

  * a manually picked line LANDS, with ``origin_kind='manual'`` and
    ``writer_engine='sales'`` -- through ``assert_owner``, not around it;
  * the DENY path still fires in both directions -- an unregistered transition
    and a registered transition claimed by the wrong engine -- and the same
    test proves the PERMITTED direction, so the case cannot pass with the
    guard defeated (_ORCHESTRATOR.md §6.4);
  * a caller-supplied ``origin_kind`` never reaches the store: the writer has
    no such parameter and the handler never reads the key;
  * idempotency on ``bom_line_label``.

``@pytest.mark.integration`` on every DB-touching test. The signature,
registration and advertisement checks are pure logic and stay unmarked so they
run in the job that always runs.
"""

from __future__ import annotations

import inspect
import json
import uuid
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.bom_lines import bom_line_label
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.sales import lines as sales_lines
from nce.vertical_modules.sales.lines import do_add_quote_line

_OWNERSHIP_MAP_PATH = (
    Path(__file__).resolve().parent.parent / "nce" / "config_data" / "node-ownership.json"
)

_TOOL = "sales_add_quote_line"


# ---------------------------------------------------------------------------
# Pure logic -- the trust boundary is STRUCTURAL, so it is checkable without a
# database.
# ---------------------------------------------------------------------------


def test_the_writer_has_no_origin_kind_or_flow_parameter() -> None:
    """The trust boundary is the signature itself, not a convention.

    A caller that can name its own origin can forge provenance, so there must
    be no ``origin_kind`` and no ``flow`` parameter -- and no ``**kwargs`` that
    would quietly swallow one and pass it on.
    """
    params = inspect.signature(do_add_quote_line).parameters
    assert "origin_kind" not in params
    assert "flow" not in params
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def test_the_module_constants_match_a_registered_ownership_row() -> None:
    """``FLOW``/``WRITER_ENGINE`` are checked against node-ownership.json, not
    against each other -- self-consistency proves nothing about the guard.

    This wave does NOT edit that file: Batch 132a registered all twelve
    BOM_LINE rows at once, so this test reads a row that already exists.
    """
    data: dict[str, Any] = json.loads(_OWNERSHIP_MAP_PATH.read_text(encoding="utf-8"))
    rows = {
        (e["owner_engine"], e["transition"])
        for e in data["ownership"]
        if e["node_type"] == "BOM_LINE"
    }
    assert sales_lines.TRANSITION == "content:create:manual"
    assert sales_lines.TRANSITION == f"content:create:{sales_lines.FLOW}"
    assert (sales_lines.WRITER_ENGINE, sales_lines.TRANSITION) in rows


def test_tool_registered_with_tenant_write_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert _TOOL in TOOL_REGISTRY, f"{_TOOL!r} not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY[_TOOL]
    assert spec.mutation is True
    assert spec.admin_only is False
    assert spec.cacheable is False
    assert spec.migration is False


def test_tool_is_advertised_with_an_input_schema() -> None:
    """OQ-3: a registered tool that is not advertised is invisible to clients."""
    from nce.mcp_stdio_tools import TOOLS

    advertised = {t.name: t for t in TOOLS}
    assert _TOOL in advertised, f"{_TOOL} is registered but not advertised"
    schema = advertised[_TOOL].inputSchema
    assert schema["type"] == "object"
    assert set(schema["required"]) == {
        "namespace_id",
        "quote_id",
        "line_ref",
        "qty",
        "unit_price",
    }
    assert "origin_kind" not in schema["properties"], (
        "advertising origin_kind would invite callers to forge provenance"
    )


@pytest.mark.parametrize("bad", [{}, {"qty": "abc"}, {"unit_price": -1}, {"line_ref": "  "}])
def test_invalid_arguments_raise_value_error_before_any_db_work(bad: dict[str, Any]) -> None:
    """Validation happens before the connection is touched, so ``None`` as the
    connection is safe here -- and proves it."""
    import asyncio

    kwargs: dict[str, Any] = dict(quote_id="QMAN01", line_ref="AMP01", qty="2", unit_price="1000")
    kwargs.update(bad)
    if not bad:
        kwargs["quote_id"] = ""
    with pytest.raises(ValueError):
        asyncio.run(do_add_quote_line(None, uuid.uuid4(), **kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration helpers -- same shape as tests/test_bom_line_store.py's.
# ---------------------------------------------------------------------------


class _Engine:
    """Minimal NCEEngine stand-in: the handler only needs ``pg_pool``."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:  # type: ignore[type-arg]
        self.pg_pool = pg_pool


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await seed_node_ownership_registry(conn, namespace_id)


async def _add(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    **overrides: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        quote_id="QMAN01", line_ref="AMP01", qty="2", unit_price="1000.50"
    )
    kwargs.update(overrides)
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        return await do_add_quote_line(conn, namespace_id, **kwargs)


# ---------------------------------------------------------------------------
# 1. The line lands, with the right provenance.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manual_pick_lands_with_manual_origin_and_sales_writer(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    row = await _add(pg_pool, namespace_id)

    assert row["origin_kind"] == "manual"
    assert row["writer_engine"] == "sales"
    assert row["bom_line_label"] == bom_line_label("QMAN01", "AMP01")
    assert row["qty"] == 2.0
    # line_total defaults to qty * unit_price, computed as Decimal (not float).
    assert row["line_total"] == 2001.0
    assert row["currency"] == "NOK"

    # The kg_nodes BOM_LINE node exists too -- content row without node is a
    # half-write nothing downstream can traverse.
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        node = await conn.fetchrow(
            "SELECT entity_type FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            bom_line_label("QMAN01", "AMP01"),
        )
    assert node is not None and node["entity_type"] == "BOM_LINE"


# ---------------------------------------------------------------------------
# 2. THE DENY PATH -- both directions in one test, so it cannot pass with the
#    guard defeated (§6.4). This is the step-7 positive-control target.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_module_transition_is_permitted_and_an_unregistered_one_is_denied(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PERMITTED with the module's own transition, DENIED with any other.

    Both halves are required. A deny-only test passes just as happily when the
    writer's transition has rotted into something unregistered -- it would then
    be proving that everything is denied, which gates nothing.
    """
    await _seed_ownership(pg_pool, namespace_id)

    # PERMITTED: the module's own registered transition.
    row = await _add(pg_pool, namespace_id, line_ref="PERMIT01")
    assert row["origin_kind"] == sales_lines.FLOW

    # DENIED: an unregistered transition, reached the only way a caller could
    # ever reach one -- by the module's own constant changing.
    monkeypatch.setattr(sales_lines, "FLOW", "manual_forged")
    with pytest.raises(OwnershipError):
        await _add(pg_pool, namespace_id, line_ref="DENY01")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_registered_transition_claimed_by_the_wrong_engine_is_denied(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """content:create:manual is sales-owned; inventory may not claim it."""
    await _seed_ownership(pg_pool, namespace_id)
    monkeypatch.setattr(sales_lines, "WRITER_ENGINE", "inventory")
    with pytest.raises(OwnershipError):
        await _add(pg_pool, namespace_id, line_ref="WRONGENG01")


# ---------------------------------------------------------------------------
# 3. origin_kind is a trust boundary, all the way through the tool surface.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_caller_supplied_origin_kind_never_reaches_the_store(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Through the real MCP handler, with a hostile argument set.

    ``origin_kind`` and ``writer_engine`` in the arguments must be inert: a
    manually entered line that could claim ``design`` would be trusted by a
    later reconciliation report against a legally signed baseline.
    """
    from nce.vertical_modules.sales.mcp_handlers import handle_sales_add_quote_line

    await _seed_ownership(pg_pool, namespace_id)
    payload = await handle_sales_add_quote_line(
        _Engine(pg_pool),  # type: ignore[arg-type]
        {
            "namespace_id": str(namespace_id),
            "quote_id": "QMAN02",
            "line_ref": "SPOOF01",
            "qty": "3",
            "unit_price": "10",
            "origin_kind": "design",
            "writer_engine": "system_design",
            "flow": "design",
        },
    )
    row = json.loads(payload)
    assert row["origin_kind"] == "manual"
    assert row["writer_engine"] == "sales"

    # And the same is true of what is actually on disk, not just what the
    # handler echoed back.
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        stored = await conn.fetchrow(
            "SELECT origin_kind, writer_engine FROM bom_line_content "
            "WHERE namespace_id = $1 AND bom_line_label = $2",
            namespace_id,
            bom_line_label("QMAN02", "SPOOF01"),
        )
    assert stored is not None
    assert stored["origin_kind"] == "manual"
    assert stored["writer_engine"] == "sales"


# ---------------------------------------------------------------------------
# 4. Idempotency on bom_line_label.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replaying_the_same_pick_creates_no_second_row(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    first = await _add(pg_pool, namespace_id, line_ref="IDEM01")
    second = await _add(pg_pool, namespace_id, line_ref="IDEM01", qty="99")

    assert second["id"] == first["id"]
    # The replay must not overwrite content either -- create is not update.
    assert second["qty"] == first["qty"]

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        count = await conn.fetchval(
            "SELECT count(*) FROM bom_line_content WHERE namespace_id = $1 AND bom_line_label = $2",
            namespace_id,
            bom_line_label("QMAN01", "IDEM01"),
        )
    assert count == 1
