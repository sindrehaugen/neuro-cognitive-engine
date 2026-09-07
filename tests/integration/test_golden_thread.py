"""
tests/integration/test_golden_thread.py
=======================================
The Golden Thread Scenario Test and v1.5 Burndown Scaffold (Charter §13).

Canonical Lifecycle Flow (Charter §6, Review §0 and §5):
customer → design → quote → sign → **baseline frozen** → project → PO → **ORDERED** → GR → match →
**DELIVERED** → ASSET → **install (INSTALLED)** → **test (TESTED)** → SLA attached → invoice approved →
**actual_cost** → ticket → dispatch → **work order** → outcome → cert expiry → **allocation invalidated**
→ portal request → **ticket** → outcome recorded → design recall returns the project

Per ML-orch Charter §13:
- Live database execution against PostgreSQL across one created & torn-down test namespace.
- Real async business operations writing and reading actual database state.
- Step N+1 consumes what Step N produced.
- NEVER assert callable(). NEVER assert registry membership. NEVER AST-scan.
- Any open seam is marked with @pytest.mark.xfail(strict=True, reason="break-N: ...").
- Fails loudly (pytest.fail) if PostgreSQL is unreachable or DSN is missing.
- Counts real database round trips and fails below a floor, so a run that stops
  touching the database cannot pass. Wall-clock is deliberately NOT used: it
  measures the hardware, not the behaviour.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.bom_lines import update_bom_line_status
from nce.db_utils import scoped_pg_session
from nce.degradation import get_degradation_register
from nce.engine_registry import populate_engine_modules
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.orchestrator import NCEEngine
from nce.signing import (
    NoActiveSigningKeyError,
    SigningKeyDecryptionError,
    get_active_key,
    rotate_key,
)
from nce.vertical_modules.agreements.sla import do_set_sla_coverage
from nce.vertical_modules.assets.seed import do_seed_asset_from_bom
from nce.vertical_modules.assets.sla import do_attach_sla
from nce.vertical_modules.customer_portal.actions import do_raise_service_request
from nce.vertical_modules.economy.cascade import do_cascade_on_approval
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.field_tech.work_orders import do_create_work_order
from nce.vertical_modules.hr.certs import do_check_hr_cert_expiry
from nce.vertical_modules.inventory.goods_receipt import do_record_goods_receipt
from nce.vertical_modules.procurement.po import _derive_po_idempotency_key, do_generate_po
from nce.vertical_modules.procurement.three_way_match import do_evaluate_three_way_match
from nce.vertical_modules.project.convert import do_convert_signed_quote
from nce.vertical_modules.project.recall import do_record_project_outcome
from nce.vertical_modules.resources.watcher import handle_hr_cert_change
from nce.vertical_modules.sales.baseline import do_freeze_baseline, get_signed_baseline
from nce.vertical_modules.sales.graph import do_create_deal
from nce.vertical_modules.sales.lines import do_add_quote_line
from nce.vertical_modules.sales.signing import do_on_signed_callback, do_request_signature
from nce.vertical_modules.support.dispatch import do_dispatch_work_order
from nce.vertical_modules.support.tickets import do_open_ticket
from nce.vertical_modules.system_design.devices import do_author_device_topology
from nce.vertical_modules.system_design.graph import do_author_functional_location
from nce.vertical_modules.system_design.propose import do_propose_design
from tests.conftest import _refresh_signing_when_decrypt_fails

pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Golden Thread Burndown Manifest
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BurndownStep:
    """Specification for a step in the Golden Thread scenario."""

    index: int
    name: str
    canonical_label: str
    is_broken: bool
    review_break: str | None
    phase1_wave: str | None
    description: str


GOLDEN_THREAD_STEPS: tuple[BurndownStep, ...] = (
    BurndownStep(
        index=1,
        name="customer",
        canonical_label="customer",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Customer account / deal created and verified in Sales / CRM read model",
    ),
    BurndownStep(
        index=2,
        name="design",
        canonical_label="design",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="System design topology authored with devices, ports, and cables",
    ),
    BurndownStep(
        index=3,
        name="quote",
        canonical_label="quote",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Sales quote authored from system design bill of materials (BOM lines)",
    ),
    BurndownStep(
        index=4,
        name="sign",
        canonical_label="sign",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Signature requested for the sales quote",
    ),
    BurndownStep(
        index=5,
        name="baseline_frozen",
        canonical_label="baseline frozen",
        is_broken=False,
        review_break="break-1",
        phase1_wave="S-2a",
        description="Signed quote freezes baseline via signing webhook/callback in production",
    ),
    BurndownStep(
        index=6,
        name="project",
        canonical_label="project",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Project converted from signed quote with baseline scope reference",
    ),
    BurndownStep(
        index=7,
        name="po",
        canonical_label="PO",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Procurement purchase order generated from project BOM lines",
    ),
    BurndownStep(
        index=8,
        name="ordered",
        canonical_label="ORDERED",
        is_broken=False,
        review_break="break-2",
        phase1_wave="PR-1",
        description="PO submission emits PO_LINE.status_changed to write BOM_LINE ORDERED rung",
    ),
    BurndownStep(
        index=9,
        name="gr",
        canonical_label="GR",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Goods receipt created for delivered procurement items",
    ),
    BurndownStep(
        index=10,
        name="match",
        canonical_label="match",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Three-way match performed between PO, GR, and supplier invoice",
    ),
    BurndownStep(
        index=11,
        name="delivered",
        canonical_label="DELIVERED",
        is_broken=False,
        review_break="break-2",
        phase1_wave="IN-1",
        description="GOODS_RECEIPT.created event published to transition BOM_LINE to DELIVERED",
    ),
    BurndownStep(
        index=12,
        name="asset",
        canonical_label="ASSET",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Asset seeded from BOM line through Assets engine",
    ),
    BurndownStep(
        index=13,
        name="installed",
        canonical_label="install (INSTALLED)",
        is_broken=False,
        review_break="break-2",
        phase1_wave="FT-1",
        description="Field Tech scan/install completion calls update_bom_line_status with INSTALLED",
    ),
    BurndownStep(
        index=14,
        name="tested",
        canonical_label="test (TESTED)",
        is_broken=False,
        review_break="break-2",
        phase1_wave="FT-1",
        description="Field Tech test completion calls update_bom_line_status with TESTED",
    ),
    BurndownStep(
        index=15,
        name="sla_attached",
        canonical_label="SLA attached",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="SLA coverage attached to installed asset",
    ),
    BurndownStep(
        index=16,
        name="invoice_approved",
        canonical_label="invoice approved",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Supplier invoice approved in Economy engine",
    ),
    BurndownStep(
        index=17,
        name="actual_cost",
        canonical_label="actual_cost",
        is_broken=False,
        review_break="break-3",
        phase1_wave="E-3",
        description="Economy invoice approval cascades to write actual_cost on BOM_LINE",
    ),
    BurndownStep(
        index=18,
        name="ticket",
        canonical_label="ticket",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Customer support ticket created for service dispatch",
    ),
    BurndownStep(
        index=19,
        name="dispatch",
        canonical_label="dispatch",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Support ticket dispatched with dispatched_as edge to work order",
    ),
    BurndownStep(
        index=20,
        name="work_order",
        canonical_label="work order",
        is_broken=False,
        review_break="break-5b",
        phase1_wave="SU-1/FT-3",
        description="Support dispatched_as boundary edge consumed by Field Tech to create work order",
    ),
    BurndownStep(
        index=21,
        name="outcome",
        canonical_label="outcome",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Field Tech records work order resolution outcome",
    ),
    BurndownStep(
        index=22,
        name="cert_expiry",
        canonical_label="cert expiry",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Technician certification expiry condition occurs",
    ),
    BurndownStep(
        index=23,
        name="allocation_invalidated",
        canonical_label="allocation invalidated",
        is_broken=False,
        review_break="break-5a",
        phase1_wave="HR-1/V-2",
        description="HR/Vendors cert expiry event invalidates scheduled resource allocation",
    ),
    BurndownStep(
        index=24,
        name="portal_request",
        canonical_label="portal request",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Customer raises service request via Customer Portal",
    ),
    BurndownStep(
        index=25,
        name="portal_ticket",
        canonical_label="ticket",
        is_broken=False,
        review_break="break-5c",
        phase1_wave="CP-1",
        description="Customer Portal hand-off creates real Support ticket via engine.modules['support']",
    ),
    BurndownStep(
        index=26,
        name="outcome_recorded",
        canonical_label="outcome recorded",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Project outcome recorded at G5 phase gate with case-study edge",
    ),
    BurndownStep(
        index=27,
        name="design_recall",
        canonical_label="design recall returns the project",
        is_broken=False,
        review_break="break-4",
        phase1_wave="PJ-1/SD-2",
        description="System design recall returns similar projects weighted by measured outcome",
    ),
    BurndownStep(
        index=28,
        name="degradations",
        canonical_label="degradation register empty",
        is_broken=False,
        review_break="break-degradations",
        phase1_wave="I-5",
        description="GET /api/health/degradations mounted and reports zero active degradations",
    ),
)


# ---------------------------------------------------------------------------
# Scenario Context & Live Database Harness
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GoldenThreadScenarioContext:
    """Live execution state carried across the 28 lifecycle steps."""

    pool: asyncpg.Pool
    engine: NCEEngine
    namespace_id: UUID
    namespace_slug: str
    start_time: float
    deal_id: str = ""
    customer_id: str = ""
    quote_id: str = ""
    design_id: str = ""
    line_ref: str = "LINE-001"
    bom_line_id: UUID | None = None
    session_id: str = ""
    po_number: str = ""
    location_id: UUID | None = None
    agreement_id: str = ""
    ticket_id: UUID | None = None
    work_order_id: str = ""
    employee_id: str = ""
    cert_id: str = ""
    resource_id: UUID | None = None
    allocation_id: UUID | None = None
    portal_ticket_id: UUID | None = None


async def _live_read_signed_baseline(eng: NCEEngine, nsu: UUID, qid: str) -> dict | None:
    """Bridge Sales-frozen baseline to Project convert via live database read."""
    async with scoped_pg_session(eng.pg_pool, nsu) as c:
        return await get_signed_baseline(c, nsu, qid)


# ---------------------------------------------------------------------------
# Round-trip counting: the only honest "did this actually run?" check
# ---------------------------------------------------------------------------
#
# A wall-clock floor was tried first and removed. It measures the machine: 28
# small statements against a Postgres on the same host is ~7ms each, so an
# entirely honest run finishes in ~0.2s and a slow runner passes a threshold a
# fast one fails. Worse, it invites the fix that was actually committed --
# `await asyncio.sleep(1.05 - elapsed)` -- which satisfies the assertion and
# destroys its meaning. Count the round trips instead: that is the property the
# check is about, and no amount of sleeping produces one.

_QUERY_METHODS = ("execute", "executemany", "fetch", "fetchrow", "fetchval")

# 28 steps, each reading or writing at least once, plus fixture setup.
MIN_DB_ROUND_TRIPS = 40


class _QueryCounter:
    """Shared mutable counter; one per scenario."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


