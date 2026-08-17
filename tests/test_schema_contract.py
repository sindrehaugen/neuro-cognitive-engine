import uuid

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provenance_and_depth_columns_exist(pg_admin_conn):
    """Verify that change_origin, origin_event_id, and derivation_depth exist with correct types/defaults."""
    # Check memories columns
    mem_cols = await pg_admin_conn.fetch("""
        SELECT column_name, data_type, column_default
        FROM information_schema.columns
        WHERE table_name = 'memories'
          AND column_name IN ('change_origin', 'origin_event_id', 'derivation_depth')
    """)
    cols = {r["column_name"]: r for r in mem_cols}
    assert "change_origin" in cols
    assert cols["change_origin"]["data_type"] == "text"
    assert "unknown" in cols["change_origin"]["column_default"]

    assert "origin_event_id" in cols
    assert cols["origin_event_id"]["data_type"] == "uuid"

    assert "derivation_depth" in cols
    assert cols["derivation_depth"]["data_type"] == "smallint"
    assert "0" in cols["derivation_depth"]["column_default"]

    # Check kg_nodes columns
    node_cols = await pg_admin_conn.fetch("""
        SELECT column_name, data_type, column_default
        FROM information_schema.columns
        WHERE table_name = 'kg_nodes'
          AND column_name IN ('change_origin', 'origin_event_id')
    """)
    cols = {r["column_name"]: r for r in node_cols}
    assert "change_origin" in cols
    assert cols["change_origin"]["data_type"] == "text"
    assert "unknown" in cols["change_origin"]["column_default"]
    assert "origin_event_id" in cols
    assert cols["origin_event_id"]["data_type"] == "uuid"

    # Check kg_edges columns
    edge_cols = await pg_admin_conn.fetch("""
        SELECT column_name, data_type, column_default
        FROM information_schema.columns
        WHERE table_name = 'kg_edges'
          AND column_name IN ('change_origin', 'origin_event_id')
    """)
    cols = {r["column_name"]: r for r in edge_cols}
    assert "change_origin" in cols
    assert cols["change_origin"]["data_type"] == "text"
    assert "unknown" in cols["change_origin"]["column_default"]
    assert "origin_event_id" in cols
    assert cols["origin_event_id"]["data_type"] == "uuid"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dlq_triage_columns_exist(pg_admin_conn):
    """Verify that error_fingerprint and quarantined_until exist on dead_letter_queue."""
    dlq_cols = await pg_admin_conn.fetch("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'dead_letter_queue'
          AND column_name IN ('error_fingerprint', 'quarantined_until')
    """)
    cols = {r["column_name"]: r for r in dlq_cols}
    assert "error_fingerprint" in cols
    assert cols["error_fingerprint"]["data_type"] == "text"
    assert "quarantined_until" in cols
    assert "timestamp" in cols["quarantined_until"]["data_type"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_tables_rls_active(pg_admin_conn):
    """Verify that RLS is enabled and forced on the 5 new tables."""
    new_tables = [
        "processed_outbox_events",
        "actor_trust",
        "event_parents",
        "action_approval_queue",
        "action_idempotency",
    ]
    rows = await pg_admin_conn.fetch(
        """
        SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS force_rls_enabled
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = ANY($1::text[])
    """,
        new_tables,
    )
    found = {r["table_name"]: r for r in rows}
    for t in new_tables:
        assert t in found, f"Table {t} does not exist in the database"
        assert found[t]["rls_enabled"] is True, f"RLS is not enabled on {t}"
        assert found[t]["force_rls_enabled"] is True, f"FORCE RLS is not active on {t}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_parents_worm_enforcement(pg_admin_conn):
    """Verify that event_parents is append-only and blocks UPDATE and DELETE."""
    # Ensure a test namespace exists
    ns_id = await pg_admin_conn.fetchval("SELECT id FROM namespaces LIMIT 1")
    if not ns_id:
        ns_id = await pg_admin_conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id", "test-worm-parent-ns"
        )

    # Insert a dummy row
    event_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    await pg_admin_conn.execute(
        "INSERT INTO event_parents (event_id, parent_event_id, namespace_id) VALUES ($1, $2, $3)",
        event_id,
        parent_id,
        ns_id,
    )

    # UPDATE must be blocked
    with pytest.raises(Exception) as exc_info:
        await pg_admin_conn.execute(
            "UPDATE event_parents SET parent_event_id = $1 WHERE event_id = $2",
            uuid.uuid4(),
            event_id,
        )
    assert "immutable" in str(exc_info.value).lower() or "forbidden" in str(exc_info.value).lower()

    # DELETE must be blocked
    with pytest.raises(Exception) as exc_info:
        await pg_admin_conn.execute("DELETE FROM event_parents WHERE event_id = $1", event_id)
    assert "immutable" in str(exc_info.value).lower() or "forbidden" in str(exc_info.value).lower()
