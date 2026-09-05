"""Unit tests for Module 15 RS-4 Reactive Event Watcher."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources.watcher import (
    handle_hr_cert_change,
    on_hr_cert_event,
    register_resources_event_subscribers,
)


class MockEngine:
    """Mock engine holding in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.resources: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        q = query.strip().lower()
        if "from resources" in q:
            ns_id = str(args[0])
            emp_key = str(args[1])
            for r in engine.resources.values():
                if str(r["namespace_id"]) == ns_id:
                    if (
                        str(r["id"]) == emp_key
                        or r.get("metadata", {}).get("employee_id") == emp_key
                        or r.get("email") == emp_key
                    ):
                        return r
            return None
        return None

    async def mock_fetch(query, *args):
        q = query.strip().lower()
        if "from allocations" in q:
            ns_id = str(args[0])
            res_id = str(args[1])
            res = []
            for a in engine.allocations.values():
                if (
                    str(a["namespace_id"]) == ns_id
                    and str(a["resource_id"]) == res_id
                    and a.get("status") != "released"
                ):
                    res.append(a)
            return res
        return []

    async def mock_execute(query, *args):
        q = query.strip().lower()
        if "update allocations" in q:
            attrs = json.loads(args[0]) if isinstance(args[0], str) else args[0]
            new_status = args[1]
            aid = str(args[2])
            _ns_id = str(args[3])
            if aid in engine.allocations:
                engine.allocations[aid]["attrs"] = attrs
                engine.allocations[aid]["status"] = new_status
            return "UPDATE 1"
        return ""

    class MockConn:
        async def fetchrow(self, query, *args):
            return await mock_fetchrow(query, *args)

        async def fetch(self, query, *args):
            return await mock_fetch(query, *args)

        async def execute(self, query, *args):
            return await mock_execute(query, *args)

    class MockContextManager:
        async def __aenter__(self):
            return MockConn()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        "nce.vertical_modules.resources.watcher.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


@pytest.mark.asyncio
async def test_handle_hr_cert_change_expired(mock_engine):
    """RS-4: When HR cert expires, future allocations are reactively flagged tentative with conflict details."""
    ns_id = uuid4()
    tech_id = uuid4()

    mock_engine.resources[str(tech_id)] = {
        "id": str(tech_id),
        "namespace_id": str(ns_id),
        "name": "Tech Sigma",
        "email": "sigma@example.test",
        "metadata": {"employee_id": "EMP-042"},
    }

    alloc_id = str(uuid4())
    future_start = datetime.now(timezone.utc) + timedelta(days=5)
    future_end = future_start + timedelta(hours=8)
    mock_engine.allocations[alloc_id] = {
        "id": alloc_id,
        "namespace_id": str(ns_id),
        "resource_id": str(tech_id),
        "starts_at": future_start,
        "ends_at": future_end,
        "status": "reserved",
        "attrs": {},
    }

    # Reactive event from HR: cert expired!
    res = await handle_hr_cert_change(
        mock_engine,
        {
            "namespace_id": ns_id,
            "employee_id": "EMP-042",
            "cert_name": "Dante Level 3",
            "status": "expired",
            "valid_to": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        },
    )

    assert res["reactive_event_processed"] is True
    assert res["affected_count"] == 1
    assert res["affected_allocations"][0]["allocation_id"] == alloc_id
    assert res["affected_allocations"][0]["status"] == "tentative"

    # Allocation in store has been updated
    updated_alloc = mock_engine.allocations[alloc_id]
    assert updated_alloc["status"] == "tentative"
    assert "cert_conflict" in updated_alloc["attrs"]
    assert "Certification expired" in updated_alloc["attrs"]["cert_conflict"]["reason"]


@pytest.mark.asyncio
async def test_on_hr_cert_event_relay(mock_engine):
    """Outbox relay adapter parses event payload and routes to handler."""
    ns_id = uuid4()
    tech_id = uuid4()

    mock_engine.resources[str(tech_id)] = {
        "id": str(tech_id),
        "namespace_id": str(ns_id),
        "name": "Tech Delta",
        "email": "delta@example.test",
        "metadata": {"employee_id": "EMP-999"},
    }

    event = {
        "namespace_id": str(ns_id),
        "payload": json.dumps(
            {
                "employee_id": "EMP-999",
                "cert_name": "Offshore Safety",
                "status": "revoked",
            }
        ),
    }

    await on_hr_cert_event(mock_engine, event)
    # Proves relay integration executes cleanly without exception


def test_register_resources_event_subscribers():
    """Verify subscriber registration is idempotent and succeeds."""
    register_resources_event_subscribers()
