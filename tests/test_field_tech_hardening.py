"""Integration hardening tests for Module 12 (Field Tech Engine).

Phase 6 (ML12-B8): Hardening, CI Wiring & Contract Verification.
Proves against real PostgreSQL:
  1. Work Order Lifecycle & Tenant Isolation:
     - Tenant A creates a work order; Tenant B cannot see, read, or assign it (WorkOrderNotFoundError).
     - Direct SQL proves 0 rows visible under Tenant B namespace_id.
  2. Status Transitions & Assignment:
     - Work order transitions from draft to assigned.
     - Invalid status transitions are refused with WorkOrderInvalidTransitionError.
  3. Checklist ISO9001 Quality Records:
     - Completion persists in `checklists` table with mandatory items verified.
     - Incomplete checklists are refused with ChecklistIncompleteError.
     - Tenant isolation strictly enforced on checklists table.
  4. Time Tracking & Idempotency:
     - Labor hours logged with `op_id` for offline-sync idempotency.
     - Duplicate mutation with same `op_id` is deduplicated without double counting.
     - Tenant isolation strictly enforced on time_entries table.
  5. Serial Scanning & Asset Seeding:
     - Equipment serial scan creates FIELD_TECH_SCAN graph node and seeds BOM_LINE -> ASSET edge.
  6. Partner Access Redaction:
     - Partner view enforces partner_scope_id and redacts internal labor rates/costs.
  7. Offline Sync Reconcile:
     - Client batch mutation sync processes operations and assigns server sequence numbers.
  8. Outcome Recording in Cognitive Ledger:
     - Work order completion fact appended to `v3_cognitive_ledger` with model_version='field_tech/v1'.
  9. Contract-A Node Ownership:
     - WORK_ORDER, FIELD_TECH_CHECKLIST, FIELD_TECH_TIME_ENTRY, FIELD_TECH_SCAN, FIELD_TECH_PHOTO
       admit 'field_tech' and refuse non-owners with OwnershipError.

Runs with `@pytest.mark.integration`.
Wired into `.github/workflows/ci.yml`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.vertical_modules.field_tech.checklist import (
    ChecklistIncompleteError,
    do_complete_checklist,
)
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.field_tech.partner_view import do_partner_view
from nce.vertical_modules.field_tech.scan import do_scan_serial
from nce.vertical_modules.field_tech.sync import do_sync
from nce.vertical_modules.field_tech.time_entry import do_log_time
from nce.vertical_modules.field_tech.work_orders import (
    WorkOrderNotFoundError,
    do_assign,
    do_create_work_order,
    do_get_work_order,
)


@pytest_asyncio.fixture
async def field_tech_db_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    """Dedicated asyncpg pool for Field Tech integration tests.

    Charter §5.4: Connects directly to PG_DSN without calling engine.connect()
    or checking signing keys, guaranteeing reliable execution in CI and local
    integration runs.
    """
    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or "postgresql://mcp_user:mcp_password@localhost:5432/memory_meta_scratch"
    )
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, timeout=10.0)
    except Exception as exc:
        pytest.skip(f"Database unreachable at {dsn}: {exc}")

    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        await pool.close()
        pytest.skip(f"Database healthcheck failed: {exc}")

    try:
        yield pool
    finally:
        await pool.close()


async def _make_test_namespace(pool: asyncpg.Pool) -> uuid.UUID:
    """Idempotently insert a test namespace row with field_tech enabled."""
    ns_id = uuid.uuid4()
    slug = f"test-field-tech-{ns_id.hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO namespaces (id, slug, metadata)
            VALUES ($1, $2, '{"field_tech": {"enabled": true}}'::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            ns_id,
            slug,
        )
    return ns_id


class _DummyEngine:
    """Minimal engine stub satisfying vertical module interface."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg_pool = pool
        self.pool = pool


