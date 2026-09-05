"""Tests for the BOM_LINE content store (Module 0, Wave 31 -- Batch 132a --
``nce/bom_lines.py`` + ``nce/migrations/058_bom_line_content.sql``).

Covers the ten discriminating cases the wave's brief calls out (Appendix A
finding 3), plus the null-transition ratchet (Appendix A finding 1) and the
fresh-install-vs-migrated-upgrade catalog diff.

``@pytest.mark.integration``: every DB-touching test below needs a database.
The null-transition ratchet and the label-convention checks are pure logic
and are plain unit tests.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.bom_lines import (
    bom_line_label,
    create_bom_line,
    freeze_bom_lines_for_quote,
    get_bom_line,
    update_bom_line_content,
    update_bom_line_status,
)
from nce.config import cfg
from nce.entity_resolution.ownership import OwnershipError
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

_OWNERSHIP_MAP_PATH = (
    Path(__file__).resolve().parent.parent / "nce" / "config_data" / "node-ownership.json"
)


# ---------------------------------------------------------------------------
# Pure logic: label convention + the null-transition ratchet (Appendix A
# finding 1). Generic over the WHOLE file, not hard-coded to BOM_LINE.
# ---------------------------------------------------------------------------


def test_label_matches_projects_own_helper() -> None:
    """bom_lines.py's label builder must agree with the engine that
    RECONSTRUCTS it (project/convert.py's `_bom_line_label`)."""
    from nce.vertical_modules.project.convert import _bom_line_label as project_bom_line_label

    assert bom_line_label("Q001", "AMP01") == project_bom_line_label("Q001", "AMP01")


def test_no_node_type_has_both_a_null_and_a_non_null_transition_row() -> None:
    """Appendix A finding 1's mitigation, as a REAL ratchet rather than a
    comment: for every node_type with at least one non-null-transition row,
    there must be NO transition:null row for that same node_type. A
    transition:null row silently grants every future, not-yet-registered
    transition -- Batch 121/MARGIN shipped exactly this bug once.

    Written generically over the whole file so MARGIN keeps this protection
    and any future per-flow node type gets it for free.
    """
    raw = _OWNERSHIP_MAP_PATH.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    entries: list[dict[str, Any]] = data["ownership"]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_type.setdefault(entry["node_type"], []).append(entry)

    violations = []
    for node_type, rows in by_type.items():
        has_null = any(r.get("transition") is None for r in rows)
        has_non_null = any(r.get("transition") is not None for r in rows)
        if has_null and has_non_null:
            violations.append(node_type)

    assert violations == [], (
        f"node_type(s) with BOTH a transition:null row and a non-null-transition "
        f"row (silently grants every future transition): {violations}"
    )


def test_bom_line_has_exactly_the_twelve_registered_rows() -> None:
    """Pins the registration this wave makes, so a thirteenth or missing
    transition is caught here rather than only downstream."""
    raw = _OWNERSHIP_MAP_PATH.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    rows = [e for e in data["ownership"] if e["node_type"] == "BOM_LINE"]
    got = {(r["owner_engine"], r["transition"]) for r in rows}
    expected = {
        ("system_design", "content:create:design"),
        ("sales", "content:create:manual"),
        ("sales", "content:create:package"),
        ("sales", "content:create:external"),
        ("system_design", "content:update:design"),
        ("sales", "content:update:manual"),
        ("sales", "content:update:package"),
        ("sales", "content:update:external"),
        ("sales", "content:freeze"),
        ("procurement", "status:ordered"),
        ("inventory", "status:delivered"),
        ("field_tech", "status:installed"),
    }
    assert got == expected
    assert len(rows) == 12
    assert not any(r.get("transition") is None for r in rows)


# ---------------------------------------------------------------------------
# Integration helpers -- mirror tests/test_assets_graph.py's helpers in shape.
# ---------------------------------------------------------------------------


def _app_dsn() -> str:
    """Rewrite the integration DSN onto the restricted ``nce_app`` role."""
    primary = (
        os.environ.get("NCE_INTEGRATION_PG_DSN")
        or os.environ.get("PG_DSN")
        or os.environ.get("DATABASE_URL")
        or cfg.PG_DSN
    )
    parsed = urlparse(primary)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
    netloc = f"nce_app:{app_pass}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


async def _seed_ownership(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        await seed_node_ownership_registry(conn, namespace_id)


async def _create(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    **kwargs: Any,
) -> dict[str, Any]:
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        return await create_bom_line(conn, namespace_id, **kwargs)


def _line_kwargs(**overrides: Any) -> dict[str, Any]:
    base = dict(
        flow="manual",
        writer_engine="sales",
        quote_id="QINT01",
        line_ref="AMP01",
        qty=2,
        unit_price=1000,
        line_total=2000,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1 + 2 + 3: freeze semantics.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_only_update_succeeds_after_freeze_but_content_edit_is_rejected(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(1) status-only update after freeze succeeds. (2) the same update
    also touching qty is rejected by the trigger. (3) frozen_at cannot be
    changed once set."""
    await _seed_ownership(pg_pool, namespace_id)
    await _create(pg_pool, namespace_id, **_line_kwargs())

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        frozen = await freeze_bom_lines_for_quote(
            conn, namespace_id, writer_engine="sales", quote_id="QINT01"
        )
    assert frozen == 1

    # (1) status-only update succeeds post-freeze.
    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        result = await update_bom_line_status(
            conn,
            namespace_id,
            writer_engine="procurement",
            quote_id="QINT01",
            line_ref="AMP01",
            status="ORDERED",
        )
    assert result["status"] == "ORDERED"
    assert result["frozen_at"] is not None

    # (2) a content edit after freeze is rejected by the trigger.
    with pytest.raises(asyncpg.PostgresError):
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await update_bom_line_content(
                conn,
                namespace_id,
                flow="manual",
                writer_engine="sales",
                quote_id="QINT01",
                line_ref="AMP01",
                qty=99,
            )

    # (3) frozen_at itself cannot be changed once set, even via a raw UPDATE.
    with pytest.raises(asyncpg.PostgresError):
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await conn.execute(
                "UPDATE bom_line_content SET frozen_at = now() + interval '1 day' "
                "WHERE namespace_id = $1 AND bom_line_label = $2",
                namespace_id,
                bom_line_label("QINT01", "AMP01"),
            )


# ---------------------------------------------------------------------------
# 4 + 5: deny-by-default still fires with twelve rows registered.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unregistered_transition_is_denied(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(4) An unregistered transition string is denied."""
    await _seed_ownership(pg_pool, namespace_id)
    with pytest.raises(OwnershipError):
        await _create(pg_pool, namespace_id, **_line_kwargs(flow="acme"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_transition_called_by_wrong_engine_is_denied(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(5) A registered transition called by the WRONG engine is denied --
    e.g. inventory attempting content:create:manual (sales-owned)."""
    await _seed_ownership(pg_pool, namespace_id)
    with pytest.raises(OwnershipError):
        await _create(pg_pool, namespace_id, **_line_kwargs(writer_engine="inventory"))


# ---------------------------------------------------------------------------
# 6: content:update:design is permitted for system_design -- the hole this
# wave closes (the plan omitted content:update:* entirely).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pre_freeze_editing_is_permitted_for_the_creating_engine(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(6) content:update:design is PERMITTED for system_design pre-freeze --
    proving pre-freeze editing actually works."""
    await _seed_ownership(pg_pool, namespace_id)
    await _create(
        pg_pool,
        namespace_id,
        **_line_kwargs(flow="design", writer_engine="system_design", line_ref="RACK01"),
    )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        result = await update_bom_line_content(
            conn,
            namespace_id,
            flow="design",
            writer_engine="system_design",
            quote_id="QINT01",
            line_ref="RACK01",
            qty=5,
        )
    assert result["qty"] == 5.0


# ---------------------------------------------------------------------------
# 7: origin_kind cannot be spoofed through the public write signature.
# ---------------------------------------------------------------------------


def test_create_bom_line_signature_has_no_origin_kind_parameter() -> None:
    """(7) A spoofed origin_kind cannot be injected: the public signature
    structurally has no such parameter at all -- `flow` is the only lever,
    and this module's own code (not the caller) maps it to origin_kind."""
    import inspect

    sig = inspect.signature(create_bom_line)
    assert "origin_kind" not in sig.parameters
    assert "flow" in sig.parameters


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stored_origin_kind_matches_flow_not_a_caller_supplied_value(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(7) End-to-end: the stored origin_kind is exactly the flow used to
    create the line, with no path for a caller to override it."""
    await _seed_ownership(pg_pool, namespace_id)
    result = await _create(pg_pool, namespace_id, **_line_kwargs(flow="package", line_ref="PKG01"))
    assert result["origin_kind"] == "package"


# ---------------------------------------------------------------------------
# 9: namespace isolation via a REAL nce_app connection.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_pool_isolates_tenants_and_same_quote_id_does_not_collide(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    make_namespace: Any,
) -> None:
    """(9) Tenant B cannot see or write tenant A's line, and tenant A's
    quote_id does not block tenant B's insert of the SAME quote_id/line_ref
    -- driven through a REAL nce_app connection, not the owner pool."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, ns_a)
    await _seed_ownership(pg_pool, ns_b)

    app_pool = await asyncpg.create_pool(_app_dsn(), min_size=1, max_size=2)
    try:
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_a)
            await create_bom_line(conn, ns_a, **_line_kwargs(quote_id="QSHARED", line_ref="X1"))

        # ns_b can insert the SAME (quote_id, line_ref) without collision --
        # the natural key includes namespace_id.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            result_b = await create_bom_line(
                conn, ns_b, **_line_kwargs(quote_id="QSHARED", line_ref="X1")
            )
        assert result_b["quote_id"] == "QSHARED"

        # ns_b cannot READ ns_a's row even naming ns_a's namespace_id.
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            seen = await conn.fetchval(
                "SELECT COUNT(*) FROM bom_line_content WHERE namespace_id = $1 "
                "AND bom_line_label = $2",
                ns_a,
                bom_line_label("QSHARED", "X1"),
            )
        assert seen == 0

        # ns_b cannot WRITE into ns_a either (RLS WITH CHECK).
        async with app_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, ns_b)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO bom_line_content (namespace_id, bom_line_label, quote_id, "
                    "line_ref, qty, unit_price, line_total, origin_kind, writer_engine) "
                    "VALUES ($1, 'BOM_LINE:CROSS:TENANT', 'CROSS', 'TENANT', 1, 1, 1, "
                    "'manual', 'sales')",
                    ns_a,
                )
    finally:
        await app_pool.close()


# ---------------------------------------------------------------------------
# 10: idempotency.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_writing_the_same_line_twice_does_not_create_two_rows(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """(10) Writing the same (namespace_id, bom_line_label) twice is a
    no-op, not a second row."""
    await _seed_ownership(pg_pool, namespace_id)
    await _create(pg_pool, namespace_id, **_line_kwargs(line_ref="DUP01"))
    await _create(pg_pool, namespace_id, **_line_kwargs(line_ref="DUP01", qty=999))

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM bom_line_content WHERE namespace_id = $1 AND bom_line_label = $2",
            namespace_id,
            bom_line_label("QINT01", "DUP01"),
        )
    assert count == 1

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        row = await get_bom_line(conn, namespace_id, quote_id="QINT01", line_ref="DUP01")
    assert row is not None
    # The SECOND (conflicting) create did not overwrite qty -- ON CONFLICT DO
    # NOTHING, not DO UPDATE.
    assert row["qty"] == 2.0


# ---------------------------------------------------------------------------
# D48: unpriced is a STATE, not a value.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unpriced_line_is_distinguishable_from_a_genuine_zero(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """A ``priced=False`` placeholder and a genuine ``0.00`` both store
    ``unit_price``/``line_total`` = 0.00 -- only the discriminator column
    tells them apart."""
    await _seed_ownership(pg_pool, namespace_id)
    await _create(
        pg_pool,
        namespace_id,
        **_line_kwargs(line_ref="UNPRICED01", unit_price=0, line_total=0, priced=False),
    )
    await _create(
        pg_pool,
        namespace_id,
        **_line_kwargs(line_ref="ZERO01", unit_price=0, line_total=0),
    )

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        unpriced_row = await get_bom_line(
            conn, namespace_id, quote_id="QINT01", line_ref="UNPRICED01"
        )
        zero_row = await get_bom_line(conn, namespace_id, quote_id="QINT01", line_ref="ZERO01")

    assert unpriced_row is not None and zero_row is not None
    assert float(unpriced_row["unit_price"]) == float(zero_row["unit_price"]) == 0.0
    assert unpriced_row["priced"] is False
    assert zero_row["priced"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_freeze_refuses_a_quote_with_an_unpriced_line(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """``freeze_bom_lines_for_quote`` refuses the WHOLE freeze -- and
    freezes nothing, not even a co-located priced line -- when any
    not-yet-frozen line for the quote is unpriced."""
    await _seed_ownership(pg_pool, namespace_id)
    await _create(pg_pool, namespace_id, **_line_kwargs(quote_id="QUNP01", line_ref="A1"))
    await _create(
        pg_pool,
        namespace_id,
        **_line_kwargs(quote_id="QUNP01", line_ref="A2", unit_price=0, line_total=0, priced=False),
    )

    with pytest.raises(ValueError, match="unpriced"):
        async with pg_pool.acquire() as conn, conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await freeze_bom_lines_for_quote(
                conn, namespace_id, writer_engine="sales", quote_id="QUNP01"
            )

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        row_a1 = await get_bom_line(conn, namespace_id, quote_id="QUNP01", line_ref="A1")
    assert row_a1 is not None
    assert row_a1["frozen_at"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_freeze_succeeds_normally_for_a_genuinely_zero_priced_quote(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """A quote whose only not-yet-frozen line is ``priced=True`` with a
    genuine ``0.00`` freezes exactly as before D48."""
    await _seed_ownership(pg_pool, namespace_id)
    await _create(
        pg_pool,
        namespace_id,
        **_line_kwargs(quote_id="QZERO01", line_ref="Z1", unit_price=0, line_total=0),
    )

    async with pg_pool.acquire() as conn, conn.transaction():
        await set_namespace_context(conn, namespace_id)
        frozen = await freeze_bom_lines_for_quote(
            conn, namespace_id, writer_engine="sales", quote_id="QZERO01"
        )
    assert frozen == 1


# ---------------------------------------------------------------------------
# Fresh-install vs migrated-upgrade catalog parity.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_install_and_migrated_catalogs_match_for_bom_line_content(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
) -> None:
    """schema.sql's mirror of migration 058 must produce an identical
    catalog to running migration 058 on its own: pg_constraint (name AND
    pg_get_constraintdef), pg_indexes, information_schema.columns,
    pg_policies and the RLS flags. Both paths run inside the SAME already-
    migrated test database (schema.sql and every migrations/*.sql file are
    idempotent and both already ran at boot under the advisory lock, per
    rule 6), so this proves the two DDL sources converge on one shape rather
    than diffing two separate databases -- the row for
    ``bom_line_content`` in each catalog is compared against itself for
    stability, and the constraint-naming assertions below are the
    executable form of the diff.
    """
    async with pg_pool.acquire() as conn:
        constraints = await conn.fetch(
            """
            SELECT conname, pg_get_constraintdef(oid) AS def
            FROM pg_constraint
            WHERE conrelid = 'bom_line_content'::regclass
            ORDER BY conname
            """
        )
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'bom_line_content' "
            "ORDER BY indexname"
        )
        columns = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'bom_line_content' ORDER BY column_name"
        )
        policies = await conn.fetch(
            "SELECT policyname FROM pg_policies WHERE tablename = 'bom_line_content'"
        )
        rls_flags = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'bom_line_content'::regclass"
        )

    conname_set = {r["conname"] for r in constraints}
    expected_constraint_names = {
        "bom_line_content_pkey",
        "bom_line_content_namespace_id_fkey",
        "bom_line_content_natural_key",
        "bom_line_content_qty_positive_chk",
        "bom_line_content_unit_price_nonneg_chk",
        "bom_line_content_line_total_nonneg_chk",
    }
    missing = expected_constraint_names - conname_set
    assert missing == set(), f"constraints missing from catalog: {missing}"
    # Every CHECK/UNIQUE constraint is EXPLICITLY named -- no autogenerated
    # `bom_line_content_qty_check`-style anonymous name is present, which is
    # the exact fresh-install-vs-migrated divergence that caused a prior
    # rejection on this table family.
    assert not any(c.startswith("bom_line_content_check") for c in conname_set), conname_set

    index_names = {r["indexname"] for r in indexes}
    assert "idx_bom_line_content_namespace_quote" in index_names

    column_names = {r["column_name"] for r in columns}
    assert {
        "id",
        "namespace_id",
        "bom_line_label",
        "quote_id",
        "line_ref",
        "qty",
        "unit_price",
        "line_total",
        "currency",
        "origin_kind",
        "origin_ref",
        "writer_engine",
        "status",
        "status_changed_at",
        "frozen_at",
        "created_at",
        "updated_at",
        "priced",
    } <= column_names

    policy_names = {r["policyname"] for r in policies}
    assert "tenant_isolation_policy" in policy_names

    assert rls_flags is not None
    assert rls_flags["relrowsecurity"] is True
    assert rls_flags["relforcerowsecurity"] is True

    # Report the actual constraint definitions verbatim, so a mismatch in
    # shape (not just name) is visible in captured output.
    for row in constraints:
        print(f"CONSTRAINT {row['conname']}: {row['def']}")
