"""
tests/unit/test_netbox_contacts.py
==================================
Unit tests for NetBox Tenancy Contact and Operator Stress Tracking integration.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import Request, Response

from nce.signing import require_master_key
from nce.vertical_modules.netbox.contacts import NetBoxClient, NetBoxContactSync

NS = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def setup_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set a valid 32-byte master key
    monkeypatch.setenv("NCE_MASTER_KEY", "x" * 32)


class MockTransaction:
    async def __aenter__(self) -> MockTransaction:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


class MockConnection:
    def __init__(self) -> None:
        self.fetch_results: list[dict[str, Any]] = []
        self.fetchval_results: list[Any] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        self.fetch_calls.append((query, args))
        return self.fetch_results

    async def fetchval(self, query: str, *args: Any) -> Any:
        return self.fetchval_results.pop(0) if self.fetchval_results else None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "SUCCESS"

    def transaction(self) -> MockTransaction:
        return MockTransaction()


@pytest.mark.anyio
class TestNetBoxClient:
    async def test_fetch_contacts(self, monkeypatch):
        client = NetBoxClient("http://netbox.local", "token123")

        mock_results = {
            "results": [{"name": "John Doe", "email": "jdoe@example.com", "username": "jdoe"}]
        }
        mock_get = AsyncMock(
            return_value=Response(
                200,
                json=mock_results,
                request=Request("GET", "http://netbox.local/api/tenancy/contacts/"),
            )
        )
        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)

        contacts = await client.fetch_contacts()
        assert len(contacts) == 1
        assert contacts[0]["name"] == "John Doe"
        mock_get.assert_called_once_with(
            "http://netbox.local/api/tenancy/contacts/",
            headers={"Authorization": "Token token123", "Accept": "application/json"},
            timeout=10.0,
        )

    async def test_fetch_contacts_timeout_and_config(self, monkeypatch):
        import httpx

        client = NetBoxClient("http://netbox.local", "token123")

        timeout_value = None

        async def mock_send(self, request, *args, **kwargs):
            nonlocal timeout_value
            timeout_value = self.timeout
            raise httpx.ReadTimeout("Request timed out", request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

        with pytest.raises(httpx.TimeoutException):
            await client.fetch_contacts()

        assert timeout_value is not None
        assert timeout_value.read == 30.0


@pytest.mark.anyio
class TestNetBoxContactSync:
    async def test_ensure_on_call_schema(self):
        conn = MockConnection()
        conn.fetchval_results = [False]  # Policy does not exist
        sync = NetBoxContactSync(None, None)

        await sync.ensure_on_call_schema(conn)

        assert any("CREATE TABLE IF NOT EXISTS on_call_routing" in c[0] for c in conn.execute_calls)
        assert any(
            "ALTER TABLE on_call_routing ENABLE ROW LEVEL SECURITY" in c[0]
            for c in conn.execute_calls
        )
        assert any("CREATE POLICY on_call_tenant_isolation" in c[0] for c in conn.execute_calls)

    async def test_evaluate_contact_stress_report(self):
        conn = MockConnection()
        now = datetime.now(timezone.utc)
        # 5 consecutive shifts with frustration (index 5) = 8.0 (burnout)
        conn.fetch_results = [
            {"empathic_tensor": [1.0, 2.0, 3.0, 4.0, 5.0, 8.0], "created_at": now} for _ in range(5)
        ]

        sync = NetBoxContactSync(None, None)
        with require_master_key() as mk:
            report = await sync.evaluate_contact_stress_report(
                conn, NS, "operator_jane", "jane@example.com", mk
            )

        assert report["burnout_alert"] is True
        assert report["record_count"] == 5
        assert report["last_frustration"] == 8.0
        assert report["frustration_trend"] == [8.0] * 5

    async def test_sync_contacts_and_update_oncall_burnout_trigger(self, monkeypatch):
        # 1. Mock NetBox API to return two operators: Jane and Bob
        client_mock = MagicMock()
        client_mock.fetch_contacts = AsyncMock(
            return_value=[
                {"name": "Jane", "email": "jane@example.com", "username": "jane"},
                {"name": "Bob", "email": "bob@example.com", "username": "bob"},
            ]
        )

        conn = MockConnection()
        conn.fetchval_results = [True]  # Policy already exists

        # 2. Mock fetch results to return:
        # Jane has frustration 8.0 (Index 5)
        # Bob has frustration 4.0
        now = datetime.now(timezone.utc)
        jane_records = [{"empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 8.0], "created_at": now}]
        bob_records = [{"empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 4.0], "created_at": now}]

        # Match fetch logic based on username argument
        async def mock_fetch(query, *args):
            if "jane" in args:
                return jane_records
            if "bob" in args:
                return bob_records
            return []

        conn.fetch = mock_fetch

        sync = NetBoxContactSync(None, client_mock)

        # 3. Synchronize
        with require_master_key() as mk:
            results = await sync.sync_contacts_and_update_oncall(conn, NS, mk)

        # Jane should be standby, Bob active
        jane_res = next(r for r in results if r["username"] == "jane")
        bob_res = next(r for r in results if r["username"] == "bob")

        assert jane_res["status"] == "burnout_standby"
        assert jane_res["is_active"] is False
        assert jane_res["weight"] == 0.0

        assert bob_res["status"] == "active"
        assert bob_res["is_active"] is True
        # Bob should receive the redistributed load from Jane (Bob baseline 1.0 + Jane's lost 1.0 = 2.0)
        assert bob_res["weight"] == 2.0

        # Verify SQL updates were executed
        update_calls = [c for c in conn.execute_calls if "UPDATE on_call_routing" in c[0]]
        assert len(update_calls) == 1
        assert update_calls[0][1][0] == 2.0  # Bob's redistributed weight
        assert update_calls[0][1][2] == "bob@example.com"
