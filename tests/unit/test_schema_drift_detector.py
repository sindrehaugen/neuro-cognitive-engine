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


# ---------------------------------------------------------------------------
# Q-4 step 1b — RLS coverage
#
# The column-only version of this checker reported a database as clean while four tables
# had their tenant-isolation policy ONLY in schema.sql. A missing column eventually raises
# an error at query time; a missing policy raises nothing at all. It is one tenant reading
# another tenant's rows with every test still green.
# ---------------------------------------------------------------------------


def test_the_dynamic_tenant_loop_is_expanded_not_skipped() -> None:
    """🔴 Most of this estate's RLS is dynamic, not static DDL.

    ``schema.sql`` declares ``tenant_tables TEXT[] := ARRAY[...]`` and loops over it,
    executing ``format('CREATE POLICY ... ON public.%I', t)``. A naive regex captures
    ``%I`` as the table name and yields a policy on an empty string -- a permanent false
    positive that would make the check red forever. Skipping the loop instead would drop
    the majority of the estate's policies from the expected set.
    """
    from check_schema_drift import _strip_sql_comments, tenant_tables_loop

    schema = Path(__file__).resolve().parents[2] / "nce" / "schema.sql"
    looped = tenant_tables_loop(_strip_sql_comments(schema.read_text(encoding="utf-8")))

    assert len(looped) > 30, f"the tenant_tables array should be large; parsed {len(looped)}"
    for core in ("memories", "kg_nodes", "kg_edges"):
        assert core in looped, f"{core} fell out of the tenant-isolation loop"


def test_no_policy_is_parsed_with_an_empty_table_name() -> None:
    """The ``%I`` placeholder must never reach the expected set as a real table."""
    from check_schema_drift import expected_rls

    _enabled, policies = expected_rls()
    malformed = [p for p in policies if not p[0] or not p[1]]
    assert not malformed, (
        f"malformed policy entries would be permanent false positives: {malformed}"
    )


def test_the_four_schema_only_rls_tables_are_expected() -> None:
    """These four have policy AND enablement in schema.sql and in no migration.

    Measured on `f2dba69`. They are the reason migration 082 exists, and the reason Q-4
    step 2 cannot land on the column-only checker.
    """
    from check_schema_drift import expected_rls

    enabled, policies = expected_rls()
    for table in ("outbox_events", "replay_runs", "saga_execution_log", "topology_graph"):
        assert table in enabled, f"{table} lost its expected RLS enablement"
    assert ("outbox_events", "tenant_isolation_policy") in policies
    assert ("topology_graph", "topology_graph_tenant_isolation") in policies


def test_compare_rls_reports_missing_enablement_and_policies() -> None:
    from check_schema_drift import compare_rls

    expected = ({"a", "b"}, {("a", "pol_a"), ("b", "pol_b")})
    actual = ({"a"}, {("a", "pol_a")})
    assert compare_rls(expected, actual) == (["b"], ["b.pol_b"])


def test_compare_rls_is_clean_when_the_database_matches() -> None:
    """🔴 The over-claim control, again. Red on a correct database gets switched off."""
    from check_schema_drift import compare_rls

    state = ({"a", "b"}, {("a", "pol_a"), ("b", "pol_b")})
    assert compare_rls(state, state) == ([], [])


def test_extra_rls_in_the_database_is_not_drift() -> None:
    """Migrations enable RLS on tables schema.sql never mentions; that is them working."""
    from check_schema_drift import compare_rls

    expected = ({"a"}, {("a", "pol_a")})
    actual = ({"a", "from_a_migration"}, {("a", "pol_a"), ("from_a_migration", "pol_m")})
    assert compare_rls(expected, actual) == ([], [])


@pytest.mark.parametrize(
    "table,policy",
    [
        ("outbox_events", "tenant_isolation_policy"),
        ("replay_runs", "tenant_isolation_policy"),
        ("saga_execution_log", "tenant_isolation_policy"),
        ("topology_graph", "topology_graph_tenant_isolation"),
    ],
)
def test_migration_082_covers_every_schema_only_policy(table: str, policy: str) -> None:
    """🔴 The step-2 safety interlock for RLS, mirroring the one 081 has for columns."""
    mig = (
        Path(__file__).resolve().parents[2]
        / "nce"
        / "migrations"
        / "082_schema_sql_only_rls_baseline.sql"
    ).read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in mig
    assert table in mig, f"082 no longer covers {table}"
    assert policy in mig, f"082 no longer creates {policy}"


