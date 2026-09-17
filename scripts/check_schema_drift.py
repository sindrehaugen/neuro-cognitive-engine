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
This compares **tables and columns**. It does not compare types, defaults, nullability,
indexes, constraints, triggers or policies. A column present with the wrong type reads as
present here. That is a real gap and it is named on purpose — a checker that quietly
claims more coverage than it has is the failure mode this whole row is about.

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


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True, help="postgresql://... of the database to check")
    ap.add_argument("--schema", default=str(_SCHEMA), help="path to schema.sql")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    expected = expected_columns(Path(args.schema))
    actual = await actual_columns(args.dsn)
    missing_tables, missing_cols = compare(expected, actual)

    if args.json:
        print(
            json.dumps(
                {
                    "expected_tables": len(expected),
                    "missing_tables": missing_tables,
                    "missing_columns": missing_cols,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"schema.sql guarantees {len(expected)} tables")
        if not missing_tables and not missing_cols:
            print("NO DRIFT: the database has every table and column schema.sql guarantees.")
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
            print(
                "\nThis database would be changed by running schema.sql. Until Q-4 step 2 "
                "lands, connect() will apply these silently and without a ledger entry."
            )

    return 1 if (missing_tables or missing_cols) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
