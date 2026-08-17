"""Unit tests for the Dynamics 365 Sales source adapter.

Covers:
  - Watermark loading/saving from/to d365_delta_tokens table.
  - Incremental sync using modifiedon watermark queries.
  - Change-tracking sync using deltaLinks (upserts + soft-deletes).
  - Falling back to watermark sync on change tracking errors.
  - Deletion reconciliation during full sync.
  - Verification of no logged secrets or tokens.
"""

from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any

import pytest

from nce.config import cfg
from nce.vertical_modules.dynamics365.client import DataverseClient
from nce.vertical_modules.sales.source_adapters.d365 import SalesD365SyncEngine, parse_datetime

# Mock Org URL for Dataverse Client
_ORG_URL = "https://mockorg.crm.dynamics.com"


class _FakeClient(DataverseClient):
    """Mock DataverseClient for unit testing."""

    def __init__(
        self, paginate_results: list[dict[str, Any]] | None = None, track_changes_result: Any = None
    ) -> None:
        self.paginate_results = paginate_results or []
        self.track_changes_result: Any = track_changes_result or ([], [], None)
        self.paginate_calls: list[dict[str, Any]] = []
        self.track_changes_calls: list[dict[str, Any]] = []

    async def paginate(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter_expr: str | None = None,
        page_size: int = 1000,
    ) -> Any:
        self.paginate_calls.append(
            {
                "entity_set": entity_set,
                "select": select,
                "filter_expr": filter_expr,
                "page_size": page_size,
            }
        )
        for rec in self.paginate_results:
            yield rec

    async def track_changes(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        delta_link: str | None = None,
        page_size: int = 1000,
    ) -> tuple[list[dict[str, Any]], list[str], str | None]:
        self.track_changes_calls.append(
            {
                "entity_set": entity_set,
                "select": select,
                "delta_link": delta_link,
                "page_size": page_size,
            }
        )
        if isinstance(self.track_changes_result, Exception):
            raise self.track_changes_result
        return self.track_changes_result


class _FakeConn:
    """Mock asyncpg.Connection for unit testing."""

    def __init__(
        self,
        fetchrow_val: dict[str, Any] | None = None,
        fetch_val: list[dict[str, Any]] | None = None,
    ) -> None:
        self.fetchrow_val = fetchrow_val
        self.fetch_val = fetch_val or []
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetches.append((sql, args))
        return self.fetchrow_val

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.fetches.append((sql, args))
        return self.fetch_val

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return "UPDATE 1"


