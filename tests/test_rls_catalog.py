"""TASK-09: Verify deployed schema matches RLS intent declarations."""

import pytest

from nce.event_log import EXPECTED_TENANT_RLS_TABLES, verify_rls_catalog_consistency


async def _require_current_tenant_columns(conn) -> None:
    """Skip when the database predates ``schema.sql`` tenant RLS columns."""

    missing: list[str] = []
    for table_name, col in EXPECTED_TENANT_RLS_TABLES.items():
        has_table = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM   information_schema.tables
                WHERE  table_schema = 'public'
                  AND  table_name   = $1
            )
            """,
            table_name,
        )
        if not has_table:
            missing.append(f"{table_name} (table missing)")
            continue
        has_col = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM   information_schema.columns
                WHERE  table_schema = 'public'
                  AND  table_name   = $1
                  AND  column_name  = $2
            )
            """,
            table_name,
            col,
        )
        if not has_col:
            missing.append(f"{table_name}.{col}")
    if missing:
        sample = "; ".join(missing[:12])
        pytest.skip(
            "RLS catalog test needs current nce/schema.sql tenant tables — gaps: "
            f"{sample}" + ("; …" if len(missing) > 12 else ""),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rls_catalog_consistency(pg_admin_conn, pg_app_conn):
    await _require_current_tenant_columns(pg_admin_conn)
    # Verify using the database owner/admin connection to test full catalog schema visibility
    await verify_rls_catalog_consistency(pg_admin_conn)
    # Verify using the nce_app application role connection
    await verify_rls_catalog_consistency(pg_app_conn)


def test_static_schema_rls_declarations() -> None:
    """Verify statically that all tables in schema.sql referencing namespaces are registered in event_log.py."""
    import re
    from pathlib import Path

    from nce.event_log import (
        EXPECTED_GLOBAL_TABLES,
        EXPECTED_SPECIAL_RLS_TABLES,
        EXPECTED_TENANT_RLS_TABLES,
    )

    repo_root = Path(__file__).resolve().parents[1]
    schema_path = repo_root / "nce" / "schema.sql"
    assert schema_path.is_file(), f"schema.sql not found at {schema_path}"

    schema_content = schema_path.read_text(encoding="utf-8")

    # Balancing logic to extract table body safely including nested parentheses
    def extract_tables(schema_text: str) -> list[tuple[str, str]]:
        tables = []
        start_pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)\s*\(", re.IGNORECASE
        )
        for match in start_pattern.finditer(schema_text):
            table_name = match.group(1)
            start_idx = match.end()
            paren_count = 1
            curr_idx = start_idx
            while curr_idx < len(schema_text) and paren_count > 0:
                char = schema_text[curr_idx]
                if char == "(":
                    paren_count += 1
                elif char == ")":
                    paren_count -= 1
                curr_idx += 1
            if paren_count == 0:
                table_body = schema_text[start_idx : curr_idx - 1]
                tables.append((table_name.lower(), table_body.lower()))
        return tables

    registered_tables = (
        set(EXPECTED_TENANT_RLS_TABLES.keys())
        | set(EXPECTED_SPECIAL_RLS_TABLES.keys())
        | EXPECTED_GLOBAL_TABLES
    )

    # Exclude root namespaces and custom tables that handle RLS differently
    ignore_tables = {"namespaces", "replay_runs"}
    unregistered = []

    for name, body in extract_tables(schema_content):
        if name in ignore_tables:
            continue
        if "namespace_id" in body or "namespaces" in body:
            if name not in registered_tables:
                unregistered.append(name)

    assert not unregistered, (
        f"drift: tables in schema.sql referencing namespace_id must be declared in "
        f"nce/event_log.py (in EXPECTED_TENANT_RLS_TABLES, EXPECTED_SPECIAL_RLS_TABLES, or EXPECTED_GLOBAL_TABLES): {sorted(unregistered)}"
    )
