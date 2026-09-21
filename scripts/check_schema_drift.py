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

Indexes, constraints, triggers and functions were added after measuring on ``1dda962``::

    INDEX       schema.sql=185  migrations=152  only in schema.sql=47
    TRIGGER     schema.sql=  6  migrations=  5  only in schema.sql= 1
    FUNCTION    schema.sql=  7  migrations=  4  only in schema.sql= 3
    CONSTRAINT  schema.sql= 23  migrations= 22  only in schema.sql= 9

The one trigger is ``trg_event_log_worm`` and the functions include ``prevent_mutation``:
together, the WORM append-only guarantee. Migration 080 *calls* ``prevent_mutation()`` and
does not create it, so applying the chain to a database that never ran ``schema.sql`` fails
with *"function prevent_mutation() does not exist"* -- proven on a throwaway database. The
migration chain has never been a standalone path, and that is worth knowing before anyone
reasons about step 2 from the file names alone.

Still NOT compared: column types, defaults, nullability, index and constraint
*definitions*, function *bodies*, and policy *predicate text*. Everything beyond
tables/columns is matched **by name**. A policy with a weakened ``USING`` clause, or a
function whose body changed, reads as present. That last one matters for step 2:
``CREATE OR REPLACE FUNCTION`` in ``schema.sql`` is how function updates currently reach
existing databases, and this checker would not notice if they stopped. Named on purpose --
a checker that quietly claims more coverage than it has is the failure mode this whole row
is about.

(``expected_column_nullability()`` below extracts nullability from the same static text,
but for a different consumer, ``gen_openapi.py`` -- this checker's own drift comparison
above still never reads it and still does not compare nullability between schema.sql and a
live database. The two are not the same claim: one is "what does schema.sql itself say",
the other would be "does a live database match schema.sql", and only the first exists.)

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


# ---------------------------------------------------------------------------
# Nullability -- NOT part of this script's own drift comparison (see the
# module docstring's "Still NOT compared" list; that stays true, this
# checker never reads the functions below). Added for a different consumer,
# gen_openapi.py, which needs to know whether a documented response field
# can be `null` and has no other static source for that. Extracted from the
# exact same CREATE TABLE walk `_extract_create_table` already does --
# `part` (the full column definition) is already in hand there and thrown
# away down to just the name; this keeps it instead of a second, duplicate
# parser.
# ---------------------------------------------------------------------------


def _strip_parens(text: str) -> str:
    """Remove every parenthesized group, nesting-aware. A bare column-level
    ``NOT NULL`` always sits outside any parens (Postgres column-constraint
    grammar: ``name type [NOT NULL] [DEFAULT ...] [CHECK (...)] [REFERENCES
    ...(...)]``) -- stripping parens first means a `NOT NULL` appearing
    *inside* an inline `CHECK (col IS NOT NULL OR other IS NOT NULL)` or a
    typed precision like `NUMERIC(12, 2)` is never mistaken for the
    column's own constraint. No such inline CHECK exists in `schema.sql`
    today (checked: every `IS NOT NULL` on a `CHECK`-shaped line is inside
    a `CREATE POLICY ... WITH CHECK (...)`, never a column definition), but
    stripping unconditionally means this doesn't silently start lying the
    day one is added.
    """
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.I)


def _is_not_null(column_definition: str) -> bool:
    """True if this column's own definition text declares ``NOT NULL`` as a
    bare column constraint (not inside a type's precision or an inline
    CHECK's parens -- see :func:`_strip_parens`)."""
    return bool(_NOT_NULL_RE.search(_strip_parens(column_definition)))


def _extract_create_table_defs(sql: str) -> dict[str, dict[str, str]]:
    """Same walk as :func:`_extract_create_table`, keeping each column's
    full definition text instead of discarding it down to just the name."""
    out: dict[str, dict[str, str]] = {}
    pattern = re.compile(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([\w.\"]+)\s*\(", re.I)
    for m in pattern.finditer(sql):
        table = m.group(1).strip('"').split(".")[-1].lower()
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
        cols = out.setdefault(table, {})
        for part in _split_top_level_commas(body):
            part = part.strip()
            if not part or _TABLE_LEVEL.match(part):
                continue
            name = re.match(r'^\s*"?([A-Za-z_][\w]*)"?\s', part + " ")
            if name:
                cols[name.group(1).lower()] = part
    return out


def _extract_add_column_defs(sql: str) -> dict[tuple[str, str], str]:
    """Same match as :func:`_extract_add_column`, keeping the rest of the
    statement (up to the terminating ``;``) instead of discarding it.

    Assumes the statement's own ``;`` is not inside a quoted string -- true
    for every ``ADD COLUMN`` in `schema.sql` today (checked directly: none
    of their ``DEFAULT`` clauses contain a literal semicolon), stated
    rather than silently relied on.
    """
    pattern = re.compile(
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)\s+"
        r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_][\w]*)\"?"
        r"([^;]*);",
        re.I,
    )
    return {
        (t.strip('"').split(".")[-1].lower(), c.lower()): rest
        for t, c, rest in pattern.findall(sql)
    }