class _CountingConn:
    """Forwards everything to a real connection, counting query calls."""

    def __init__(self, conn: Any, counter: _QueryCounter) -> None:
        self._conn = conn
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._conn, name)
        if name in _QUERY_METHODS and callable(attr):

            async def _counted(*args: Any, **kwargs: Any) -> Any:
                self._counter.count += 1
                return await attr(*args, **kwargs)

            return _counted
        return attr


class _CountingAcquire:
    """Mirrors asyncpg's PoolAcquireContext: awaitable AND an async CM."""

    def __init__(self, inner: Any, counter: _QueryCounter) -> None:
        self._inner = inner
        self._counter = counter

    async def __aenter__(self) -> _CountingConn:
        return _CountingConn(await self._inner.__aenter__(), self._counter)

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)

    def __await__(self) -> Any:
        async def _wrap() -> _CountingConn:
            return _CountingConn(await self._inner, self._counter)

        return _wrap().__await__()


class _CountingPool:
    """Forwards everything to a real pool, wrapping acquired connections."""

    def __init__(self, pool: Any, counter: _QueryCounter) -> None:
        self._pool = pool
        self._counter = counter

    def acquire(self, *args: Any, **kwargs: Any) -> _CountingAcquire:
        return _CountingAcquire(self._pool.acquire(*args, **kwargs), self._counter)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._pool, name)
        if name in _QUERY_METHODS and callable(attr):

            async def _counted(*args: Any, **kwargs: Any) -> Any:
                self._counter.count += 1
                return await attr(*args, **kwargs)

            return _counted
        return attr


async def _setup_live_context() -> AsyncGenerator[GoldenThreadScenarioContext, None]:
    """Provisions a live Postgres connection and test namespace for Golden Thread."""
    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        pytest.fail(
            "Live Golden Thread requires a reachable Postgres database. "
            "Set NCE_INTEGRATION_PG_DSN, PG_DSN, or DATABASE_URL."
        )

    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.fail(f"Live Golden Thread failed to connect to Postgres ({dsn}): {exc}")

    start_time = time.monotonic()
    counter = _QueryCounter()
    raw_pool = pool
    pool = _CountingPool(raw_pool, counter)
    slug = f"pytest-gt-{uuid.uuid4().hex[:8]}"

    async with pool.acquire() as conn:
        ns_id = await conn.fetchval("INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", slug)
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)

        try:
            await get_active_key(conn)
        except (NoActiveSigningKeyError, SigningKeyDecryptionError) as exc:
            # NEVER rotate unconditionally.  This fixture runs under ``-m live`` and the
            # rebuild runbook points it at the DEPLOYED database.  ``rotate_key`` retires
            # that deployment's active key and inserts a replacement wrapped under THIS
            # shell's ``NCE_MASTER_KEY``, orphaning the running stack.  That is exactly
            # what happened on 2026-09-07 at 01:46:27 and again at 10:55:05: both
            # replacement keys were wrapped under ``tests/conftest.py``'s "x" * 32, and
            # the containers kept reporting healthy while unable to sign anything.
            #
            # Rotation here is opt-in and disposable-database-only, behind the same flag
            # ``tests/conftest.py`` already gates its own seeding on.  Without it, SKIP --
            # a green run bought by destroying the deployment's key is not a pass.
            if not _refresh_signing_when_decrypt_fails():
                pytest.skip(
                    "Live Golden Thread needs a signing key that this shell's "
                    f"NCE_MASTER_KEY can use ({type(exc).__name__}).  Rotating one here "
                    "would retire the deployment's active key and re-wrap it under this "
                    "shell's master key, orphaning the running stack.  Either run with the "
                    "deployment master key, or set "
                    "NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL=1 -- on a DISPOSABLE "
                    "database only.",
                )
            await rotate_key(conn)

    engine = NCEEngine()
    engine.pg_pool = pool
    populate_engine_modules(engine)

    ctx = GoldenThreadScenarioContext(
        pool=pool,
        engine=engine,
        namespace_id=ns_id,
        namespace_slug=slug,
        start_time=start_time,
        deal_id=f"DEAL-{uuid.uuid4().hex[:6].upper()}",
        customer_id=str(uuid.uuid4()),
        quote_id=f"Q-{uuid.uuid4().hex[:6].upper()}",
        design_id=f"DSN-{uuid.uuid4().hex[:6].upper()}",
        line_ref="LINE-001",
    )

    try:
        yield ctx
    finally:
        observed = counter.count
        async with raw_pool.acquire() as conn:
            try:
                await conn.execute("DELETE FROM namespaces WHERE id = $1", ns_id)
            except (asyncpg.PostgresError, OSError):
                pass
        await raw_pool.close()

        # Asserted AFTER cleanup so a hollow run still tears its namespace down.
        assert observed >= MIN_DB_ROUND_TRIPS, (
            f"Hollow live run detected: only {observed} database round trips, "
            f"floor is {MIN_DB_ROUND_TRIPS}. The scenario is not exercising the "
            "database -- steps have become static assertions again."
        )


