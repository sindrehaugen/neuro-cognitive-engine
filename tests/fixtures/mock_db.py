"""
tests/fixtures/mock_db.py
=========================
Generalized connection, transaction, and pool mocks for PostgreSQL database unit tests.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any


class MockTransaction:
    def __init__(self, conn: MockConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.transaction_enters += 1

    async def __aexit__(
        self, exc_type: type | None, exc_val: Exception | None, exc_tb: Any
    ) -> None:
        self.conn.transaction_exits += 1


class MockConnection:
    def __init__(self, fetch_results: dict | list | None = None) -> None:
        self.fetch_results = fetch_results or {}
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.transaction_enters = 0
        self.transaction_exits = 0

    def transaction(self) -> MockTransaction:
        return MockTransaction(self)

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        self.fetch_calls.append((query, args))

        # Support list format for direct mock payloads
        if isinstance(self.fetch_results, list):
            return self.fetch_results

        # Support dict format for query-substring matching
        q_compact = "".join(query.split()).lower()
        for key, value in self.fetch_results.items():
            if "".join(key.split()).lower() in q_compact:
                if isinstance(value, Exception):
                    raise value
                return value
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 1"


class MockPool:
    def __init__(self, conn: MockConnection) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self, timeout: float | None = None) -> AsyncGenerator[MockConnection, None]:
        yield self._conn
