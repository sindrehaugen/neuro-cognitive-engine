#!/usr/bin/env python3
"""Does a live database have everything ``nce/schema.sql`` guarantees? (Q-4, step 1)

Why this exists
---------------
``NCEEngine.connect()`` runs **two** schema writers on every connect
(``orchestrator.py:205-206``): the whole of ``nce/schema.sql``, and then the numbered
migration chain with its ``applied_migrations`` ledger.

It is tempting to describe ``schema.sql`` as a dead second writer whose changes never
reach a live database. That is false, and the charter row said it for two weeks. The
13 unguarded ``ADD COLUMN`` statements sit inside ``DO $$`` blocks that probe
``information_schema`` first and then backfill — ``schema.sql:249-256``::

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='kg_nodes' AND column_name='namespace_id') THEN
        ALTER TABLE kg_nodes ADD COLUMN namespace_id UUID;
    END IF;
    UPDATE kg_nodes SET namespace_id = global_ns_id WHERE namespace_id IS NULL;

So ``schema.sql`` **is** a migration runner. It is simply an *unledgered* one: measured
on the live database 2026-09-17, all ten columns that exist only in ``schema.sql`` were
present, while ``applied_migrations`` held 74 rows and had never heard of them.

That is the real defect. Not "two sources of truth" in the abstract, but: **nothing can
say what schema version a live database is actually at**, because one of the two writers
keeps no record.

What this script is for
-----------------------
Before ``schema.sql`` can safely become bootstrap-only (Q-4 step 2), something has to be
able to answer *"is this database missing anything schema.sql would have given it?"*
without running ``schema.sql`` to find out. That is this script.

It is deliberately **read-only**. It changes nothing; it reports.

Limits, stated rather than implied
----------------------------------
This compares **tables, columns, RLS enablement and policy presence**.

RLS coverage was added after the column-only version nearly let step 2 through unsafely.
Measured on ``f2dba69``: four tables have their tenant-isolation policy AND their
``ENABLE ROW LEVEL SECURITY`` **only** in ``schema.sql`` and in no migration --
``outbox_events``, ``replay_runs``, ``saga_execution_log``, ``topology_graph``. A
column-only checker reports those databases clean. If ``schema.sql`` stopped running with
a policy missing, the failure mode is a silent tenant-isolation hole, not a crash.

Still NOT compared: column types, defaults, nullability, indexes, constraints, triggers,
and the *predicate text* of a policy. Policy presence is checked by name; a policy that
exists with a weakened ``USING`` clause reads as present. That is a real gap and it is
named on purpose -- a checker that quietly claims more coverage than it has is the failure
mode this whole row is about.

Usage::

    python scripts/check_schema_drift.py --dsn postgresql://user@host/db
    python scripts/check_schema_drift.py --dsn "$DSN" --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = _REPO_ROOT / "nce" / "schema.sql"

# Objects that schema.sql references but never defines, or that are created outside the
# DDL path entirely. Each entry needs a reason; an unexplained skip is how a gap hides.
_NOT_EXPECTED: dict[str, str] = {}


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` blocks.

    Without this, a commented-out ``ALTER TABLE ... ADD COLUMN`` is parsed as a promise
    the file never makes, and the checker reports drift against a column that was
    deliberately retired.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _split_top_level_commas(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses.

    ``NUMERIC(12, 2)`` and ``CHECK (x IN ('a', 'b'))`` both contain commas that do not
    separate column definitions.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    in_str = False
    for ch in body:
        if ch == "'":
            in_str = not in_str
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


# A table-level constraint clause, not a column definition.
_TABLE_LEVEL = re.compile(
    r"^\s*(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT|EXCLUDE|LIKE)\b",
    re.I,
)


def _extract_create_table(sql: str) -> dict[str, set[str]]:
    """Map table -> columns, for every ``CREATE TABLE`` in the file."""
    out: dict[str, set[str]] = {}
    pattern = re.compile(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([\w.\"]+)\s*\(", re.I)
    for m in pattern.finditer(sql):
        table = m.group(1).strip('"').split(".")[-1].lower()
        # Walk to the matching close paren so nested parens do not end the body early.
        depth = 0
        start = m.end() - 1
        end = None
        in_str = False
        for i in range(start, len(sql)):
            ch = sql[i]
            if ch == "'":
                in_str = not in_str
            if in_str:
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        body = sql[start + 1 : end]
        cols = out.setdefault(table, set())
        for part in _split_top_level_commas(body):
            part = part.strip()
            if not part or _TABLE_LEVEL.match(part):
                continue
            name = re.match(r'^\s*"?([A-Za-z_][\w]*)"?\s', part + " ")
            if name:
                cols.add(name.group(1).lower())
    return out


def _extract_add_column(sql: str) -> set[tuple[str, str]]:
    """Every ``ALTER TABLE x ADD COLUMN y``, guarded or not, including inside DO blocks."""
    pattern = re.compile(
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)\s+"
        r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_][\w]*)\"?",
        re.I,
    )
    return {(t.strip('"').split(".")[-1].lower(), c.lower()) for t, c in pattern.findall(sql)}


def expected_columns(schema_path: Path = _SCHEMA) -> dict[str, set[str]]:
    """Everything ``schema.sql`` guarantees a database will have, as table -> columns."""
    sql = _strip_sql_comments(schema_path.read_text(encoding="utf-8", errors="replace"))
    tables = _extract_create_table(sql)
    for table, col in _extract_add_column(sql):
        # A column added to a table this file never creates is still a promise the file
        # makes; record the table so the drift shows up rather than being skipped.
        tables.setdefault(table, set()).add(col)
    return {t: c for t, c in tables.items() if t not in _NOT_EXPECTED}


async def actual_columns(dsn: str) -> dict[str, set[str]]:
    import asyncpg  # imported late so --help works without the driver

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
    finally:
        await conn.close()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["table_name"].lower(), set()).add(r["column_name"].lower())
    return out


def compare(
    expected: dict[str, set[str]], actual: dict[str, set[str]]
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (tables missing entirely, {table: columns missing})."""
    missing_tables = sorted(t for t in expected if t not in actual)
    missing_cols: dict[str, list[str]] = {}
    for table, cols in expected.items():
        if table not in actual:
            continue
        gap = sorted(cols - actual[table])
        if gap:
            missing_cols[table] = gap
    return missing_tables, missing_cols


