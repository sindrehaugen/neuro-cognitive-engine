"""
Optional Postgres probes for event_log append-only grants (Roadmap §2.3 / TEST-2.3-05).

Set ``NCE_TEST_PG_DSN`` to a DSN connected as a role that can read
``information_schema`` (typically superuser maintenance account used in CI smoke).

Skipped automatically when unset — CI without Postgres still passes locally.
"""

from __future__ import annotations

import os

import pytest

PG_DSN = os.getenv("NCE_TEST_PG_DSN", "")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PG_DSN.strip(),
    reason="NCE_TEST_PG_DSN not configured — Postgres privilege probes skipped.",
)
async def test_nce_app_missing_update_delete_grants_event_log() -> None:
    import asyncpg

    conn = await asyncpg.connect(PG_DSN)
    try:
        rows = await conn.fetch("""
            SELECT privilege_type
            FROM   information_schema.role_table_grants
            WHERE  table_schema = 'public'
              AND  table_name   = 'event_log'
              AND  grantee     = 'nce_app'
            """)
    finally:
        await conn.close()

    if not rows:
        pytest.skip("Role nce_app has no grants on event_log (schema not bootstrapped).")

    privs = {r["privilege_type"] for r in rows}
    assert "INSERT" in privs
    assert "SELECT" in privs
    assert "UPDATE" not in privs
    assert "DELETE" not in privs
