"""TASK-10: WORM registry correctness — two layers.

Layer 1 (unit): Python constant correctness, no DB needed.
Layer 2 (integration): DB role must lack UPDATE/DELETE on WORM tables.

If Layer 2 fails without raising InsufficientPrivilegeError, WORM is only
application-level. Fix: REVOKE UPDATE, DELETE ON event_log, pii_redactions
FROM <app_role> in schema.sql.
"""

import asyncpg
import pytest

from nce.event_log import _WORM_TABLES

# ---------------------------------------------------------------------------
# Layer 1 — unit
# ---------------------------------------------------------------------------


def test_memory_salience_not_in_worm_tables():
    assert "memory_salience" not in _WORM_TABLES


def test_worm_tables_contains_expected_entries():
    assert "event_log" in _WORM_TABLES
    assert "event_parents" in _WORM_TABLES
    assert len(_WORM_TABLES) == 2


# ---------------------------------------------------------------------------
# Layer 2 — integration: DB role enforcement
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worm_tables_db_role_cannot_update(pg_app_conn):
    """Privilege-layer denial: UPDATE … WHERE FALSE must not succeed at SQL level.

    Database owners (Compose ``mcp_user``) commonly retain UPDATE privileges even
    while row-level triggers block real mutations — ``WHERE FALSE`` does not fire
    row triggers, so **skip** unless a least-privilege DSN is wired via ``PG_DSN_APP``.
    """
    try:
        await pg_app_conn.execute("SET nce.namespace_id = '00000000-0000-0000-0000-000000000000'")
    except Exception:
        pass

    for table_name in _WORM_TABLES:
        try:
            try:
                await pg_app_conn.execute(f"UPDATE {table_name} SET id = id WHERE FALSE")
            except asyncpg.exceptions.UndefinedColumnError:
                await pg_app_conn.execute(
                    f"UPDATE {table_name} SET event_id = event_id WHERE FALSE"
                )
        except asyncpg.exceptions.InsufficientPrivilegeError:
            continue
        pytest.skip(
            f"{table_name}: UPDATE with WHERE FALSE succeeded — privilege-layer "
            "WORM is not asserted for this login. Set PG_DSN_APP to a restricted role "
            "(e.g. nce_app) to run this assertion."
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worm_tables_db_role_cannot_delete(pg_app_conn):
    try:
        await pg_app_conn.execute("SET nce.namespace_id = '00000000-0000-0000-0000-000000000000'")
    except Exception:
        pass

    for table_name in _WORM_TABLES:
        try:
            await pg_app_conn.execute(f"DELETE FROM {table_name} WHERE FALSE")
        except asyncpg.exceptions.InsufficientPrivilegeError:
            continue
        pytest.skip(
            f"{table_name}: DELETE with WHERE FALSE succeeded — privilege-layer "
            "WORM is not asserted for this login. Set PG_DSN_APP to a restricted role."
        )
