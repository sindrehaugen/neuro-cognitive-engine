"""Q-4 step 1 — the drift detector must not over-claim and must not be blind.

Context, because this row's premise was wrong for two weeks and the correction is the
reason the script exists:

``NCEEngine.connect()`` runs ``schema.sql`` in full on **every connect**
(``orchestrator.py:205``), and its unguarded ``ADD COLUMN`` statements live inside
``DO $$`` blocks that probe ``information_schema`` first. So ``schema.sql`` is not a dead
second writer -- it is a migration runner that keeps no ledger. Measured on the live
database 2026-09-17: ``applied_migrations`` held 74 rows, max ``078``, while all ten
columns that exist only in ``schema.sql`` were present.

Before ``schema.sql`` can become bootstrap-only (step 2), something has to answer *"is this
database missing anything schema.sql would have given it?"* without running ``schema.sql``
to find out. These tests pin that something.

Both failure directions are asserted, because the two ways a checker can be useless are
opposite: one that reports drift on a correct database gets switched off, and one that
reports none on a broken database was never looking.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_schema_drift import (  # noqa: E402
    _extract_add_column,
    _extract_create_table,
    _split_top_level_commas,
    _strip_sql_comments,
    compare,
    expected_columns,
)

# ---------------------------------------------------------------------------
# Parsing: the places a naive regex gets this wrong
# ---------------------------------------------------------------------------


def test_commented_out_ddl_is_not_treated_as_a_promise() -> None:
    """🔴 A retired column must not be reported as drift forever.

    If a commented-out ``ADD COLUMN`` counted, the checker would demand a column the file
    deliberately stopped providing -- and the only way to silence it would be to add the
    column back.
    """
    sql = _strip_sql_comments(
        """
        -- ALTER TABLE memories ADD COLUMN retired_col TEXT;
        /* ALTER TABLE memories ADD COLUMN also_retired TEXT; */
        ALTER TABLE memories ADD COLUMN IF NOT EXISTS real_col TEXT;
        """
    )
    found = _extract_add_column(sql)
    assert ("memories", "real_col") in found
    assert ("memories", "retired_col") not in found
    assert ("memories", "also_retired") not in found


def test_commas_inside_types_do_not_split_columns() -> None:
    """``NUMERIC(12, 2)`` contains a comma that does not end a column definition."""
    parts = _split_top_level_commas("id UUID, amount NUMERIC(12, 2), note TEXT")
    assert [p.strip().split()[0] for p in parts] == ["id", "amount", "note"]


def test_table_level_constraints_are_not_mistaken_for_columns() -> None:
    """``PRIMARY KEY (a, b)`` is a constraint; ``PRIMARY`` is not a column name."""
    tables = _extract_create_table(
        """
        CREATE TABLE IF NOT EXISTS widget (
            id UUID,
            namespace_id UUID,
            PRIMARY KEY (id, namespace_id),
            CONSTRAINT widget_ns_fk FOREIGN KEY (namespace_id) REFERENCES namespaces(id),
            CHECK (id IS NOT NULL)
        );
        """
    )
    assert tables["widget"] == {"id", "namespace_id"}


def test_nested_parens_do_not_end_the_table_body_early() -> None:
    """A column after a parenthesised default must still be found."""
    tables = _extract_create_table(
        """
        CREATE TABLE thing (
            id UUID,
            meta JSONB NOT NULL DEFAULT '{}'::jsonb,
            score NUMERIC(8, 3) DEFAULT (0.0),
            trailing TEXT
        );
        """
    )
    assert "trailing" in tables["thing"]


def test_add_column_on_a_table_created_elsewhere_is_still_recorded() -> None:
    """The file promises the column even if it does not create the table here."""
    sql = "ALTER TABLE only_altered ADD COLUMN IF NOT EXISTS late_col TEXT;"
    tables = _extract_create_table(sql)
    assert "only_altered" not in tables
    assert ("only_altered", "late_col") in _extract_add_column(sql)


# ---------------------------------------------------------------------------
# compare(): both directions
# ---------------------------------------------------------------------------


def test_a_matching_database_reports_no_drift() -> None:
    """🔴 The over-claim control. A checker that cries wolf gets switched off."""
    expected = {"memories": {"id", "metadata"}, "kg_nodes": {"id", "namespace_id"}}
    actual = {"memories": {"id", "metadata"}, "kg_nodes": {"id", "namespace_id"}}
    assert compare(expected, actual) == ([], {})


def test_a_missing_column_is_detected() -> None:
    expected = {"kg_nodes": {"id", "namespace_id"}}
    actual = {"kg_nodes": {"id"}}
    assert compare(expected, actual) == ([], {"kg_nodes": ["namespace_id"]})


def test_a_missing_table_is_detected() -> None:
    expected = {"consolidation_runs": {"id"}}
    assert compare(expected, {}) == (["consolidation_runs"], {})


def test_extra_columns_in_the_database_are_not_drift() -> None:
    """Migrations legitimately add tables and columns schema.sql never mentions.

    Measured 2026-09-17: schema.sql guarantees 94 tables; the live database had 132. The
    other 38 come from the migration chain. Reporting those as drift would make the
    checker useless on every real deployment.
    """
    expected = {"memories": {"id"}}
    actual = {"memories": {"id", "added_by_a_migration"}, "wholly_new_table": {"id"}}
    assert compare(expected, actual) == ([], {})


# ---------------------------------------------------------------------------
# The real schema.sql
# ---------------------------------------------------------------------------


def test_expected_columns_parses_the_real_schema_and_finds_the_known_ten() -> None:
    """The ten columns that live only in schema.sql must all be recognised as promises.

    These are the reason migration 081 exists. If the parser stopped seeing one, step 2
    would look safe while silently dropping it.
    """
    exp = expected_columns()
    assert len(exp) > 50, "schema.sql should yield many tables; the parser likely broke"
    for table, col in [
        ("kg_nodes", "namespace_id"),
        ("kg_edges", "namespace_id"),
        ("kg_nodes", "embedding_model_id"),
        ("memories", "metadata"),
        ("consolidation_runs", "completed_at"),
        ("consolidation_runs", "events_processed"),
        ("consolidation_runs", "clusters_formed"),
        ("consolidation_runs", "abstractions_created"),
        ("consolidation_runs", "error_message"),
        ("bridge_subscriptions", "oauth_access_token_enc"),
    ]:
        assert col in exp.get(table, set()), f"{table}.{col} is no longer parsed as a promise"


def test_the_core_tables_are_promised_by_schema_sql() -> None:
    """These exist in schema.sql and in ZERO migrations — which is why step 1 cannot be
    "port everything to migrations", and why schema.sql stays the bootstrap."""
    exp = expected_columns()
    for table in ("memories", "kg_nodes", "kg_edges", "event_log", "namespaces"):
        assert table in exp, f"{table} vanished from schema.sql's guarantees"


@pytest.mark.parametrize(
    "table,col",
    [
        ("kg_nodes", "namespace_id"),
        ("kg_edges", "namespace_id"),
        ("memories", "metadata"),
        ("consolidation_runs", "error_message"),
        ("bridge_subscriptions", "oauth_access_token_enc"),
    ],
)
def test_migration_081_covers_every_schema_only_column(table: str, col: str) -> None:
    """🔴 The step-2 safety interlock.

    Once ``_init_pg_schema`` is bootstrap-only, an existing database stops receiving
    anything that lives solely in ``schema.sql``. Migration 081 is the ledgered path to
    these columns. If a future edit removes one from 081 while it is still only in
    ``schema.sql``, step 2 becomes unsafe again — so it fails here instead.
    """
    mig = (
        Path(__file__).resolve().parents[2]
        / "nce"
        / "migrations"
        / "081_schema_sql_only_ddl_baseline.sql"
    ).read_text(encoding="utf-8")
    assert f"ALTER TABLE {table}" in mig, f"081 no longer touches {table}"
    assert col in mig, f"081 no longer provides {table}.{col}"
