"""
tests/integration/test_jsonb_string_decode_live.py
======================================================
Live-Postgres regression tests for three call sites that never decoded a
jsonb column's raw string value, found by Lane E sweeping the estate for the
same shape as nce/vertical_modules/documents/service.py's fixed bug
(tests/integration/test_documents_jsonb_metadata_live.py).

THE SHARED ROOT CAUSE, EXACTLY
----------------------------------
This pool registers no jsonb codec, so every jsonb column round-trips
through asyncpg as raw JSON TEXT, never an automatically-decoded Python
dict. Each of the three call sites below assumed a dict and broke in a
different, symptom-specific way:

1. nce/orchestrators/temporal.py's create_snapshot/list_snapshots pass the
   raw string straight into Pydantic's SnapshotRecord(metadata: dict[str,
   Any]) -- Pydantic does not parse a JSON string for a dict field, so this
   is a ValidationError, not a Python TypeError/ValueError like the other
   two.
2. nce/vertical_modules/economy/contracts.py's do_validate_contract fallback
   path (reached only when a contract_id is absent from economy_contracts
   but present in agreements) does `(agr_row["metadata"] or {}).get(...)` --
   agreements.metadata is JSONB NOT NULL DEFAULT '{}'::jsonb, so the value
   is always a non-empty, truthy string; `or {}` never fires, and `.get()`
   on a str raises AttributeError.
3. nce/replay.py's get_run_status does `dict(row["config_overrides"]) if
   row["config_overrides"] else None` -- identical shape to the original
   documents bug: dict(a_json_string) doesn't parse it, it raises
   ValueError trying to treat the string as an iterable of key-value pairs.

NOT registering a global jsonb codec instead of three local fixes: it would
change the return type of every jsonb column asyncpg touches on this pool,
silently breaking the ~42 call sites elsewhere in this codebase that
already do their own str-or-dict handling by hand (e.g.
nce/resource_surface/comments.py, documents/service.py's own
_parse_metadata) -- their json.loads would receive an already-decoded dict
and raise. Named and rejected explicitly, not just avoided by omission.

Each test below is mutation-verified: reverting the corresponding source
fix reproduces the exact traceback quoted in that test's own docstring.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from nce.engine_registry import populate_engine_modules
from nce.models import CreateSnapshotRequest
from nce.orchestrator import NCEEngine
from nce.orchestrators.temporal import TemporalOrchestrator
from nce.replay import get_run_status
from nce.vertical_modules.economy.contracts import do_validate_contract

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_create_snapshot_metadata_round_trips_a_real_dict(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Before this fix: pydantic_core._pydantic_core.ValidationError: 1
    validation error for SnapshotRecord / metadata / Input should be a
    valid dictionary [type=dict_type, input_value='{"k": "v"}', ...] --
    reproduced live, `SnapshotRecord(**row)` on the raw string.
    """
    orch = TemporalOrchestrator(pg_pool, mongo_client=None, semantic_search_fn=None)
    payload = CreateSnapshotRequest(
        namespace_id=namespace_id,
        name=f"jsonb-probe-{uuid.uuid4().hex[:8]}",
        metadata={"reason": "regression-test", "n": 1},
    )
    created = await orch.create_snapshot(payload)
    assert created.metadata == {"reason": "regression-test", "n": 1}, (
        f"metadata did not round-trip as a real dict: {created.metadata!r}"
    )


@pytest.mark.asyncio
async def test_list_snapshots_metadata_round_trips_a_real_dict(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Same defect, the list path -- a separate call site
    (`[SnapshotRecord(**r) for r in rows]`), same ValidationError before
    this fix.
    """
    orch = TemporalOrchestrator(pg_pool, mongo_client=None, semantic_search_fn=None)
    await orch.create_snapshot(
        CreateSnapshotRequest(
            namespace_id=namespace_id,
            name=f"jsonb-probe-list-{uuid.uuid4().hex[:8]}",
            metadata={"k": "v"},
        )
    )
    listed = await orch.list_snapshots(str(namespace_id))
    matching = [s for s in listed if s.metadata == {"k": "v"}]
    assert matching, f"expected a snapshot with metadata={{'k': 'v'}}, got {listed!r}"


@pytest.mark.asyncio
async def test_validate_contract_fallback_reads_real_cpi_cap_from_agreements(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Before this fix: AttributeError: 'str' object has no attribute 'get'
    -- reproduced live. Reaches the fallback path deliberately: the
    agreement is never inserted into economy_contracts, only agreements,
    so do_validate_contract's primary lookup misses and falls through to
    `(agr_row["metadata"] or {}).get("cpi_cap", ...)`.
    """
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)

    async with pg_pool.acquire() as conn:
        agreement_id = await conn.fetchval(
            "INSERT INTO agreements (namespace_id, title, annual_value, metadata) "
            "VALUES ($1, $2, 120000.00, $3) RETURNING id",
            namespace_id,
            f"jsonb-probe-agreement-{uuid.uuid4().hex[:8]}",
            '{"cpi_cap": 0.03}',
        )

    result = await do_validate_contract(
        eng,
        {
            "namespace_id": str(namespace_id),
            "contract_id": str(agreement_id),
            "proposed_cpi_pct": Decimal("0.02"),
        },
    )
    assert result["ok"] is True
    assert result["cpi_cap"] == Decimal("0.0300"), (
        f"cpi_cap was not read from the agreement's real metadata: {result!r}"
    )


@pytest.mark.asyncio
async def test_get_run_status_config_overrides_round_trips_a_real_dict(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """Before this fix: ValueError: dictionary update sequence element #0
    has length 1; 2 is required -- reproduced live, identical shape to the
    original documents.metadata bug this sweep started from.
    """
    async with pg_pool.acquire() as conn:
        run_id = await conn.fetchval(
            "INSERT INTO replay_runs "
            "(source_namespace_id, mode, start_seq, status, config_overrides) "
            "VALUES ($1, 'observational', 1, 'running', $2) RETURNING id",
            namespace_id,
            '{"dry_run": true}',
        )

    status = await get_run_status(pg_pool, run_id)
    assert status["config_overrides"] == {"dry_run": True}, (
        f"config_overrides did not round-trip as a real dict: {status!r}"
    )