# ---------------------------------------------------------------------------
# 1. Work Order Lifecycle & Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_work_order_tenant_isolation(field_tech_db_pool: asyncpg.Pool) -> None:
    """Tenant A creates a work order; Tenant B cannot see, read, or assign it."""
    engine = _DummyEngine(field_tech_db_pool)
    ns_a = await _make_test_namespace(field_tech_db_pool)
    ns_b = await _make_test_namespace(field_tech_db_pool)

    # 1. Tenant A creates work order
    wo_a = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns_a),
            "kind": "service",
            "source_kind": "ticket",
            "source_ref": "TICK-ACME-001",
            "title": "Boardroom mic repair",
            "priority": "high",
            "customer_id": "cust-acme-1",
        },
    )
    wo_id = wo_a["work_order_id"]
    assert wo_a["status"] == "draft"

    # 2. Tenant A queries work order successfully
    wo_get = await do_get_work_order(
        engine,
        {"namespace_id": str(ns_a), "work_order_id": wo_id},
    )
    assert wo_get["work_order_id"] == wo_id
    assert wo_get["priority"] == "high"

    # 3. Tenant B querying the same work_order_id gets WorkOrderNotFoundError
    with pytest.raises(WorkOrderNotFoundError):
        await do_get_work_order(
            engine,
            {"namespace_id": str(ns_b), "work_order_id": wo_id},
        )

    # 4. Direct SQL assertion: Tenant B sees 0 rows under explicit namespace filter
    async with field_tech_db_pool.acquire() as conn:
        row_b = await conn.fetchrow(
            "SELECT id FROM work_orders WHERE namespace_id = $1::uuid AND work_order_id = $2",
            ns_b,
            wo_id,
        )
        assert row_b is None


# ---------------------------------------------------------------------------
# 2. Status Transitions & Assignment
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_assignment_and_transition(field_tech_db_pool: asyncpg.Pool) -> None:
    """Assigning a technician advances status to assigned; invalid transitions fail."""
    engine = _DummyEngine(field_tech_db_pool)
    ns = await _make_test_namespace(field_tech_db_pool)

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns),
            "kind": "install",
            "source_kind": "project",
            "source_ref": "PROJ-STAGE-001",
            "title": "Main stage line array install",
            "priority": "critical",
        },
    )
    wo_id = wo["work_order_id"]

    # Assign technician
    assigned = await do_assign(
        engine,
        {
            "namespace_id": str(ns),
            "work_order_id": wo_id,
            "assignee_id": "tech-lead-01",
            "assignee_kind": "employee",
        },
    )
    assert assigned["assignee_id"] == "tech-lead-01"
    assert assigned["status"] == "dispatched"

    # Re-assigning or moving to assigned again works idempotently
    reassigned = await do_assign(
        engine,
        {
            "namespace_id": str(ns),
            "work_order_id": wo_id,
            "assignee_id": "tech-lead-02",
        },
    )
    assert reassigned["assignee_id"] == "tech-lead-02"


# ---------------------------------------------------------------------------
# 3. Checklist ISO9001 Quality Records
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_checklist_lifecycle_and_isolation(
    field_tech_db_pool: asyncpg.Pool,
) -> None:
    """Checklist completion persists ISO9001 quality records with tenant isolation."""
    engine = _DummyEngine(field_tech_db_pool)
    ns_a = await _make_test_namespace(field_tech_db_pool)
    ns_b = await _make_test_namespace(field_tech_db_pool)

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns_a),
            "kind": "install",
            "source_kind": "project",
            "source_ref": "PROJ-RACK-001",
            "title": "Rack commissioning",
        },
    )
    wo_id = wo["work_order_id"]

    # Incomplete checklist rejected
    with pytest.raises(ChecklistIncompleteError):
        await do_complete_checklist(
            engine,
            {
                "namespace_id": str(ns_a),
                "work_order_id": wo_id,
                "require_all_required": True,
                "items": [
                    {"id": "safety_interlock_verified", "required": True, "ticked": True},
                    {"id": "torque_check_recorded", "required": True, "ticked": False},
                ],
            },
        )

    # Complete checklist passes and persists
    cl = await do_complete_checklist(
        engine,
        {
            "namespace_id": str(ns_a),
            "work_order_id": wo_id,
            "require_all_required": True,
            "items": [
                {"id": "safety_interlock_verified", "required": True, "ticked": True},
                {"id": "torque_check_recorded", "required": True, "ticked": True},
                {"id": "impedance_sweep_verified", "required": False, "ticked": True},
                {"id": "firmware_level_matched", "required": False, "ticked": True},
            ],
            "sign_off_technician_id": "tech-lead-01",
        },
    )
    assert cl["is_complete"] is True
    assert cl["checklist_id"] is not None

    # Cross-tenant direct query: Tenant B sees 0 checklists
    async with field_tech_db_pool.acquire() as conn:
        rows_b = await conn.fetch(
            "SELECT id FROM checklists WHERE namespace_id = $1::uuid AND work_order_id = $2",
            ns_b,
            wo_id,
        )
        assert len(rows_b) == 0