def test_migration_082_reproduces_topology_graph_weakly_on_purpose() -> None:
    """🔴 A baseline must not silently tighten a security predicate.

    ``topology_graph``'s policy in ``schema.sql`` has no ``WITH CHECK``, so it constrains
    reads but not writes -- a caller can INSERT a row carrying another namespace's id.
    082 reproduces that faithfully. Adding ``WITH CHECK`` here would be a behaviour change
    wearing a no-op's clothing, landing without anyone deciding it. The gap is filed as
    its own row instead.

    If someone strengthens it, this test fails and forces the decision into the open.
    """
    mig = (
        Path(__file__).resolve().parents[2]
        / "nce"
        / "migrations"
        / "082_schema_sql_only_rls_baseline.sql"
    ).read_text(encoding="utf-8")
    start = mig.index("CREATE POLICY topology_graph_tenant_isolation")
    body = mig[start : mig.index(";", start)]
    assert "WITH CHECK" not in body, (
        "082 now adds WITH CHECK to topology_graph. That is a real security improvement "
        "and it may well be right -- but it is a behaviour change, so it does not belong "
        "in a baseline migration. Land it as its own PR with its own reasoning."
    )


# ---------------------------------------------------------------------------
# The RLS-posture report (F11 ratchet)
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal asyncpg stand-in, mirroring tests/test_rewrap_sweep_requires_bypassrls.py."""

    def __init__(self, *, can_bypass: bool | None, user: str = "probe_role") -> None:
        self._can_bypass = can_bypass
        self._user = user

    async def fetchval(self, query: str, *_args: object) -> object:
        if "rolbypassrls" in query:
            return self._can_bypass
        if "current_user" in query:
            return self._user
        raise AssertionError(f"unexpected fetchval: {query}")

    async def close(self) -> None:  # pragma: no cover - not used by these tests
        pass


@pytest.mark.asyncio
async def test_posture_is_silent_for_a_bypassrls_role() -> None:
    """``mcp_user`` bypasses RLS, so there is nothing to caveat about the verdict."""
    from nce.master_key_registry import rls_visibility_problem

    assert await rls_visibility_problem(_FakeConn(can_bypass=True, user="mcp_user")) is None


@pytest.mark.asyncio
async def test_posture_speaks_up_for_a_restricted_role() -> None:
    """🔴 The point of calling the guard here.

    This script reads only catalogues, so unlike ``rewrap_master_key.py`` its answer is
    correct for any role and it does NOT refuse. What it must not do is let "0 missing
    policies" be read as "tenant isolation is in force" by someone whose role cannot see
    the data those policies govern.
    """
    from nce.master_key_registry import rls_visibility_problem

    msg = await rls_visibility_problem(_FakeConn(can_bypass=False, user="nce_app"))
    assert msg is not None
    assert "nce_app" in msg


def test_the_posture_call_is_a_report_not_a_refusal() -> None:
    """🔴 Pin the deliberate difference from the rewrap sweep.

    ``rewrap_master_key.py`` REFUSES on a restricted role, because it reads wrapped data
    rows and would under-report silently. This script reads ``information_schema`` and the
    ``pg_*`` catalogues, which are not RLS-filtered, so refusing would be cargo-cult and
    would make the checker unusable for exactly the roles most worth checking.

    If someone converts this into a refusal, that is a real decision and it should be made
    on purpose, not by copying the neighbouring script.
    """
    src = (Path(__file__).resolve().parents[2] / "scripts" / "check_schema_drift.py").read_text(
        encoding="utf-8"
    )
    start = src.index("async def rls_role_posture")
    body = src[start : src.index("\ndef ", start)]
    assert "return await rls_visibility_problem(conn)" in body
    assert "sys.exit" not in body, "posture must not terminate the run"
    assert "raise" not in body, "posture must not refuse; it reports"