def tenant_tables_loop(sql: str) -> set[str]:
    """The tables covered by ``schema.sql``'s dynamic tenant-isolation loop.

    Most of this estate's RLS is not written as static DDL. ``schema.sql`` declares a
    ``tenant_tables TEXT[] := ARRAY[...]`` literal and then, for each entry, executes::

        ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.%I FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_policy ON public.%I;
        CREATE POLICY tenant_isolation_policy ON public.%I
            FOR ALL TO nce_app
            USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
            WITH CHECK (...);
        REVOKE ALL ON TABLE public.%I FROM nce_app;

    It drops and *recreates* the policy on every connect, so any drift is silently
    repaired -- which is another reason nothing has ever noticed that ``schema.sql`` is a
    schema writer. It is also why Q-4 step 2 needs this checker: once the file stops
    running, that repair loop stops with it.

    A naive ``CREATE POLICY ... ON <table>`` regex captures ``%I`` here and yields a
    policy on an empty table name -- a permanent false positive. Expanding the array is
    both correct and far better coverage than skipping the loop.
    """
    m = re.search(r"tenant_tables\s+TEXT\[\]\s*:=\s*ARRAY\[(.*?)\];", sql, re.S | re.I)
    if not m:
        return set()
    return {name.lower() for name in re.findall(r"'(\w+)'", m.group(1))}


def expected_rls(schema_path: Path = _SCHEMA) -> tuple[set[str], set[tuple[str, str]]]:
    """What ``schema.sql`` guarantees about row-level security.

    Returns ``(tables with RLS enabled, {(table, policy_name)})``, combining the static
    ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` / ``CREATE POLICY`` statements with the
    dynamic loop expanded over its table array.
    """
    sql = _strip_sql_comments(schema_path.read_text(encoding="utf-8", errors="replace"))
    enabled = {
        m.group(1).split(".")[-1].strip('"').lower()
        for m in re.finditer(
            r'ALTER\s+TABLE\s+"?([\w.]+)"?\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY', sql, re.I
        )
    }
    policies = {
        (m.group(2).split(".")[-1].strip('"').lower(), m.group(1).strip('"').lower())
        for m in re.finditer(r'CREATE\s+POLICY\s+"?(\w+)"?\s+ON\s+"?([\w.]+)"?', sql, re.I)
        # ``%I`` is a format placeholder, not a table; the loop is expanded below.
        if m.group(2).split(".")[-1].strip('"')
    }
    looped = tenant_tables_loop(sql)
    enabled |= looped
    policies |= {(t, "tenant_isolation_policy") for t in looped}
    return enabled, policies


async def actual_rls(dsn: str) -> tuple[set[str], set[tuple[str, str]]]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        # relkind IN ('r','p') — 'p' is a PARTITIONED table, and on this estate the nine
        # most important tenant tables are partitioned: memories, kg_nodes, kg_edges,
        # event_log, contradictions, pii_redactions, memory_salience, memory_embeddings,
        # embedding_aspects. Filtering to 'r' alone made every one of them invisible, so
        # the checker reported "no RLS" for tables that had it, and would equally have
        # reported nothing had they lost it.
        rows = await conn.fetch(
            "SELECT c.relname AS tbl, c.relrowsecurity AS enabled "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        )
        pols = await conn.fetch(
            "SELECT c.relname AS tbl, p.polname AS pol "
            "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
        )
    finally:
        await conn.close()
    enabled = {r["tbl"].lower() for r in rows if r["enabled"]}
    policies = {(r["tbl"].lower(), r["pol"].lower()) for r in pols}
    return enabled, policies


