"""Q-4 step 2 — the two halves of schema.sql must stay honest.

``schema.sql`` used to run in full on every connect. Steps 1/1b/1c established what that
actually did: its ``DO`` blocks probe ``information_schema`` and add what is missing, and the
tenant-isolation loop drops and recreates every policy. It was an **unledgered, self-healing
migration runner** — which is why a naive "only run it on an empty database" flip would have
silently stopped function and policy updates reaching existing databases.

So it is split instead of gated:

* ``schema_runtime.sql``   — every connect, as before. How updates propagate, how drift heals.
* ``schema_bootstrap.sql`` — only when the migration ledger is empty.

Equivalence was proven against a real Postgres, not assumed: bootstrap-then-runtime on one
database and ``schema.sql`` on another produce **4146 identical catalogue objects**. These
tests pin the properties that proof depends on, so a future edit cannot quietly break them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from split_schema import (  # noqa: E402
    build,
    classify,
    is_prerequisite,
    split_statements,
)

_SCHEMA = _ROOT / "nce" / "schema.sql"
_BOOTSTRAP = _ROOT / "nce" / "schema_bootstrap.sql"
_RUNTIME = _ROOT / "nce" / "schema_runtime.sql"


# ---------------------------------------------------------------------------
# The splitter must not lose SQL
# ---------------------------------------------------------------------------


def test_splitter_round_trips_schema_sql() -> None:
    """🔴 Everything downstream assumes no statement is dropped or mangled.

    A naive split on ``;`` shreds all 84 ``DO`` blocks. Rejoining must reproduce the file
    exactly, apart from trailing whitespace.
    """
    sql = _SCHEMA.read_text(encoding="utf-8", errors="replace")
    rejoined = "".join(split_statements(sql))
    assert rejoined.strip() == sql.strip()


def test_every_do_block_survives_whole() -> None:
    sql = _SCHEMA.read_text(encoding="utf-8", errors="replace")
    stmts = split_statements(sql)
    do_stmts = [s for s in stmts if re.search(r"\bDO\s*\$", s, re.I)]
    assert len(do_stmts) >= len(re.findall(r"DO\s*\$\$", sql))
    assert not [s for s in do_stmts if s.count("$$") % 2], "a DO block was cut in half"


# ---------------------------------------------------------------------------
# Classification — the two regressions this caught, pinned
# ---------------------------------------------------------------------------


def test_do_block_bodies_are_classified_not_ignored() -> None:
    """🔴 Regression: the first version blanked every ``$$`` body before classifying.

    For a FUNCTION the body is a definition; for a ``DO`` block **the body is the
    statement**. Blanking it left the classifier reading ``DO ;`` — nothing — so all 84
    fell through to the runtime default, including the ``kg_nodes`` backfill. That single
    side effect is the main reason this split exists, and the bug kept it running forever.
    """
    stmt = (
        "DO $$\nBEGIN\n"
        "  ALTER TABLE kg_nodes ADD COLUMN namespace_id UUID;\n"
        "  UPDATE kg_nodes SET namespace_id = '00000000-0000-0000-0000-000000000000';\n"
        "END\n$$;"
    )
    assert classify(stmt) == "bootstrap"


def test_function_bodies_are_ignored_when_classifying() -> None:
    """A function that CREATEs tables when called is still a runtime definition."""
    stmt = (
        "CREATE OR REPLACE FUNCTION nce_make_partition() RETURNS void AS $$\n"
        "BEGIN\n  CREATE TABLE IF NOT EXISTS part_x (id int);\nEND\n$$ LANGUAGE plpgsql;"
    )
    assert classify(stmt) == "runtime"


def test_drop_constraint_travels_with_its_add() -> None:
    """🔴 Regression: a drop/recreate pair split across halves executes in reverse.

    ``schema.sql`` uses ``DROP CONSTRAINT IF EXISTS x; ADD CONSTRAINT x ...``. The first
    version sent the DROP to runtime and the ADD to bootstrap, so bootstrap added the
    constraint and runtime then dropped it — 13 constraints and indexes silently absent,
    caught only by the catalogue comparison.
    """
    drop = "ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_natural_key;"
    add = "ALTER TABLE bom_line_content ADD CONSTRAINT bom_line_content_natural_key UNIQUE (namespace_id);"
    assert classify(drop) == classify(add) == "bootstrap"


def test_drop_policy_stays_with_create_policy_in_runtime() -> None:
    """The same pairing rule, the other way round."""
    assert classify("DROP POLICY IF EXISTS tenant_isolation_policy ON memories;") == "runtime"
    assert (
        classify(
            "CREATE POLICY tenant_isolation_policy ON memories FOR ALL TO nce_app USING (true);"
        )
        == "runtime"
    )


def test_extensions_are_prerequisites_so_bootstrap_can_use_their_types() -> None:
    """🔴 Regression: ``CREATE EXTENSION vector`` in runtime, ``CREATE TABLE memories``
    (vector columns) in bootstrap, bootstrap first → ``type "halfvec" does not exist``,
    64 errors and 894 objects missing. Prerequisites are emitted into BOTH halves."""
    assert is_prerequisite("CREATE EXTENSION IF NOT EXISTS vector;")
    assert not is_prerequisite("CREATE TABLE IF NOT EXISTS foo (id int);")


# ---------------------------------------------------------------------------
# The generated files must match the source, and carry the right content
# ---------------------------------------------------------------------------


def test_generated_halves_are_current() -> None:
    """🔴 The gate. ``schema.sql`` is the reviewable source; the halves are generated."""
    boot, run, _ = build()
    assert _BOOTSTRAP.exists() and _RUNTIME.exists(), "run scripts/split_schema.py --out-dir nce/"
    assert _BOOTSTRAP.read_text(encoding="utf-8").replace("\r\n", "\n") == boot.replace(
        "\r\n", "\n"
    ), (
        "schema_bootstrap.sql is stale — regenerate with "
        "python scripts/split_schema.py --out-dir nce/"
    )
    assert _RUNTIME.read_text(encoding="utf-8").replace("\r\n", "\n") == run.replace(
        "\r\n", "\n"
    ), "schema_runtime.sql is stale — regenerate with python scripts/split_schema.py --out-dir nce/"


@pytest.mark.parametrize(
    "needle,where,why",
    [
        (
            "SET namespace_id = global_ns_id",
            "bootstrap",
            "the tenant-assignment side effect must stop running on every connect",
        ),
        ("tenant_tables", "runtime", "the RLS loop is self-healing and must keep running"),
        (
            "CREATE OR REPLACE FUNCTION prevent_mutation",
            "runtime",
            "function updates must keep reaching existing databases",
        ),
        ("trg_event_log_worm", "runtime", "the WORM trigger must stay re-asserted"),
    ],
)
def test_critical_statements_land_in_the_right_half(needle: str, where: str, why: str) -> None:
    boot = _BOOTSTRAP.read_text(encoding="utf-8")
    run = _RUNTIME.read_text(encoding="utf-8")
    target, other = (boot, run) if where == "bootstrap" else (run, boot)
    assert needle in target, f"{needle!r} should be in {where}: {why}"
    if where == "bootstrap":
        # Comments mentioning it are fine; executable occurrences are not.
        executable = [
            ln for ln in other.splitlines() if needle in ln and not ln.strip().startswith("--")
        ]
        assert not executable, f"{needle!r} still executes in runtime: {why}"


def test_orchestrator_no_longer_executes_the_whole_schema() -> None:
    """🔴 The wiring check. The split is inert unless connect() actually uses it."""
    src = (_ROOT / "nce" / "orchestrator.py").read_text(encoding="utf-8")
    start = src.index("async def _init_pg_schema")
    body = src[start : src.index("\n    async def ", start + 10)]
    assert "schema_bootstrap.sql" in body
    assert "schema_runtime.sql" in body
    assert "applied_migrations" in body, "bootstrap must be gated on the migration ledger"
    assert 'here / "schema.sql"' not in body, "schema.sql must no longer be executed wholesale"
