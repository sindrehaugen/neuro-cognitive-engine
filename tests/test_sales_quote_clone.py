"""Tests for ``sales_clone_quote`` (charter Wave B-5,
``nce/vertical_modules/sales/quote_clone.py``).

Origination-adjacent, not itself an origination path: what is proved here:

  * the new quote is a fresh draft (status/version reset), not a continuation
    of the source's own lifecycle;
  * cloned lines land through the SAME ``content:create:external`` transition
    ``sales_import_quote_lines`` already owns -- not the source line's own
    original flow, and not a new ``CreateFlow`` value;
  * a source with zero lines clones cleanly (no lines to import is not an
    error);
  * a missing source quote raises before any write.

``@pytest.mark.integration`` on every DB-touching test, matching
``tests/test_sales_lines_manual.py``'s own convention exactly. The signature,
registration and advertisement checks are pure logic and stay unmarked so
they run in the job that always runs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.sales.quote_clone import QuoteNotFoundError, do_clone_quote

_TOOL = "sales_clone_quote"


# ---------------------------------------------------------------------------
# Pure logic -- checkable without a database.
# ---------------------------------------------------------------------------


def test_tool_registered_with_tenant_write_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert _TOOL in TOOL_REGISTRY, f"{_TOOL!r} not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY[_TOOL]
    assert spec.mutation is True
    assert spec.admin_only is False
    assert spec.cacheable is False
    assert spec.migration is False


def test_tool_is_advertised_with_an_input_schema() -> None:
    from nce.mcp_stdio_tools import TOOLS

    advertised = {t.name: t for t in TOOLS}
    assert _TOOL in advertised, f"{_TOOL} is registered but not advertised"
    schema = advertised[_TOOL].inputSchema
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"namespace_id", "quote_id"}


def test_invalid_quote_id_raises_value_error_before_any_db_work() -> None:
    with pytest.raises(ValueError):
        asyncio.run(do_clone_quote(None, uuid.uuid4(), quote_id="  "))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration helpers -- same shape as tests/test_sales_lines_manual.py's.
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


async def _insert_quote(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    **overrides: Any,
) -> str:
    quote_id = str(uuid.uuid4())
    fields: dict[str, Any] = dict(
        id=quote_id,
        namespace_id=namespace_id,
        quote_number="Q-SRC-001",
        title="Original Quote",
        version=3,
        status="sent",
        total_ex_vat=1000.00,
        total_inc_vat=1250.00,
        currency="NOK",
        billing_method="percent",
    )
    fields.update(overrides)
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await conn.execute(
            """
            INSERT INTO sales_quotes
                (id, namespace_id, quote_number, title, version, status,
                 total_ex_vat, total_inc_vat, currency, billing_method)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            uuid.UUID(fields["id"]),
            fields["namespace_id"],
            fields["quote_number"],
            fields["title"],
            fields["version"],
            fields["status"],
            fields["total_ex_vat"],
            fields["total_inc_vat"],
            fields["currency"],
            fields["billing_method"],
        )
    return quote_id


# ---------------------------------------------------------------------------
# 1. The clone lands as a fresh draft, not a continuation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clone_resets_status_and_version_and_gets_a_new_number(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    source_id = await _insert_quote(pg_pool, namespace_id)

    result = await do_clone_quote(_Engine(pg_pool), namespace_id, quote_id=source_id)

    assert result["source_quote_id"] == source_id
    assert result["quote_number"] == "Q-SRC-001-COPY"
    assert result["quote_id"] != source_id

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        row = await conn.fetchrow(
            "SELECT * FROM sales_quotes WHERE namespace_id = $1 AND id = $2",
            namespace_id,
            uuid.UUID(result["quote_id"]),
        )
    assert row is not None
    assert row["status"] == "draft"
    assert row["version"] == 1
    assert row["title"] == "Original Quote (Copy)"
    # Totals copied verbatim, not recomputed -- see quote_clone.py's docstring.
    assert float(row["total_ex_vat"]) == 1000.00
    assert float(row["total_inc_vat"]) == 1250.00
    assert row["billing_method"] == "percent"
    metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
    assert metadata["cloned_from"] == source_id


# ---------------------------------------------------------------------------
# 2. Cloned lines carry the sales-owned external transition, not the
#    source's own original flow.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cloned_lines_carry_the_external_transition_not_the_source_flow(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    from nce.bom_lines import create_bom_line

    await _seed_ownership(pg_pool, namespace_id)
    source_id = await _insert_quote(pg_pool, namespace_id, quote_number="Q-SRC-002")

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        # The source line is genuinely design-originated -- proves the clone
        # does not simply preserve whatever flow the source line carried.
        await create_bom_line(
            conn,
            namespace_id,
            flow="design",
            writer_engine="system_design",
            quote_id=source_id,
            line_ref="DEV01",
            qty=2,
            unit_price=500,
            line_total=1000,
            currency="NOK",
        )

    result = await do_clone_quote(_Engine(pg_pool), namespace_id, quote_id=source_id)
    assert result["lines_cloned"] == 1

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        cloned = await conn.fetchrow(
            "SELECT origin_kind, writer_engine, line_ref, qty, unit_price "
            "FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2",
            namespace_id,
            result["quote_id"],
        )
    assert cloned is not None
    assert cloned["origin_kind"] == "external"
    assert cloned["writer_engine"] == "sales"
    assert cloned["line_ref"] == "DEV01"
    assert float(cloned["qty"]) == 2.0
    assert float(cloned["unit_price"]) == 500.0


# ---------------------------------------------------------------------------
# 3. A source with no lines clones cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clone_with_zero_lines_is_not_an_error(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    source_id = await _insert_quote(pg_pool, namespace_id, quote_number="Q-SRC-003")

    result = await do_clone_quote(_Engine(pg_pool), namespace_id, quote_id=source_id)
    assert result["lines_cloned"] == 0


# ---------------------------------------------------------------------------
# 4. A missing source quote raises before any write.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_source_quote_raises_quote_not_found(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)
    with pytest.raises(QuoteNotFoundError):
        await do_clone_quote(_Engine(pg_pool), namespace_id, quote_id=str(uuid.uuid4()))

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        count = await conn.fetchval(
            "SELECT count(*) FROM sales_quotes WHERE namespace_id = $1", namespace_id
        )
    assert count == 0


# ---------------------------------------------------------------------------
# 5. Through the real MCP handler.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_through_the_real_mcp_handler(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    from nce.vertical_modules.sales.mcp_handlers import handle_sales_clone_quote

    await _seed_ownership(pg_pool, namespace_id)
    source_id = await _insert_quote(pg_pool, namespace_id, quote_number="Q-SRC-004")

    payload = await handle_sales_clone_quote(
        _Engine(pg_pool),  # type: ignore[arg-type]
        {"namespace_id": str(namespace_id), "quote_id": source_id},
    )
    result = json.loads(payload)
    assert result["source_quote_id"] == source_id
    assert result["quote_number"] == "Q-SRC-004-COPY"