def expected_column_nullability(schema_path: Path = _SCHEMA) -> dict[str, dict[str, bool]]:
    """Everything ``schema.sql`` says about whether a column can be
    ``NULL``, as table -> {column: is_nullable}. A column is nullable
    unless its own definition (or, for an ``ADD COLUMN``, the ``ADD
    COLUMN`` statement itself) declares ``NOT NULL``.

    Same source, same static read, same table-name normalisation as
    :func:`expected_columns` -- this does not replace it or change its
    behaviour, it answers a question that checker was never asked (see the
    module docstring's "Still NOT compared" list, and the section comment
    above this function).
    """
    sql = _strip_sql_comments(schema_path.read_text(encoding="utf-8", errors="replace"))
    tables = _extract_create_table_defs(sql)
    result: dict[str, dict[str, bool]] = {
        table: {col: not _is_not_null(defn) for col, defn in cols.items()}
        for table, cols in tables.items()
    }
    for (table, col), rest in _extract_add_column_defs(sql).items():
        result.setdefault(table, {})[col] = not _is_not_null(rest)
    return {t: c for t, c in result.items() if t not in _NOT_EXPECTED}


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


# Object kinds compared beyond tables/columns. Each maps a ``schema.sql`` pattern to the
# catalogue query that answers "does the database have it?". Names only -- see the module
# docstring for what that does and does not prove.
_OBJECT_PATTERNS: dict[str, str] = {
    "index": r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?'
    r'(?:IF\s+NOT\s+EXISTS\s+)?"?([\w.]+)"?',
    "trigger": r'CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+"?([\w.]+)"?',
    "function": r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"?([\w.]+)"?\s*\(',
    "constraint": r'ADD\s+CONSTRAINT\s+"?([\w.]+)"?',
}


def expected_objects(schema_path: Path = _SCHEMA) -> dict[str, set[str]]:
    """Indexes, triggers, functions and constraints ``schema.sql`` guarantees, by kind.

    Measured on ``1dda962`` -- this is why the checker needed to grow again::

        INDEX       schema.sql=185  migrations=152  only in schema.sql=47
        TRIGGER     schema.sql=  6  migrations=  5  only in schema.sql= 1
        FUNCTION    schema.sql=  7  migrations=  4  only in schema.sql= 3
        CONSTRAINT  schema.sql= 23  migrations= 22  only in schema.sql= 9

    The single trigger is ``trg_event_log_worm`` and the functions include
    ``prevent_mutation`` -- together, the WORM append-only guarantee. Migration 080 calls
    ``prevent_mutation()`` and does not create it; applying 080 to a database that never
    ran ``schema.sql`` fails with *"function prevent_mutation() does not exist"*, proven on
    a throwaway database. The migration chain has never been a standalone path.
    """
    sql = _strip_sql_comments(schema_path.read_text(encoding="utf-8", errors="replace"))
    return {
        kind: {m.group(1).strip('"').split(".")[-1].lower() for m in re.finditer(pat, sql, re.I)}
        for kind, pat in _OBJECT_PATTERNS.items()
    }


async def actual_objects(dsn: str) -> dict[str, set[str]]:
    import asyncpg

    queries = {
        "index": "SELECT indexname AS n FROM pg_indexes WHERE schemaname = 'public'",
        "trigger": "SELECT tgname AS n FROM pg_trigger WHERE NOT tgisinternal",
        "function": "SELECT p.proname AS n FROM pg_proc p JOIN pg_namespace ns "
        "ON ns.oid = p.pronamespace WHERE ns.nspname = 'public'",
        "constraint": "SELECT conname AS n FROM pg_constraint c JOIN pg_namespace ns "
        "ON ns.oid = c.connamespace WHERE ns.nspname = 'public'",
    }
    conn = await asyncpg.connect(dsn)
    try:
        return {kind: {r["n"].lower() for r in await conn.fetch(q)} for kind, q in queries.items()}
    finally:
        await conn.close()


def compare_objects(
    expected: dict[str, set[str]], actual: dict[str, set[str]]
) -> dict[str, list[str]]:
    """Return ``{kind: [missing names]}``, one direction only.

    Extra objects are not drift: migrations add indexes and constraints ``schema.sql``
    never mentions, and reporting those would make the checker useless on every real
    deployment -- the same reasoning as extra columns and extra policies.
    """
    out: dict[str, list[str]] = {}
    for kind, names in expected.items():
        gap = sorted(names - actual.get(kind, set()))
        if gap:
            out[kind] = gap
    return out


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
    missing_objs = compare_objects(
        expected_objects(Path(args.schema)), await actual_objects(args.dsn)
    )

    if args.json:
        print(
            json.dumps(
                {
                    "expected_tables": len(expected),
                    "missing_tables": missing_tables,
                    "missing_columns": missing_cols,
                    "missing_rls_enablement": missing_rls,
                    "missing_policies": missing_pols,
                    "missing_objects": missing_objs,
                    "rls_role_posture": posture,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"schema.sql guarantees {len(expected)} tables")
        if not (missing_tables or missing_cols or missing_rls or missing_pols or missing_objs):
            print(
                "NO DRIFT: the database has every table, column, RLS enablement, policy, "
                "index, constraint, trigger and function schema.sql guarantees."
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
            for kind, names in sorted(missing_objs.items()):
                plural = "INDEXES" if kind == "index" else f"{kind.upper()}S"
                print(f"\nMISSING {plural} ({len(names)}):")
                for name in names:
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

    return (
        1 if (missing_tables or missing_cols or missing_rls or missing_pols or missing_objs) else 0
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