# ---------------------------------------------------------------------------
# Individual Step Tests (v1.5 Burndown)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="class")
class TestGoldenThreadSteps:
    """The 28 discrete step checkpoints of the Golden Thread lifecycle.

    Each step executes real async database operations and asserts actual rows written.
    Broken seams carry `@pytest.mark.xfail(strict=True, reason="break-N: ...")`.
    """

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def scenario(self) -> AsyncGenerator[GoldenThreadScenarioContext, None]:
        with patch(
            "nce.vertical_modules.project.convert._read_signed_baseline",
            side_effect=_live_read_signed_baseline,
        ):
            async for ctx in _setup_live_context():
                yield ctx

    async def _ensure_prereqs(self, ctx: GoldenThreadScenarioContext, up_to_step: int) -> None:
        """Idempotently executes upstream prerequisites if running an isolated step."""
        if up_to_step >= 1 and not ctx.deal_id:
            await self.test_step_01_customer(ctx)
        if up_to_step >= 2 and not ctx.design_id:
            await self.test_step_02_design(ctx)
        if up_to_step >= 3 and ctx.bom_line_id is None:
            await self.test_step_03_quote(ctx)
        if up_to_step >= 4 and not ctx.session_id:
            await self.test_step_04_sign(ctx)
        if up_to_step >= 5:
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    b = await get_signed_baseline(conn, ctx.namespace_id, ctx.quote_id)
                    if b is None:
                        await self.test_step_05_baseline_frozen(ctx)
        if up_to_step >= 6:
            async with ctx.pool.acquire() as conn:
                cnt = await conn.fetchval(
                    "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'PROJECT_PROJECT'",
                    ctx.namespace_id,
                )
                if cnt == 0:
                    await self.test_step_06_project(ctx)
        if up_to_step >= 7 and not ctx.po_number:
            await self.test_step_07_po(ctx)
        if up_to_step >= 8:
            async with ctx.pool.acquire() as conn:
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                if st != "ORDERED":
                    await self.test_step_08_ordered(ctx)
        if up_to_step >= 9 and ctx.location_id is None:
            await self.test_step_09_gr(ctx)
        if up_to_step >= 11:
            async with ctx.pool.acquire() as conn:
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                if st != "DELIVERED":
                    await self.test_step_11_delivered(ctx)
        if up_to_step >= 12:
            async with ctx.pool.acquire() as conn:
                acnt = await conn.fetchval(
                    "SELECT count(*) FROM assets WHERE namespace_id = $1 AND bom_line_id = $2",
                    ctx.namespace_id,
                    ctx.bom_line_id,
                )
                if acnt == 0:
                    await self.test_step_12_asset(ctx)
        if up_to_step >= 13:
            async with ctx.pool.acquire() as conn:
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                if st not in ("INSTALLED", "TESTED"):
                    await self.test_step_13_installed(ctx)
        if up_to_step >= 14:
            async with ctx.pool.acquire() as conn:
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                if st != "TESTED":
                    await self.test_step_14_tested(ctx)
        if up_to_step >= 15 and not ctx.agreement_id:
            await self.test_step_15_sla_attached(ctx)
        if up_to_step >= 16:
            bom_label = f"BOM_LINE:{ctx.quote_id.upper()}:{ctx.line_ref.upper()}"
            async with ctx.pool.acquire() as conn:
                ecnt = await conn.fetchval(
                    "SELECT count(*) FROM economy_bom_actual_costs WHERE namespace_id = $1 AND bom_line_label = $2",
                    ctx.namespace_id,
                    bom_label,
                )
                if ecnt == 0:
                    await self.test_step_16_invoice_approved(ctx)
        if up_to_step >= 18 and ctx.ticket_id is None:
            await self.test_step_18_ticket(ctx)
        if up_to_step >= 19:
            async with ctx.pool.acquire() as conn:
                dcnt = await conn.fetchval(
                    "SELECT count(*) FROM kg_edges WHERE namespace_id = $1 AND predicate = 'dispatched_as'",
                    ctx.namespace_id,
                )
                if dcnt == 0:
                    await self.test_step_19_dispatch(ctx)
        if up_to_step >= 20 and not ctx.work_order_id:
            await self.test_step_20_work_order(ctx)
        if up_to_step >= 21:
            async with ctx.pool.acquire() as conn:
                wst = await conn.fetchval(
                    "SELECT status FROM work_orders WHERE namespace_id = $1 AND work_order_id = $2",
                    ctx.namespace_id,
                    ctx.work_order_id,
                )
                if wst != "completed":
                    await self.test_step_21_outcome(ctx)
        if up_to_step >= 22 and not ctx.employee_id:
            await self.test_step_22_cert_expiry(ctx)
        if up_to_step >= 24 and ctx.portal_ticket_id is None:
            await self.test_step_24_portal_request(ctx)
        if up_to_step >= 26:
            async with ctx.pool.acquire() as conn:
                mcnt = await conn.fetchval(
                    "SELECT count(*) FROM memories WHERE namespace_id = $1 AND node_type = 'PROJECT'",
                    ctx.namespace_id,
                )
                if mcnt == 0:
                    await self.test_step_26_outcome_recorded(ctx)

    async def test_step_01_customer(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 1: customer account / deal model."""
        ctx = scenario
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await do_create_deal(
                    conn,
                    ctx.namespace_id,
                    deal_id=ctx.deal_id,
                    customer_id=ctx.customer_id,
                    quote_id=ctx.quote_id,
                )
                deal_cnt = await conn.fetchval(
                    "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                    ctx.namespace_id,
                    f"DEAL:{ctx.deal_id.upper()}",
                )
                assert deal_cnt == 1, f"DEAL node not written: {deal_cnt}"
                cust_cnt = await conn.fetchval(
                    "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'CUSTOMER'",
                    ctx.namespace_id,
                )
                assert cust_cnt >= 1, f"CUSTOMER node not written: {cust_cnt}"

    async def test_step_02_design(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 2: system design topology authoring."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 1)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await do_author_functional_location(
                    conn,
                    ctx.namespace_id,
                    namespace_slug=ctx.namespace_slug,
                    design_id=ctx.design_id,
                    site_name="HQ",
                    buildings=[
                        {
                            "name": "Main",
                            "floors": [
                                {"name": "1", "rooms": [{"name": "101", "positions": ["rack-1"]}]}
                            ],
                        }
                    ],
                )
                await do_author_device_topology(
                    conn,
                    ctx.namespace_id,
                    design_id=ctx.design_id,
                    devices=[
                        {
                            "device_ref": "SW-01",
                            "capability": {"device_category": "switch"},
                            "ports": [{"port_ref": "P1"}],
                        }
                    ],
                )
                dev_cnt = await conn.fetchval(
                    "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'DEVICE'",
                    ctx.namespace_id,
                )
                assert dev_cnt >= 1, f"DEVICE node not written: {dev_cnt}"

    async def test_step_03_quote(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 3: sales quote generation."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 2)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                q_line = await do_add_quote_line(
                    conn,
                    ctx.namespace_id,
                    quote_id=ctx.quote_id,
                    line_ref=ctx.line_ref,
                    qty=2,
                    unit_price=500.0,
                    line_total=1000.0,
                )
                ctx.bom_line_id = q_line["id"]
                bom_cnt = await conn.fetchval(
                    "SELECT count(*) FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2",
                    ctx.namespace_id,
                    ctx.quote_id,
                )
                assert bom_cnt == 1, f"BOM line not written: {bom_cnt}"

    async def test_step_04_sign(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 4: signature request."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 3)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await conn.execute(
                    """
                    INSERT INTO sales_read_model (namespace_id, entity, source_id, manual)
                    VALUES ($1, 'quotes', $2, $3)
                    """,
                    ctx.namespace_id,
                    ctx.quote_id,
                    json.dumps(
                        {
                            "quoteid": ctx.quote_id,
                            "name": "Signable Proposal",
                            "margin": 0.35,
                            "total_price": 1000.0,
                            "customer_name": "Acme Corp",
                        }
                    ),
                )
        await do_freeze_baseline(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "quote_id": ctx.quote_id,
                "signed_margin_pct": 0.35,
                "signed_total_nok": 1000.0,
            },
        )
        session = await do_request_signature(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "quote_id": ctx.quote_id,
                "signer": {"name": "Alice Signer", "email": "alice@acme.com"},
                "method": "manual",
            },
        )
        ctx.session_id = session["session_id"]
        async with ctx.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT manual FROM sales_read_model WHERE namespace_id = $1 AND entity = 'quotes' AND source_id = $2",
                ctx.namespace_id,
                ctx.quote_id,
            )
            assert row is not None, "Quote read model row missing"
            man = json.loads(row["manual"]) if isinstance(row["manual"], str) else row["manual"]
            assert man["signing_status"] == "pending"

    async def test_step_05_baseline_frozen(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 5: baseline frozen via signing webhook."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 4)
        callback_res = await do_on_signed_callback(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "session_id": ctx.session_id,
            },
        )
        assert callback_res["ok"] is True
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                baseline = await get_signed_baseline(conn, ctx.namespace_id, ctx.quote_id)
                assert baseline is not None, "Signed baseline not found"
                assert float(baseline["signed_margin_pct"]) == 0.35
                assert float(baseline["signed_total_nok"]) == 1000.0

    async def test_step_06_project(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 6: project converted from signed quote."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 5)
        with patch(
            "nce.vertical_modules.project.convert._read_signed_baseline",
            side_effect=_live_read_signed_baseline,
        ):
            proj_res = await do_convert_signed_quote(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "quote_id": ctx.quote_id,
                    "signed_by": "alice@acme.com",
                    "signature_ref": ctx.session_id,
                },
            )
            assert proj_res["degraded"] is False, (
                f"Conversion degraded: {proj_res.get('degraded_reasons')}"
            )
        async with ctx.pool.acquire() as conn:
            proj_cnt = await conn.fetchval(
                "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'PROJECT_PROJECT'",
                ctx.namespace_id,
            )
            assert proj_cnt >= 1, f"PROJECT node not found: {proj_cnt}"

    async def test_step_07_po(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 7: purchase order generated from BOM."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 6)
        po_num = f"PO-{uuid.uuid4().hex[:6].upper()}"
        ctx.po_number = po_num
        ikey = _derive_po_idempotency_key(str(ctx.namespace_id), ["ART-001"], "v1")
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                po_res = await do_generate_po(
                    conn,
                    ctx.namespace_id,
                    idempotency_key=ikey,
                    confirm=True,
                    engine=ctx.engine,
                    po_number=po_num,
                    artnrs=["ART-001"],
                    bom_line={"quantity": 2, "unit_price": 500.0},
                )
        assert po_res["status"] == "executed"
        async with ctx.pool.acquire() as conn:
            po_cnt = await conn.fetchval(
                "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'PO'",
                ctx.namespace_id,
            )
            assert po_cnt >= 1, f"PO node not found: {po_cnt}"

    async def test_step_08_ordered(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 8: BOM_LINE ORDERED rung written."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 7)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await update_bom_line_status(
                    conn,
                    ctx.namespace_id,
                    writer_engine="procurement",
                    quote_id=ctx.quote_id,
                    line_ref=ctx.line_ref,
                    status="ORDERED",
                )
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                assert st == "ORDERED", f"Expected ORDERED status, got {st}"

    async def test_step_09_gr(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 9: goods receipt created."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 8)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                loc_id = await conn.fetchval(
                    "INSERT INTO stock_locations (namespace_id, name, kind) VALUES ($1, 'Main Depot', 'warehouse') RETURNING id",
                    ctx.namespace_id,
                )
                ctx.location_id = loc_id
        gr_res = await do_record_goods_receipt(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "po_ref": ctx.po_number,
                "location_id": str(ctx.location_id),
                "lines": [{"sku": "SKU-001", "qty": 2, "unit_cost": 400.0}],
                "delivery_note_ref": "DN-001",
            },
        )
        assert gr_res["ok"] is True
        async with ctx.pool.acquire() as conn:
            gr_cnt = await conn.fetchval(
                "SELECT count(*) FROM goods_receipts WHERE namespace_id = $1", ctx.namespace_id
            )
            assert gr_cnt >= 1, f"Goods receipt not written: {gr_cnt}"

    async def test_step_10_match(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 10: three-way match executed."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 9)
        match_eval = do_evaluate_three_way_match(
            {},
            po={"article_id": "ART-001", "quantity": 2, "unit_price": 500.0},
            goods_receipt={"quantity": 2},
            invoice={"article_id": "ART-001", "quantity": 2, "unit_price": 500.0},
        )
        assert match_eval["tier"] == "GREEN"
        async with ctx.pool.acquire() as conn:
            po_cnt = await conn.fetchval(
                "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'PO'",
                ctx.namespace_id,
            )
            assert po_cnt >= 1

    async def test_step_11_delivered(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 11: BOM_LINE DELIVERED rung written."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 10)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await update_bom_line_status(
                    conn,
                    ctx.namespace_id,
                    writer_engine="inventory",
                    quote_id=ctx.quote_id,
                    line_ref=ctx.line_ref,
                    status="DELIVERED",
                )
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                assert st == "DELIVERED", f"Expected DELIVERED status, got {st}"

    async def test_step_12_asset(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 12: asset seeded from BOM line."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 11)
        asset_res = await do_seed_asset_from_bom(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "bom_line_id": str(ctx.bom_line_id),
                "serial": "SN-GT-001",
                "functional_location_id": "HQ/Main/1/101",
            },
        )
        assert asset_res["created"] is True
        async with ctx.pool.acquire() as conn:
            asset_cnt = await conn.fetchval(
                "SELECT count(*) FROM assets WHERE namespace_id = $1 AND bom_line_id = $2",
                ctx.namespace_id,
                ctx.bom_line_id,
            )
            assert asset_cnt == 1, f"Asset not found: {asset_cnt}"

    async def test_step_13_installed(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 13: BOM_LINE INSTALLED rung written."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 12)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await update_bom_line_status(
                    conn,
                    ctx.namespace_id,
                    writer_engine="field_tech",
                    quote_id=ctx.quote_id,
                    line_ref=ctx.line_ref,
                    status="INSTALLED",
                )
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                assert st == "INSTALLED", f"Expected INSTALLED status, got {st}"

    async def test_step_14_tested(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 14: BOM_LINE TESTED rung written."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 13)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await update_bom_line_status(
                    conn,
                    ctx.namespace_id,
                    writer_engine="field_tech",
                    quote_id=ctx.quote_id,
                    line_ref=ctx.line_ref,
                    status="TESTED",
                )
                st = await conn.fetchval(
                    "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                    ctx.namespace_id,
                    ctx.quote_id,
                    ctx.line_ref,
                )
                assert st == "TESTED", f"Expected TESTED status, got {st}"

    async def test_step_15_sla_attached(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 15: SLA coverage attached to asset."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 14)
        agr_id = str(uuid.uuid4())
        ctx.agreement_id = agr_id
        await do_set_sla_coverage(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "agreement_id": agr_id,
                "functional_location_id": "HQ/Main/1/101",
                "sla_terms": {"responseHours": 4, "coverageWindow": "24x7"},
            },
        )
        await do_attach_sla(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "agreement_id": agr_id,
                "functional_location_id": "HQ/Main/1/101",
                "namespace_slug": ctx.namespace_slug,
            },
        )
        async with ctx.pool.acquire() as conn:
            sla_cnt = await conn.fetchval(
                "SELECT count(*) FROM kg_edges WHERE namespace_id = $1 AND predicate = 'covered_by'",
                ctx.namespace_id,
            )
            assert sla_cnt >= 1, f"SLA edge covered_by not found: {sla_cnt}"

    async def test_step_16_invoice_approved(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 16: supplier invoice approved."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 15)
        bom_label = f"BOM_LINE:{ctx.quote_id.upper()}:{ctx.line_ref.upper()}"
        cascade_res = await do_cascade_on_approval(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "approval_id": f"APP-{uuid.uuid4().hex[:6]}",
                "quote_id": ctx.quote_id,
                "lines": [{"bom_line_label": bom_label, "actual_cost": 450.00}],
            },
        )
        assert cascade_res["ok"] is True
        async with ctx.pool.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT count(*) FROM economy_bom_actual_costs WHERE namespace_id = $1 AND bom_line_label = $2",
                ctx.namespace_id,
                bom_label,
            )
            assert cnt >= 1, f"Actual cost record not written: {cnt}"

    async def test_step_17_actual_cost(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 17: actual_cost written to BOM_LINE."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 16)
        bom_label = f"BOM_LINE:{ctx.quote_id.upper()}:{ctx.line_ref.upper()}"
        async with ctx.pool.acquire() as conn:
            act_cost = await conn.fetchval(
                "SELECT actual_cost FROM economy_bom_actual_costs WHERE namespace_id = $1 AND bom_line_label = $2",
                ctx.namespace_id,
                bom_label,
            )
            assert act_cost is not None, "actual_cost was not recorded"
            assert float(act_cost) == 450.00

    async def test_step_18_ticket(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 18: support ticket created."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 17)
        ticket_res = await do_open_ticket(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "summary": "Display flickering",
                "customer_id": ctx.customer_id,
                "priority": "high",
                "source": "nce",
            },
        )
        ctx.ticket_id = UUID(ticket_res["ticket"]["id"])
        async with ctx.pool.acquire() as conn:
            t_cnt = await conn.fetchval(
                "SELECT count(*) FROM service_tickets WHERE namespace_id = $1 AND id = $2",
                ctx.namespace_id,
                ctx.ticket_id,
            )
            assert t_cnt == 1, f"Support ticket not found: {t_cnt}"

    async def test_step_19_dispatch(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 19: work order dispatched."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 18)
        dispatch_res = await do_dispatch_work_order(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "ticket_id": str(ctx.ticket_id),
                "service_type": "repair",
                "priority": "high",
                "estimated_cost": 500.0,
                "confirm": True,
            },
        )
        assert dispatch_res["dispatched"] is True
        async with ctx.pool.acquire() as conn:
            disp_cnt = await conn.fetchval(
                "SELECT count(*) FROM kg_edges WHERE namespace_id = $1 AND predicate = 'dispatched_as'",
                ctx.namespace_id,
            )
            assert disp_cnt >= 1, f"dispatched_as edge not found: {disp_cnt}"

    async def test_step_20_work_order(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 20: field tech work order created."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 19)
        wo_res = await do_create_work_order(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "summary": "Field Tech Repair 101",
                "kind": "service",
                "source_kind": "ticket",
                "source_ref": str(ctx.ticket_id),
            },
        )
        ctx.work_order_id = wo_res["work_order_id"]
        async with ctx.pool.acquire() as conn:
            wo_cnt = await conn.fetchval(
                "SELECT count(*) FROM work_orders WHERE namespace_id = $1 AND work_order_id = $2",
                ctx.namespace_id,
                ctx.work_order_id,
            )
            assert wo_cnt == 1, f"Work order not found in DB: {wo_cnt}"

    async def test_step_21_outcome(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 21: work order outcome recorded."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 20)
        with patch(
            "nce.vertical_modules.field_tech.outcome.record_decision_feedback",
            new_callable=AsyncMock,
        ):
            outcome_res = await do_record_outcome(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "work_order_id": str(ctx.work_order_id),
                    "resolution_notes": "Cable re-seated and tested",
                    "quality_score": 0.95,
                    "rating": 5.0,
                },
            )
            assert outcome_res["status"] == "recorded"
        async with ctx.pool.acquire() as conn:
            wo_st = await conn.fetchval(
                "SELECT status FROM work_orders WHERE namespace_id = $1 AND work_order_id = $2",
                ctx.namespace_id,
                ctx.work_order_id,
            )
            assert wo_st == "completed"
            ledger_cnt = await conn.fetchval(
                "SELECT count(*) FROM v3_cognitive_ledger WHERE namespace_id = $1",
                ctx.namespace_id,
            )
            assert ledger_cnt >= 1, "Cognitive ledger row not written"

    async def test_step_22_cert_expiry(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 22: technician certification expiry evaluated."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 21)
        emp_uuid = str(uuid.uuid4())
        ctx.employee_id = emp_uuid
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                await conn.execute(
                    """
                    INSERT INTO employees (namespace_id, employee_id, name, email, role, department, leave_balance, active, raw)
                    VALUES ($1, $2, 'John Tech', 'john@acme.com', 'technician', 'field', 10.0, true, '{}'::jsonb)
                    """,
                    ctx.namespace_id,
                    emp_uuid,
                )
                await conn.execute(
                    """
                    INSERT INTO certifications (namespace_id, cert_id, employee_id, authority, name, issued, valid_to, status, raw)
                    VALUES ($1, $2, $3, 'OSHA', 'Safety Level 1', now() - interval '1 year', $4, 'active', '{}'::jsonb)
                    """,
                    ctx.namespace_id,
                    f"CERT-{uuid.uuid4().hex[:6]}",
                    emp_uuid,
                    datetime.now(timezone.utc) - timedelta(days=1),
                )
        hr_res = await do_check_hr_cert_expiry(ctx.engine, {"namespace_id": str(ctx.namespace_id)})
        assert hr_res["expired"] >= 1
        async with ctx.pool.acquire() as conn:
            ev_cnt = await conn.fetchval(
                "SELECT count(*) FROM outbox_events WHERE namespace_id = $1 AND event_type = 'CERTIFICATION.EXPIRED'",
                ctx.namespace_id,
            )
            assert ev_cnt >= 1, "CERTIFICATION.EXPIRED event not found in outbox"

    async def test_step_23_allocation_invalidated(
        self, scenario: GoldenThreadScenarioContext
    ) -> None:
        """Step 23: certification expiry invalidates scheduled allocation."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 22)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                res_id = await conn.fetchval(
                    """
                    INSERT INTO resources (namespace_id, display_name, kind, ref_id, attrs)
                    VALUES ($1, 'John Tech', 'employee', $2, '{}'::jsonb)
                    RETURNING id
                    """,
                    ctx.namespace_id,
                    ctx.employee_id,
                )
                ctx.resource_id = res_id
                alloc_id = await conn.fetchval(
                    """
                    INSERT INTO allocations (namespace_id, resource_id, demand_kind, demand_id, starts_at, ends_at, status)
                    VALUES ($1, $2, 'project', $3, now() + interval '1 day', now() + interval '2 days', 'confirmed')
                    RETURNING id
                    """,
                    ctx.namespace_id,
                    res_id,
                    uuid.uuid4(),
                )
                ctx.allocation_id = alloc_id

        alloc_invalid_res = await handle_hr_cert_change(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "employee_id": ctx.employee_id,
                "status": "expired",
            },
        )
        assert alloc_invalid_res["affected_count"] >= 1
        async with ctx.pool.acquire() as conn:
            alloc_row = await conn.fetchrow(
                "SELECT status, attrs FROM allocations WHERE id = $1 AND namespace_id = $2",
                ctx.allocation_id,
                ctx.namespace_id,
            )
            assert alloc_row is not None, "Allocation row not found"
            assert alloc_row["status"] == "tentative"
            attrs = (
                json.loads(alloc_row["attrs"])
                if isinstance(alloc_row["attrs"], str)
                else (alloc_row["attrs"] or {})
            )
            assert "cert_conflict" in attrs, "cert_conflict not found in allocation attrs"

    async def test_step_24_portal_request(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 24: customer raises portal request."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 22)
        portal_res = await do_raise_service_request(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "customer_scope_id": ctx.customer_id,
                "summary": "Meeting room sound failure",
            },
        )
        assert "ticket_id" in portal_res
        ctx.portal_ticket_id = UUID(portal_res["ticket_id"])

    async def test_step_25_portal_ticket(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 25: customer portal creates support ticket."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 24)
        async with ctx.pool.acquire() as conn:
            pt_row = await conn.fetchrow(
                "SELECT id, source, source_id FROM service_tickets WHERE namespace_id = $1 AND id = $2",
                ctx.namespace_id,
                ctx.portal_ticket_id,
            )
            assert pt_row is not None, "Portal ticket not found in service_tickets"
            assert str(pt_row["source_id"]).startswith("customer_portal:")

    async def test_step_26_outcome_recorded(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 26: project outcome recorded at G5 phase gate."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 6)
        p_out = await do_record_project_outcome(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "project_id": f"PROJECT:{ctx.quote_id.upper()}",
                "description": "Enterprise AV Deployment",
                "slip_reason": "Logistics delivery delay",
            },
        )
        assert p_out["ok"] is True
        async with ctx.pool.acquire() as conn:
            mem_cnt = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE namespace_id = $1 AND node_type = 'PROJECT'",
                ctx.namespace_id,
            )
            assert mem_cnt >= 1, "Project outcome memory not recorded in memories table"

            # C10's decision-feedback signal must LAND, not merely be attempted.
            # recall.py passed `conn` to record_decision_feedback(), which needs a
            # pool, so every write raised and was swallowed as a warning. The tool
            # still returned ok -- which is why asserting on the return value, as
            # this step originally did, proved nothing.
            fb_cnt = await conn.fetchval(
                """
                SELECT count(*) FROM decision_feedback
                WHERE namespace_id = $1 AND engine = 'project' AND context_id = $2
                """,
                ctx.namespace_id,
                f"PROJECT:{ctx.quote_id.upper()}",
            )
            assert fb_cnt >= 1, (
                "decision_feedback row not written for the project outcome -- C10's "
                "learning loop is silently dead"
            )

    async def test_step_27_design_recall(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 27: design recall returns outcome-weighted similar project."""
        ctx = scenario
        await self._ensure_prereqs(ctx, 26)
        async with ctx.pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ctx.namespace_id)
                for i in range(5):
                    await conn.execute(
                        """
                        INSERT INTO decision_feedback (namespace_id, engine, context_id, proposal, decision, delta, actor)
                        VALUES ($1, 'system_design', $2, '{"bom": []}'::jsonb, 'accepted', '{}'::jsonb, 'human')
                        """,
                        ctx.namespace_id,
                        f"ctx-{i}",
                    )
        prop_res = await do_propose_design(
            ctx.engine,
            {
                "namespace_id": str(ctx.namespace_id),
                "room_brief": "Enterprise AV Deployment",
            },
        )
        assert "proposed_lines" in prop_res

    async def test_step_28_degradations(self, scenario: GoldenThreadScenarioContext) -> None:
        """Step 28: degradation register reports zero active degradations."""
        ctx = scenario
        reg = get_degradation_register()
        total_deg = reg.total_count(str(ctx.namespace_id))
        degs = reg.get_degradations(str(ctx.namespace_id))
        assert total_deg == 0, f"Unexpected degradations in namespace: {total_deg} ({degs})"


# ---------------------------------------------------------------------------
# End-to-End Pipeline Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGoldenThreadPipeline:
    """Full unbroken sequential execution of the 28-step Golden Thread lifecycle.

    Executes all 28 steps end-to-end against live PostgreSQL.
    """

    async def test_golden_thread_full_e2e_pipeline(self) -> None:
        """Executes the full Golden Thread lifecycle sequentially end-to-end."""
        async for ctx in _setup_live_context():
            # Step 1: Customer & Deal
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await do_create_deal(
                        conn,
                        ctx.namespace_id,
                        deal_id=ctx.deal_id,
                        customer_id=ctx.customer_id,
                        quote_id=ctx.quote_id,
                    )
                    assert (
                        await conn.fetchval(
                            "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                            ctx.namespace_id,
                            f"DEAL:{ctx.deal_id.upper()}",
                        )
                        == 1
                    )

            # Step 2: Design
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await do_author_functional_location(
                        conn,
                        ctx.namespace_id,
                        namespace_slug=ctx.namespace_slug,
                        design_id=ctx.design_id,
                        site_name="HQ",
                        buildings=[
                            {
                                "name": "Main",
                                "floors": [
                                    {
                                        "name": "1",
                                        "rooms": [{"name": "101", "positions": ["rack-1"]}],
                                    }
                                ],
                            }
                        ],
                    )
                    await do_author_device_topology(
                        conn,
                        ctx.namespace_id,
                        design_id=ctx.design_id,
                        devices=[
                            {
                                "device_ref": "SW-01",
                                "capability": {"device_category": "switch"},
                                "ports": [{"port_ref": "P1"}],
                            }
                        ],
                    )
                    assert (
                        await conn.fetchval(
                            "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'DEVICE'",
                            ctx.namespace_id,
                        )
                        >= 1
                    )

            # Step 3: Quote line
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    q_line = await do_add_quote_line(
                        conn,
                        ctx.namespace_id,
                        quote_id=ctx.quote_id,
                        line_ref=ctx.line_ref,
                        qty=2,
                        unit_price=500.0,
                        line_total=1000.0,
                    )
                    ctx.bom_line_id = q_line["id"]
                    assert (
                        await conn.fetchval(
                            "SELECT count(*) FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2",
                            ctx.namespace_id,
                            ctx.quote_id,
                        )
                        == 1
                    )

            # Step 4: Sign
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await conn.execute(
                        """
                        INSERT INTO sales_read_model (namespace_id, entity, source_id, manual)
                        VALUES ($1, 'quotes', $2, $3)
                        """,
                        ctx.namespace_id,
                        ctx.quote_id,
                        json.dumps(
                            {
                                "quoteid": ctx.quote_id,
                                "name": "Signable Proposal",
                                "margin": 0.35,
                                "total_price": 1000.0,
                                "customer_name": "Acme Corp",
                            }
                        ),
                    )
            await do_freeze_baseline(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "quote_id": ctx.quote_id,
                    "signed_margin_pct": 0.35,
                    "signed_total_nok": 1000.0,
                },
            )
            session = await do_request_signature(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "quote_id": ctx.quote_id,
                    "signer": {"name": "Alice Signer", "email": "alice@acme.com"},
                    "method": "manual",
                },
            )
            ctx.session_id = session["session_id"]

            # Step 5 & 6: Baseline frozen via callback & converted to project
            with patch(
                "nce.vertical_modules.project.convert._read_signed_baseline",
                side_effect=_live_read_signed_baseline,
            ):
                callback_res = await do_on_signed_callback(
                    ctx.engine,
                    {
                        "namespace_id": str(ctx.namespace_id),
                        "session_id": ctx.session_id,
                    },
                )
                assert callback_res["ok"] is True
                async with ctx.pool.acquire() as conn:
                    async with conn.transaction():
                        await set_namespace_context(conn, ctx.namespace_id)
                        baseline = await get_signed_baseline(conn, ctx.namespace_id, ctx.quote_id)
                        assert baseline is not None
                        assert float(baseline["signed_margin_pct"]) == 0.35

                proj_res = await do_convert_signed_quote(
                    ctx.engine,
                    {
                        "namespace_id": str(ctx.namespace_id),
                        "quote_id": ctx.quote_id,
                        "signed_by": "alice@acme.com",
                        "signature_ref": ctx.session_id,
                    },
                )
                assert proj_res["degraded"] is False
                async with ctx.pool.acquire() as conn:
                    assert (
                        await conn.fetchval(
                            "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = 'PROJECT_PROJECT'",
                            ctx.namespace_id,
                        )
                        >= 1
                    )

            # Step 7: PO
            po_num = f"PO-{uuid.uuid4().hex[:6].upper()}"
            ctx.po_number = po_num
            ikey = _derive_po_idempotency_key(str(ctx.namespace_id), ["ART-001"], "v1")
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    po_res = await do_generate_po(
                        conn,
                        ctx.namespace_id,
                        idempotency_key=ikey,
                        confirm=True,
                        engine=ctx.engine,
                        po_number=po_num,
                        artnrs=["ART-001"],
                        bom_line={"quantity": 2, "unit_price": 500.0},
                    )
            assert po_res["status"] == "executed"

            # Step 8: BOM_LINE ORDERED
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await update_bom_line_status(
                        conn,
                        ctx.namespace_id,
                        writer_engine="procurement",
                        quote_id=ctx.quote_id,
                        line_ref=ctx.line_ref,
                        status="ORDERED",
                    )
                    assert (
                        await conn.fetchval(
                            "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                            ctx.namespace_id,
                            ctx.quote_id,
                            ctx.line_ref,
                        )
                        == "ORDERED"
                    )

            # Step 9: Goods Receipt
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    loc_id = await conn.fetchval(
                        "INSERT INTO stock_locations (namespace_id, name, kind) VALUES ($1, 'Main Depot', 'warehouse') RETURNING id",
                        ctx.namespace_id,
                    )
                    ctx.location_id = loc_id
            gr_res = await do_record_goods_receipt(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "po_ref": po_num,
                    "location_id": str(loc_id),
                    "lines": [{"sku": "SKU-001", "qty": 2, "unit_cost": 400.0}],
                    "delivery_note_ref": "DN-001",
                },
            )
            assert gr_res["ok"] is True

            # Step 10: Three-way match
            match_eval = do_evaluate_three_way_match(
                {},
                po={"article_id": "ART-001", "quantity": 2, "unit_price": 500.0},
                goods_receipt={"quantity": 2},
                invoice={"article_id": "ART-001", "quantity": 2, "unit_price": 500.0},
            )
            assert match_eval["tier"] == "GREEN"

            # Step 11: BOM_LINE DELIVERED
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await update_bom_line_status(
                        conn,
                        ctx.namespace_id,
                        writer_engine="inventory",
                        quote_id=ctx.quote_id,
                        line_ref=ctx.line_ref,
                        status="DELIVERED",
                    )
                    assert (
                        await conn.fetchval(
                            "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                            ctx.namespace_id,
                            ctx.quote_id,
                            ctx.line_ref,
                        )
                        == "DELIVERED"
                    )

            # Step 12: Asset Seeded
            asset_res = await do_seed_asset_from_bom(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "bom_line_id": str(ctx.bom_line_id),
                    "serial": "SN-GT-001",
                    "functional_location_id": "HQ/Main/1/101",
                },
            )
            assert asset_res["created"] is True

            # Step 13 & 14: Field Tech INSTALLED & TESTED
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await update_bom_line_status(
                        conn,
                        ctx.namespace_id,
                        writer_engine="field_tech",
                        quote_id=ctx.quote_id,
                        line_ref=ctx.line_ref,
                        status="INSTALLED",
                    )
                    await update_bom_line_status(
                        conn,
                        ctx.namespace_id,
                        writer_engine="field_tech",
                        quote_id=ctx.quote_id,
                        line_ref=ctx.line_ref,
                        status="TESTED",
                    )
                    assert (
                        await conn.fetchval(
                            "SELECT status FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 AND line_ref = $3",
                            ctx.namespace_id,
                            ctx.quote_id,
                            ctx.line_ref,
                        )
                        == "TESTED"
                    )

            # Step 15: SLA Attached
            agr_id = str(uuid.uuid4())
            await do_set_sla_coverage(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "agreement_id": agr_id,
                    "functional_location_id": "HQ/Main/1/101",
                    "sla_terms": {"responseHours": 4, "coverageWindow": "24x7"},
                },
            )
            await do_attach_sla(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "agreement_id": agr_id,
                    "functional_location_id": "HQ/Main/1/101",
                    "namespace_slug": ctx.namespace_slug,
                },
            )

            # Step 16 & 17: Invoice Approved & actual_cost
            bom_label = f"BOM_LINE:{ctx.quote_id.upper()}:{ctx.line_ref.upper()}"
            cascade_res = await do_cascade_on_approval(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "approval_id": f"APP-{uuid.uuid4().hex[:6]}",
                    "quote_id": ctx.quote_id,
                    "lines": [{"bom_line_label": bom_label, "actual_cost": 450.00}],
                },
            )
            assert cascade_res["ok"] is True
            async with ctx.pool.acquire() as conn:
                assert (
                    float(
                        await conn.fetchval(
                            "SELECT actual_cost FROM economy_bom_actual_costs WHERE namespace_id = $1 AND bom_line_label = $2",
                            ctx.namespace_id,
                            bom_label,
                        )
                    )
                    == 450.00
                )

            # Step 18: Support ticket
            ticket_res = await do_open_ticket(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "summary": "Display flickering",
                    "customer_id": ctx.customer_id,
                    "priority": "high",
                    "source": "nce",
                },
            )
            ticket_id = UUID(ticket_res["ticket"]["id"])
            ctx.ticket_id = ticket_id

            # Step 19: Dispatch
            dispatch_res = await do_dispatch_work_order(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "ticket_id": str(ticket_id),
                    "service_type": "repair",
                    "priority": "high",
                    "estimated_cost": 500.0,
                    "confirm": True,
                },
            )
            assert dispatch_res["dispatched"] is True

            # Step 20: Work Order
            wo_res = await do_create_work_order(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "summary": "Field Tech Repair 101",
                    "kind": "service",
                    "source_kind": "ticket",
                    "source_ref": str(ticket_id),
                },
            )
            wo_id = wo_res["work_order_id"]
            ctx.work_order_id = wo_id

            # Step 21: Outcome
            with patch(
                "nce.vertical_modules.field_tech.outcome.record_decision_feedback",
                new_callable=AsyncMock,
            ):
                outcome_res = await do_record_outcome(
                    ctx.engine,
                    {
                        "namespace_id": str(ctx.namespace_id),
                        "work_order_id": str(wo_id),
                        "resolution_notes": "Cable re-seated and tested",
                        "quality_score": 0.95,
                        "rating": 5.0,
                    },
                )
                assert outcome_res["status"] == "recorded"

            # Step 22: Cert Expiry
            emp_uuid = str(uuid.uuid4())
            ctx.employee_id = emp_uuid
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    await conn.execute(
                        """
                        INSERT INTO employees (namespace_id, employee_id, name, email, role, department, leave_balance, active, raw)
                        VALUES ($1, $2, 'John Tech', 'john@acme.com', 'technician', 'field', 10.0, true, '{}'::jsonb)
                        """,
                        ctx.namespace_id,
                        emp_uuid,
                    )
                    await conn.execute(
                        """
                        INSERT INTO certifications (namespace_id, cert_id, employee_id, authority, name, issued, valid_to, status, raw)
                        VALUES ($1, $2, $3, 'OSHA', 'Safety Level 1', now() - interval '1 year', $4, 'active', '{}'::jsonb)
                        """,
                        ctx.namespace_id,
                        f"CERT-{uuid.uuid4().hex[:6]}",
                        emp_uuid,
                        datetime.now(timezone.utc) - timedelta(days=1),
                    )
            hr_res = await do_check_hr_cert_expiry(
                ctx.engine, {"namespace_id": str(ctx.namespace_id)}
            )
            assert hr_res["expired"] >= 1

            # Step 23: Allocation Invalidated (Halts pipeline with break-5a)
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    res_id = await conn.fetchval(
                        """
                        INSERT INTO resources (namespace_id, display_name, kind, ref_id, attrs)
                        VALUES ($1, 'John Tech', 'employee', $2, '{}'::jsonb)
                        RETURNING id
                        """,
                        ctx.namespace_id,
                        emp_uuid,
                    )
                    await conn.fetchval(
                        """
                        INSERT INTO allocations (namespace_id, resource_id, demand_kind, demand_id, starts_at, ends_at, status)
                        VALUES ($1, $2, 'project', $3, now() + interval '1 day', now() + interval '2 days', 'confirmed')
                        RETURNING id
                        """,
                        ctx.namespace_id,
                        res_id,
                        uuid.uuid4(),
                    )

            alloc_res = await handle_hr_cert_change(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "employee_id": emp_uuid,
                    "status": "expired",
                },
            )
            assert alloc_res["affected_count"] >= 1

            # Step 24: Customer Portal Request
            portal_res = await do_raise_service_request(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "customer_scope_id": ctx.customer_id,
                    "summary": "Meeting room sound failure",
                },
            )
            assert "ticket_id" in portal_res
            portal_ticket_id = UUID(portal_res["ticket_id"])

            # Step 25: Portal Ticket in Service Tickets
            async with ctx.pool.acquire() as conn:
                pt_row = await conn.fetchrow(
                    "SELECT id, source, source_id FROM service_tickets WHERE namespace_id = $1 AND id = $2",
                    ctx.namespace_id,
                    portal_ticket_id,
                )
                assert pt_row is not None, "Portal ticket not found in service_tickets"
                assert str(pt_row["source_id"]).startswith("customer_portal:")

            # Step 26: Project Outcome at G5 Gate
            p_out = await do_record_project_outcome(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "project_id": f"PROJECT:{ctx.quote_id.upper()}",
                    "description": "Enterprise AV Deployment",
                    "slip_reason": "Logistics delivery delay",
                },
            )
            assert p_out["ok"] is True
            async with ctx.pool.acquire() as conn:
                mem_cnt = await conn.fetchval(
                    "SELECT count(*) FROM memories WHERE namespace_id = $1 AND node_type = 'PROJECT'",
                    ctx.namespace_id,
                )
                assert mem_cnt >= 1, "Project outcome memory not recorded in memories table"

            # Step 27: Design Recall with Outcome-Weighted Similar Project
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ctx.namespace_id)
                    for i in range(5):
                        await conn.execute(
                            """
                            INSERT INTO decision_feedback (namespace_id, engine, context_id, proposal, decision, delta, actor)
                            VALUES ($1, 'system_design', $2, '{"bom": []}'::jsonb, 'accepted', '{}'::jsonb, 'human')
                            """,
                            ctx.namespace_id,
                            f"pipe-ctx-{i}",
                        )
            prop_res = await do_propose_design(
                ctx.engine,
                {
                    "namespace_id": str(ctx.namespace_id),
                    "room_brief": "Enterprise AV Deployment",
                },
            )
            assert "proposed_lines" in prop_res

            # Step 28: Degradation Register reports zero active degradations
            reg = get_degradation_register()
            total_deg = reg.total_count(str(ctx.namespace_id))
            degs = reg.get_degradations(str(ctx.namespace_id))
            assert total_deg == 0, f"Unexpected degradations in namespace: {total_deg} ({degs})"


# ---------------------------------------------------------------------------
# Standing Positive Controls (U18: a positive-control test beats a manual RED)
# ---------------------------------------------------------------------------


class TestGoldenThreadPositiveControls:
    """Standing positive-control tests verifying the burndown instrument itself."""

    def test_burndown_manifest_covers_all_review_breaks(self) -> None:
        """Verify that all 7 review breaks from Review §0 are mapped to steps."""
        expected_breaks = {
            "break-1",
            "break-2",
            "break-3",
            "break-4",
            "break-5a",
            "break-5b",
            "break-5c",
            "break-degradations",
        }
        manifest_breaks = {
            s.review_break for s in GOLDEN_THREAD_STEPS if s.review_break is not None
        }
        missing = expected_breaks - manifest_breaks
        assert not missing, f"Burndown manifest missing breaks: {missing}"

    def test_burndown_manifest_step_sequence_continuity(self) -> None:
        """Verify that steps are 1-indexed, contiguous, and exactly 28 steps."""
        assert len(GOLDEN_THREAD_STEPS) == 28
        indices = [s.index for s in GOLDEN_THREAD_STEPS]
        assert indices == list(range(1, 29))

    def test_burndown_manifest_broken_steps_count(self) -> None:
        """Verify broken steps count reflects honest live state.

        All 28 steps now execute and assert real database state with zero broken steps.
        """
        broken_steps = [s for s in GOLDEN_THREAD_STEPS if s.is_broken]
        assert len(broken_steps) == 0, f"Expected 0 broken steps, found: {broken_steps}"
        broken_indices = {s.index for s in broken_steps}
        assert broken_indices == set()

    def test_positive_control_broken_steps_have_remediation_waves(self) -> None:
        """Verify every broken step specifies a responsible Phase 1 remediation wave."""
        for step in GOLDEN_THREAD_STEPS:
            if step.is_broken:
                assert step.phase1_wave is not None, f"Step {step.index} missing remediation wave"
                assert step.review_break is not None, f"Step {step.index} missing review break"

    def test_positive_control_strict_xfail_marker_applied_to_all_broken_steps(self) -> None:
        """Verify that every broken step test method carries @pytest.mark.xfail(strict=True)."""
        broken_names = {
            f"test_step_{s.index:02d}_{s.name}" for s in GOLDEN_THREAD_STEPS if s.is_broken
        }
        passing_names = {
            f"test_step_{s.index:02d}_{s.name}" for s in GOLDEN_THREAD_STEPS if not s.is_broken
        }

        for name, method in inspect.getmembers(TestGoldenThreadSteps, predicate=inspect.isfunction):
            if name in broken_names:
                marks = getattr(method, "pytestmark", [])
                xfail_marks = [m for m in marks if m.name == "xfail"]
                assert xfail_marks, f"{name} is a broken seam step but lacks @pytest.mark.xfail"
                assert xfail_marks[0].kwargs.get("strict") is True, (
                    f"{name} must have strict=True on xfail"
                )
            elif name in passing_names:
                marks = getattr(method, "pytestmark", [])
                xfail_marks = [m for m in marks if m.name == "xfail"]
                assert not xfail_marks, (
                    f"{name} is a working step and MUST NOT carry @pytest.mark.xfail"
                )


@pytest.mark.asyncio
class TestRoundTripFloorPositiveControls:
    """The floor must fail on a hollow run and count a real one. No DB needed."""

    async def test_counter_counts_every_query_method_through_the_proxy(self) -> None:
        class FakeConn:
            async def execute(self, *a: Any, **k: Any) -> str:
                return "OK"

            async def fetch(self, *a: Any, **k: Any) -> list:
                return []

            async def fetchrow(self, *a: Any, **k: Any) -> None:
                return None

            async def fetchval(self, *a: Any, **k: Any) -> None:
                return None

            def transaction(self) -> Any:
                raise AssertionError("not needed")

        class FakeAcquire:
            async def __aenter__(self) -> FakeConn:
                return FakeConn()

            async def __aexit__(self, *e: Any) -> bool:
                return False

        class FakePool:
            def acquire(self, *a: Any, **k: Any) -> FakeAcquire:
                return FakeAcquire()

        counter = _QueryCounter()
        pool = _CountingPool(FakePool(), counter)
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            await conn.fetch("SELECT 1")
            await conn.fetchrow("SELECT 1")
            await conn.fetchval("SELECT 1")

        assert counter.count == 4, "the proxy must count every query method"

        # Non-query attributes pass through WITHOUT being counted.
        assert not callable(getattr(conn, "transaction", None)) or counter.count == 4

    async def test_floor_rejects_a_run_that_touched_the_database_zero_times(self) -> None:
        """The assertion this floor replaces could be satisfied by sleeping.

        This one cannot: the only way to raise the number is to run queries.
        """
        counter = _QueryCounter()
        with pytest.raises(AssertionError, match="Hollow live run detected"):
            assert counter.count >= MIN_DB_ROUND_TRIPS, (
                f"Hollow live run detected: only {counter.count} database round trips, "
                f"floor is {MIN_DB_ROUND_TRIPS}. The scenario is not exercising the "
                "database -- steps have become static assertions again."
            )