async def rls_role_posture(dsn: str) -> str | None:
    """Does the connecting role actually obey the policies this script just verified?

    This calls ``nce.master_key_registry.rls_visibility_problem``, which the F11 ratchet
    requires of anything working in this area. It is used here as a **report, not a
    refusal**, and the difference is deliberate:

    ``rewrap_master_key.py`` refuses, because it reads wrapped *data rows* with no namespace
    context -- a NOBYPASSRLS role sees zero of them and reports success while blobs stay
    wrapped under the old key. This script reads only ``information_schema`` and the ``pg_*``
    catalogues, which are not RLS-filtered, so its answer is correct for any role and
    refusing would be cargo-cult.

    But this script has the same failure mode in another form, and that is why the call is
    worth making. "0 missing policies" reads as "tenant isolation works". On this deployment
    it does not: ``mcp_user`` is ``rolsuper``/``rolbypassrls``, so every policy verified above
    exists and constrains nothing. A verdict that looks reassuring while the mechanism is
    inert is precisely the thing the guard was written about.
    """
    import asyncpg

    # Run as ``python scripts/check_schema_drift.py`` — which is how CI invokes it —
    # ``sys.path[0]`` is ``scripts/``, not the repo root, so ``nce`` is not importable.
    # An import probe run from the repo root passes and hides this; only an end-to-end
    # run catches it.
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from nce.master_key_registry import rls_visibility_problem

    conn = await asyncpg.connect(dsn)
    try:
        return await rls_visibility_problem(conn)
    finally:
        await conn.close()


def compare_rls(
    expected: tuple[set[str], set[tuple[str, str]]],
    actual: tuple[set[str], set[tuple[str, str]]],
) -> tuple[list[str], list[str]]:
    """Return (tables missing RLS enablement, missing "table.policy" names).

    Only one direction is reported. A table with RLS enabled that ``schema.sql`` does not
    mention is almost always a migration doing its job, and flagging it would make the
    checker useless on every real deployment -- the same reasoning as extra columns.
    """
    exp_enabled, exp_pols = expected
    act_enabled, act_pols = actual
    missing_rls = sorted(exp_enabled - act_enabled)
    missing_pols = sorted(f"{t}.{p}" for t, p in (exp_pols - act_pols))
    return missing_rls, missing_pols


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True, help="postgresql://... of the database to check")
    ap.add_argument("--schema", default=str(_SCHEMA), help="path to schema.sql")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    expected = expected_columns(Path(args.schema))
    actual = await actual_columns(args.dsn)
    missing_tables, missing_cols = compare(expected, actual)
    missing_rls, missing_pols = compare_rls(
        expected_rls(Path(args.schema)), await actual_rls(args.dsn)
    )
    posture = await rls_role_posture(args.dsn)

    if args.json:
        print(
            json.dumps(
                {
                    "expected_tables": len(expected),
                    "missing_tables": missing_tables,
                    "missing_columns": missing_cols,
                    "missing_rls_enablement": missing_rls,
                    "missing_policies": missing_pols,
                    "rls_role_posture": posture,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"schema.sql guarantees {len(expected)} tables")
        if not (missing_tables or missing_cols or missing_rls or missing_pols):
            print(
                "NO DRIFT: the database has every table, column, RLS enablement and policy "
                "schema.sql guarantees."
            )
        else:
            if missing_tables:
                print(f"\nMISSING TABLES ({len(missing_tables)}):")
                for t in missing_tables:
                    print(f"  {t}")
            if missing_cols:
                total = sum(len(v) for v in missing_cols.values())
                print(f"\nMISSING COLUMNS ({total} across {len(missing_cols)} tables):")
                for t, cols in sorted(missing_cols.items()):
                    print(f"  {t}: {', '.join(cols)}")
            if missing_rls:
                print(f"\nTABLES MISSING RLS ENABLEMENT ({len(missing_rls)}):")
                for t in missing_rls:
                    print(f"  {t}")
            if missing_pols:
                print(f"\nMISSING POLICIES ({len(missing_pols)}):")
                for name in missing_pols:
                    print(f"  {name}")
            print(
                "\nThis database would be changed by running schema.sql. Until Q-4 step 2 "
                "lands, connect() will apply these silently and without a ledger entry."
            )
            if missing_rls or missing_pols:
                print(
                    "A missing policy is not a crash. It is one tenant reading another "
                    "tenant's rows, quietly."
                )

    if posture and not args.json:
        # Not a failure: the policies are present and correct, which is what this script
        # checks. But "0 missing policies" must not be read as "tenant isolation is in
        # force" when the connecting role bypasses all of it.
        print(f"\nNOTE on what the RLS result means here: {posture}")

    return 1 if (missing_tables or missing_cols or missing_rls or missing_pols) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