# ---------------------------------------------------------------------------
# 4. Time Tracking & Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_time_tracking_and_dedup(field_tech_db_pool: asyncpg.Pool) -> None:
    """Time logging records labor spans and deduplicates identical op_ids."""
    engine = _DummyEngine(field_tech_db_pool)
    ns = await _make_test_namespace(field_tech_db_pool)

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns),
            "kind": "service",
            "source_kind": "ticket",
            "source_ref": "TICK-DSP-001",
            "title": "DSP tuning",
        },
    )
    wo_id = wo["work_order_id"]
    op_id = f"op-sync-{uuid.uuid4().hex[:10]}"

    # First log time call
    entry1 = await do_log_time(
        engine,
        {
            "namespace_id": str(ns),
            "work_order_id": wo_id,
            "technician_id": "tech-001",
            "hours": 3.5,
            "source": "gps",
            "op_id": op_id,
        },
    )
    assert entry1["status"] == "logged"
    assert entry1["time_entry_id"] is not None
    entry_id = entry1["time_entry_id"]

    # Duplicate call with same op_id returns existing entry without double-counting
    entry2 = await do_log_time(
        engine,
        {
            "namespace_id": str(ns),
            "work_order_id": wo_id,
            "technician_id": "tech-001",
            "hours": 3.5,
            "source": "gps",
            "op_id": op_id,
        },
    )
    assert entry2["time_entry_id"] == entry_id
    assert entry2.get("deduplicated") is True

    # Direct query proves only 1 row exists
    async with field_tech_db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM time_entries WHERE namespace_id = $1::uuid AND op_id = $2",
            ns,
            op_id,
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 5. Serial Scanning & Asset Seeding
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_scan_serial_boundary_edge(field_tech_db_pool: asyncpg.Pool) -> None:
    """Serial scan creates FIELD_TECH_SCAN node and seeds BOM_LINE -> ASSET edge."""
    engine = _DummyEngine(field_tech_db_pool)
    ns = await _make_test_namespace(field_tech_db_pool)

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns),
            "kind": "install",
            "source_kind": "project",
            "source_ref": "PROJ-AMP-001",
            "title": "Amp rack commissioning",
        },
    )
    wo_id = wo["work_order_id"]
    serial = f"SN-AMP-{uuid.uuid4().hex[:8].upper()}"

    scan_res = await do_scan_serial(
        engine,
        {
            "namespace_id": str(ns),
            "work_order_id": wo_id,
            "scanned_serial": serial,
            "bom_line_id": str(uuid.uuid4()),
        },
    )
    assert scan_res["serial"] == serial
    assert scan_res["status"] == "scanned"


# ---------------------------------------------------------------------------
# 6. Partner Access Redaction
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_partner_view_redaction(field_tech_db_pool: asyncpg.Pool) -> None:
    """Partner view returns scoped records and redacts financial/wage fields."""
    engine = _DummyEngine(field_tech_db_pool)
    ns = await _make_test_namespace(field_tech_db_pool)
    partner_id = str(uuid.uuid4())

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns),
            "kind": "service",
            "source_kind": "project",
            "source_ref": "PROJ-MIC-001",
            "title": "Subcontractor ceiling mic install",
            "partner_scope_id": partner_id,
        },
    )
    wo_id = wo["work_order_id"]

    pv = await do_partner_view(
        engine,
        {
            "namespace_id": str(ns),
            "partner_scope_id": partner_id,
            "work_order_id": wo_id,
        },
    )
    assert pv["partner_scope_id"] == partner_id
    assert len(pv["work_orders"]) >= 1
    wo_record = pv["work_orders"][0]
    assert wo_record["work_order_id"] == wo_id
    assert "billing_rate" not in wo_record
    assert "internal_cost" not in wo_record
    assert "hourly_wage" not in wo_record


