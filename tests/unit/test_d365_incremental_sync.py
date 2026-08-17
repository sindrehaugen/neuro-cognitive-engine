"""Unit tests for the D365 vertical incremental-sync watermark.

Covers `NCE_D365_INCREMENTAL_ENABLED`: when on, `run_full_sync` pulls only
Dataverse records modified since `d365_integrations.last_sync_at` (opt-in,
default off). See `nce/vertical_modules/dynamics365/sync.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from nce.config import cfg
from nce.vertical_modules.dynamics365.sync import DataverseSyncEngine


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def paginate(self, entity_set, *, select=None, filter_expr=None, page_size=1000):
        self.calls.append(
            {
                "entity_set": entity_set,
                "select": select,
                "filter_expr": filter_expr,
                "page_size": page_size,
            }
        )

        async def _empty():
            return
            yield  # pragma: no cover - makes this an async generator

        return _empty()


class _FakeConn:
    def __init__(self, row=None) -> None:
        self._row = row
        self.executes: list[str] = []

    async def fetchrow(self, *_args, **_kwargs):
        return self._row

    async def execute(self, sql, *_args, **_kwargs):
        self.executes.append(sql)


def _engine(conn=None, client=None) -> DataverseSyncEngine:
    return DataverseSyncEngine(conn or _FakeConn(), uuid.uuid4(), client or _FakeClient())


def test_apply_incremental_no_watermark_is_noop():
    eng = _engine()
    assert eng._apply_incremental(None) is None
    assert eng._apply_incremental("statecode eq 0") == "statecode eq 0"


def test_apply_incremental_appends_modifiedon_clause():
    eng = _engine()
    eng._since = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert eng._apply_incremental(None) == "modifiedon gt 2026-06-01T12:00:00Z"
    assert (
        eng._apply_incremental("statecode eq 0")
        == "statecode eq 0 and modifiedon gt 2026-06-01T12:00:00Z"
    )


def test_apply_incremental_normalizes_offset_to_utc_z():
    eng = _engine()
    eng._since = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert eng._apply_incremental(None) == "modifiedon gt 2026-06-01T12:00:00Z"


@pytest.mark.asyncio
async def test_load_watermark_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_INCREMENTAL_ENABLED", False)
    eng = _engine(conn=_FakeConn(row={"last_sync_at": datetime.now(timezone.utc)}))
    assert await eng._load_incremental_watermark() is None


@pytest.mark.asyncio
async def test_load_watermark_enabled_reads_last_sync_at(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_INCREMENTAL_ENABLED", True)
    ts = datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)
    eng = _engine(conn=_FakeConn(row={"last_sync_at": ts}))
    assert await eng._load_incremental_watermark() == ts


@pytest.mark.asyncio
async def test_load_watermark_enabled_no_row_returns_none(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_INCREMENTAL_ENABLED", True)
    eng = _engine(conn=_FakeConn(row=None))
    assert await eng._load_incremental_watermark() is None


@pytest.mark.asyncio
async def test_paginate_injects_watermark_into_client_call():
    client = _FakeClient()
    eng = _engine(client=client)
    eng._since = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    async for _ in eng._paginate(
        "incidents", select=["x"], filter_expr="statecode eq 0", page_size=50
    ):
        pass
    call = client.calls[0]
    assert call["entity_set"] == "incidents"
    assert call["page_size"] == 50
    assert call["filter_expr"] == "statecode eq 0 and modifiedon gt 2026-06-01T00:00:00Z"


@pytest.mark.asyncio
async def test_paginate_no_watermark_passes_base_filter_unchanged():
    client = _FakeClient()
    eng = _engine(client=client)  # _since stays None
    async for _ in eng._paginate("accounts", select=["accountid"]):
        pass
    assert client.calls[0]["filter_expr"] is None


@pytest.mark.asyncio
async def test_run_full_sync_records_audit_row_per_entity(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_INCREMENTAL_ENABLED", False)
    conn = _FakeConn(row=None)
    eng = _engine(conn=conn, client=_FakeClient())  # empty client → no upserts
    out = await eng.run_full_sync(entity_types=["accounts", "incidents"])
    assert "run_id" in out
    inserts = [s for s in conn.executes if "INSERT INTO d365_sync_runs" in s]
    assert len(inserts) == 2  # one audit row per requested entity


@pytest.mark.asyncio
async def test_run_full_sync_records_error_row_then_reraises(monkeypatch):
    monkeypatch.setattr(cfg, "NCE_D365_INCREMENTAL_ENABLED", False)
    conn = _FakeConn(row=None)
    eng = _engine(conn=conn, client=_FakeClient())

    async def _boom():
        raise RuntimeError("dataverse down")

    eng.sync_accounts = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="dataverse down"):
        await eng.run_full_sync(entity_types=["accounts"])
    inserts = [s for s in conn.executes if "INSERT INTO d365_sync_runs" in s]
    assert len(inserts) == 1  # error row recorded before the re-raise
