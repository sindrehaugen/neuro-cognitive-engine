"""
Tests for event_log WORM enforcement startup probe.

Verifies that ``verify_worm_enforcement()`` correctly detects:
- Expected: UPDATE and DELETE denied (InsufficientPrivilegeError) on each WORM table
- Critical: UPDATE or DELETE succeeds → RuntimeError raised
- Edge cases: table missing, unexpected errors
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import pytest

from nce.event_log import _WORM_TABLES, verify_worm_enforcement

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn() -> AsyncMock:
    """Return a mock asyncpg Connection with an AsyncMock execute."""
    c = AsyncMock(spec=asyncpg.Connection)
    c.execute = AsyncMock()
    return c


# ---------------------------------------------------------------------------
# Happy path — UPDATE and DELETE both denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worm_update_denied_delete_denied():
    """UPDATE and DELETE both raise InsufficientPrivilegeError → probe passes."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        if "UPDATE" in sql:
            raise asyncpg.exceptions.InsufficientPrivilegeError("UPDATE denied")
        if "DELETE" in sql:
            raise asyncpg.exceptions.InsufficientPrivilegeError("DELETE denied")
        return None

    conn.execute.side_effect = mock_execute

    # Should not raise
    await verify_worm_enforcement(conn)

    # For each table, we expect 3 calls: SET, UPDATE, DELETE
    assert conn.execute.call_count == len(_WORM_TABLES) * 3
    # Check that SET, UPDATE, and DELETE were called for each table
    calls = [call[0][0] for call in conn.execute.call_args_list]
    for table in _WORM_TABLES:
        assert any("SET nce.namespace_id" in c for c in calls)
        assert any("UPDATE" in c and table in c for c in calls)
        assert any("DELETE" in c and table in c for c in calls)


# ---------------------------------------------------------------------------
# Critical path — UPDATE or DELETE succeeds (WORM broken)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worm_update_succeeds_raises_runtime_error():
    """UPDATE succeeds → RuntimeError halts startup."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        if "UPDATE" in sql:
            return None  # UPDATE succeeds — DANGER
        return None

    conn.execute.side_effect = mock_execute

    with pytest.raises(RuntimeError, match="UPDATE on event_log succeeded"):
        await verify_worm_enforcement(conn)


@pytest.mark.asyncio
async def test_worm_delete_succeeds_raises_runtime_error():
    """DELETE succeeds → RuntimeError halts startup."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        if "UPDATE" in sql:
            raise asyncpg.exceptions.InsufficientPrivilegeError("UPDATE denied")
        if "DELETE" in sql:
            return None  # DELETE succeeds — DANGER
        return None

    conn.execute.side_effect = mock_execute

    with pytest.raises(RuntimeError, match="DELETE on event_log succeeded"):
        await verify_worm_enforcement(conn)


@pytest.mark.asyncio
async def test_worm_both_succeed_raises_on_update_first():
    """If both UPDATE and DELETE succeed, only UPDATE error is raised."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        if "UPDATE" in sql:
            return None  # UPDATE succeeds — DANGER
        if "DELETE" in sql:
            return None
        return None

    conn.execute.side_effect = mock_execute

    with pytest.raises(RuntimeError, match="UPDATE on event_log succeeded"):
        await verify_worm_enforcement(conn)


# ---------------------------------------------------------------------------
# Edge cases — table missing, unexpected errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_missing_propagates_error():
    """If the table doesn't exist, the error propagates (not caught)."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        raise asyncpg.exceptions.UndefinedTableError("event_log missing")

    conn.execute.side_effect = mock_execute

    with pytest.raises(asyncpg.exceptions.UndefinedTableError):
        await verify_worm_enforcement(conn)


@pytest.mark.asyncio
async def test_unexpected_postgres_error_propagates():
    """Non-privilege errors propagate unchanged."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        raise asyncpg.exceptions.ConnectionFailureError("broken")

    conn.execute.side_effect = mock_execute

    with pytest.raises(asyncpg.exceptions.ConnectionFailureError):
        await verify_worm_enforcement(conn)


# ---------------------------------------------------------------------------
# SQL injection safety: WHERE FALSE clause prevents data mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_uses_where_false():
    """Both probes use WHERE FALSE so no rows can ever be affected."""
    conn = _conn()

    def mock_execute(sql, *args, **kwargs):
        if "SET nce.namespace_id" in sql:
            return None
        if "UPDATE" in sql:
            raise asyncpg.exceptions.InsufficientPrivilegeError("no")
        if "DELETE" in sql:
            raise asyncpg.exceptions.InsufficientPrivilegeError("no")
        return None

    conn.execute.side_effect = mock_execute

    await verify_worm_enforcement(conn)

    for call in conn.execute.call_args_list:
        sql = call[0][0]
        if "SET nce" not in sql:
            assert "WHERE FALSE" in sql


# ---------------------------------------------------------------------------
# Exception class verification — confirm we catch the right exception
# ---------------------------------------------------------------------------


def test_insufficient_privilege_is_expected_exception():
    """Confirm InsufficientPrivilegeError is the specific asyncpg exception."""
    exc = asyncpg.exceptions.InsufficientPrivilegeError("test")
    assert isinstance(exc, asyncpg.exceptions.PostgresError)
    # SQLSTATE 42501 = insufficient_privilege
    assert exc.sqlstate == "42501"