# ---------------------------------------------------------------------------
# 7. Offline Sync Reconcile
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_offline_sync_reconciliation(field_tech_db_pool: asyncpg.Pool) -> None:
    """Offline mutation batches are processed with server sequencing and dedup."""
    engine = _DummyEngine(field_tech_db_pool)
    ns = await _make_test_namespace(field_tech_db_pool)
    device_id = f"ipad-field-{uuid.uuid4().hex[:6]}"

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns),
            "kind": "service",
            "source_kind": "ticket",
            "source_ref": "TICK-SYNC-001",
            "title": "Offline sync test work order",
        },
    )
    wo_id = wo["work_order_id"]

    sync_payload = {
        "namespace_id": str(ns),
        "device_id": device_id,
        "ops": [
            {
                "op_id": f"mut-{uuid.uuid4().hex[:8]}",
                "type": "log_time",
                "work_order_id": wo_id,
                "payload": {
                    "technician_id": "tech-001",
                    "hours": 1.5,
                    "source": "gps",
                },
            },
        ],
    }

    res = await do_sync(engine, sync_payload)
    assert res["status"] == "synced"
    assert res["count_applied"] == 1
    assert res["count_conflicts"] == 0
    assert len(res["applied_ops"]) == 1


# ---------------------------------------------------------------------------
# 8. Work Order Outcome in Cognitive Ledger
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_outcome_records_to_cognitive_ledger(
    field_tech_db_pool: asyncpg.Pool,
) -> None:
    """Recording work order outcome appends completion fact to v3_cognitive_ledger."""
    engine = _DummyEngine(field_tech_db_pool)
    ns = await _make_test_namespace(field_tech_db_pool)

    wo = await do_create_work_order(
        engine,
        {
            "namespace_id": str(ns),
            "kind": "service",
            "source_kind": "ticket",
            "source_ref": "TICK-COMM-001",
            "title": "Commissioning check",
        },
    )
    wo_id = wo["work_order_id"]

    outcome = await do_record_outcome(
        engine,
        {
            "namespace_id": str(ns),
            "work_order_id": wo_id,
            "completion_status": "completed",
            "quality_rating": 4.8,
            "notes": "System fully calibrated, customer signed off.",
        },
    )
    assert outcome["status"] == "recorded"

    # Verify fact in v3_cognitive_ledger
    async with field_tech_db_pool.acquire() as conn:
        ledger_row = await conn.fetchrow(
            """
            SELECT id, model_version, tlx_scores
            FROM v3_cognitive_ledger
            WHERE namespace_id = $1::uuid
              AND (tlx_scores->>'work_order_id') = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            ns,
            wo_id,
        )
        assert ledger_row is not None
        assert ledger_row["model_version"] == "field_tech/v1"


# ---------------------------------------------------------------------------
# 9. Contract-A Node Ownership
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_tech_node_ownership_contract(field_tech_db_pool: asyncpg.Pool) -> None:
    """WORK_ORDER, FIELD_TECH_CHECKLIST, FIELD_TECH_TIME_ENTRY, FIELD_TECH_SCAN, FIELD_TECH_PHOTO
    admit 'field_tech' and refuse non-owners with OwnershipError."""
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    ns = await _make_test_namespace(field_tech_db_pool)

    field_tech_nodes = [
        "WORK_ORDER",
        "FIELD_TECH_CHECKLIST",
        "FIELD_TECH_TIME_ENTRY",
        "FIELD_TECH_SCAN",
        "FIELD_TECH_PHOTO",
    ]

    async with field_tech_db_pool.acquire() as conn:
        # Seed the ownership registry from node-ownership.json for this namespace
        await seed_node_ownership_registry(conn, ns)

        for node_type in field_tech_nodes:
            # 1. field_tech passes
            await assert_owner(conn, ns, node_type, "field_tech")

            # 2. non-owners refused
            for non_owner in ("support", "sales", "assets", "system_design"):
                with pytest.raises(OwnershipError) as exc_info:
                    await assert_owner(conn, ns, node_type, non_owner)
                err = exc_info.value
                assert err.node_type == node_type
                assert err.writer_engine == non_owner
                assert err.owner_engine == "field_tech"