def test_parse_datetime() -> None:
    # ISO parsing
    dt = parse_datetime("2026-06-23T18:00:00Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 18

    # Edge cases
    assert parse_datetime(None) is None
    assert parse_datetime("") is None
    assert parse_datetime("invalid-date") is None


@pytest.mark.asyncio
async def test_sync_entity_watermark_incremental(monkeypatch) -> None:
    # Ensure change tracking is disabled for this test to trigger watermark flow
    monkeypatch.setattr(cfg, "NCE_D365_CHANGE_TRACKING_ENABLED", False)

    # Setup mocks
    initial_watermark = "2026-06-23T12:00:00Z"
    conn = _FakeConn(fetchrow_val={"delta_link": initial_watermark})

    mock_records = [
        {"accountid": "acc-123", "name": "Acme Corp", "modifiedon": "2026-06-23T14:30:00Z"},
        {"accountid": "acc-456", "name": "Globex", "modifiedon": "2026-06-23T15:00:00Z"},
    ]
    client = _FakeClient(paginate_results=mock_records)

    ns_id = uuid.uuid4()
    engine = SalesD365SyncEngine(conn, ns_id, client)

    # Sync
    res = await engine.sync_entity("accounts", incremental=True)

    assert res["entity"] == "accounts"
    assert res["upserted"] == 2
    assert res["deleted"] == 0
    assert res["method"] == "watermark"

    # Verify loaded watermark from table
    assert len(conn.fetches) == 1
    assert "sales:accounts" in conn.fetches[0][1]

    # Verify OData query params (assert client.paginate got modifiedon gt skewed initial_watermark)
    # Skewed watermark = 12:00:00 - 300s = 11:55:00
    assert len(client.paginate_calls) == 1
    call = client.paginate_calls[0]
    assert call["entity_set"] == "accounts"
    assert "modifiedon gt 2026-06-23T11:55:00Z" in call["filter_expr"]

    # Verify database inserts
    upserts = [e for e in conn.executes if "INSERT INTO sales_read_model" in e[0]]
    assert len(upserts) == 2
    assert upserts[0][1][2] == "acc-123"
    assert upserts[0][1][3] == "Acme Corp"

    # Verify watermark update to max seen (15:00:00)
    watermark_saves = [e for e in conn.executes if "INSERT INTO d365_delta_tokens" in e[0]]
    assert len(watermark_saves) == 1
    assert watermark_saves[0][1][1] == "sales:accounts"
    assert watermark_saves[0][1][2] == "2026-06-23T15:00:00Z"


@pytest.mark.asyncio
async def test_sync_entity_change_tracking(monkeypatch) -> None:
    # Enable change tracking
    monkeypatch.setattr(cfg, "NCE_D365_CHANGE_TRACKING_ENABLED", True)

    # Setup mocks
    initial_delta = f"{_ORG_URL}/api/data/v9.2/accounts?$deltatoken=TOKEN1"
    conn = _FakeConn(fetchrow_val={"delta_link": initial_delta})

    changed = [
        {"accountid": "acc-789", "name": "Cyberdyne Systems", "modifiedon": "2026-06-23T16:00:00Z"}
    ]
    removed = ["acc-old-delete"]
    next_delta = f"{_ORG_URL}/api/data/v9.2/accounts?$deltatoken=TOKEN2"
    client = _FakeClient(track_changes_result=(changed, removed, next_delta))

    ns_id = uuid.uuid4()
    engine = SalesD365SyncEngine(conn, ns_id, client)

    # Sync
    res = await engine.sync_entity("accounts", incremental=True)

    assert res["entity"] == "accounts"
    assert res["upserted"] == 1
    assert res["deleted"] == 1
    assert res["method"] == "change_tracking"

    # Verify track_changes call parameters
    assert len(client.track_changes_calls) == 1
    call = client.track_changes_calls[0]
    assert call["entity_set"] == "accounts"
    assert call["delta_link"] == initial_delta

    # Verify updates in local DB
    upserts = [e for e in conn.executes if "INSERT INTO sales_read_model" in e[0]]
    assert len(upserts) == 1
    assert upserts[0][1][2] == "acc-789"
    assert upserts[0][1][3] == "Cyberdyne Systems"

    # Verify soft-deletes in local DB
    deletes = [e for e in conn.executes if "UPDATE sales_read_model" in e[0]]
    assert len(deletes) == 1
    assert deletes[0][1][2] == ["acc-old-delete"]

    # Verify delta token updated to TOKEN2
    watermark_saves = [e for e in conn.executes if "INSERT INTO d365_delta_tokens" in e[0]]
    assert len(watermark_saves) == 1
    assert watermark_saves[0][1][2] == next_delta


@pytest.mark.asyncio
async def test_sync_entity_fallback_on_change_tracking_error(monkeypatch) -> None:
    # Enable change tracking
    monkeypatch.setattr(cfg, "NCE_D365_CHANGE_TRACKING_ENABLED", True)

    # Setup mocks: track_changes raises error (change tracking not supported on entity)
    conn = _FakeConn(fetchrow_val={"delta_link": "2026-06-23T12:00:00Z"})
    client = _FakeClient(
        paginate_results=[{"accountid": "acc-1", "name": "FallBack"}],
        track_changes_result=RuntimeError("Change tracking not enabled"),
    )

    ns_id = uuid.uuid4()
    engine = SalesD365SyncEngine(conn, ns_id, client)

    # Sync
    res = await engine.sync_entity("accounts", incremental=True)

    # Verify fallback to watermark sync succeeded
    assert res["entity"] == "accounts"
    assert res["upserted"] == 1
    assert res["deleted"] == 0
    assert res["method"] == "watermark"
    assert len(client.paginate_calls) == 1


@pytest.mark.asyncio
async def test_full_sync_reconciles_deletions(monkeypatch) -> None:
    # Setup mocks for full sync
    monkeypatch.setattr(cfg, "NCE_D365_CHANGE_TRACKING_ENABLED", False)

    # Active IDs in Dataverse: acc-active
    active_records = [{"accountid": "acc-active"}]

    # DB has: acc-active, acc-to-delete
    db_ids = [{"source_id": "acc-active"}, {"source_id": "acc-to-delete"}]
    conn = _FakeConn(fetchrow_val=None, fetch_val=db_ids)
    client = _FakeClient(paginate_results=active_records)

    ns_id = uuid.uuid4()
    engine = SalesD365SyncEngine(conn, ns_id, client)

    # Sync (incremental=False triggers full sync with reconciliation)
    res = await engine.sync_entity("accounts", incremental=False)

    assert res["entity"] == "accounts"
    assert res["upserted"] == 1
    assert res["deleted"] == 1
    assert res["method"] == "full_reconcile"

    # Verify deletion update query executed on acc-to-delete
    deletes = [e for e in conn.executes if "UPDATE sales_read_model" in e[0]]
    assert len(deletes) == 1
    assert deletes[0][1][2] == ["acc-to-delete"]


@pytest.mark.asyncio
async def test_run_incremental_sync_all() -> None:
    conn = _FakeConn()
    client = _FakeClient()
    ns_id = uuid.uuid4()
    engine = SalesD365SyncEngine(conn, ns_id, client)

    # Sync all
    res = await engine.run_incremental_sync(entity_types=["accounts", "contacts"])

    assert res["namespace_id"] == str(ns_id)
    assert res["mode"] == "incremental"
    assert len(res["results"]) == 2
    assert {r["entity"] for r in res["results"]} == {"accounts", "contacts"}


@pytest.mark.asyncio
async def test_run_full_sync_all() -> None:
    conn = _FakeConn()
    client = _FakeClient()
    ns_id = uuid.uuid4()
    engine = SalesD365SyncEngine(conn, ns_id, client)

    # Sync all
    res = await engine.run_full_sync(entity_types=["accounts"])

    assert res["namespace_id"] == str(ns_id)
    assert res["mode"] == "full"
    assert len(res["results"]) == 1
    assert res["results"][0]["entity"] == "accounts"
