"""TASK-08: Verify schema.sql applies cleanly on a fresh database (idempotency)."""

from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_schema_applies_cleanly_on_fresh_database(pg_admin_conn):
    schema_sql = Path("nce/schema.sql").read_text(encoding="utf-8")

    await pg_admin_conn.execute(schema_sql)
    await pg_admin_conn.execute(schema_sql)  # second apply must not error

    exists = await pg_admin_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='embedding_migrations')"
    )
    assert exists is True

    has_namespace_id = await pg_admin_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='embedding_migrations' "
        "AND column_name='namespace_id')"
    )
    assert has_namespace_id is True

    # settings table verification
    settings_exists = await pg_admin_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='settings')"
    )
    assert settings_exists is True

    # Verify column types
    columns = await pg_admin_conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'settings'
        ORDER BY ordinal_position
        """
    )
    col_map = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in columns}

    assert "key" in col_map
    assert col_map["key"] == ("text", "NO")

    assert "value" in col_map
    assert col_map["value"] == ("jsonb", "YES")  # jsonb

    assert "secret_enc" in col_map
    assert col_map["secret_enc"] == ("bytea", "YES")

    assert "is_secret" in col_map
    assert col_map["is_secret"] == ("boolean", "NO")

    assert "section" in col_map
    assert col_map["section"] == ("text", "YES")

    assert "updated_by" in col_map
    assert col_map["updated_by"] == ("text", "YES")

    assert "updated_at" in col_map
    assert col_map["updated_at"] == ("timestamp with time zone", "NO")

    # Verify RLS-exempt status
    rls_enabled = await pg_admin_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname = 'settings'"
    )
    assert rls_enabled is False

    # Verify nce_app privileges
    grants = await pg_admin_conn.fetch(
        """
        SELECT privilege_type
        FROM information_schema.role_table_grants
        WHERE grantee = 'nce_app' AND table_name = 'settings'
        """
    )
    privileges = {r["privilege_type"] for r in grants}
    assert {"SELECT", "INSERT", "UPDATE", "DELETE"}.issubset(privileges)
