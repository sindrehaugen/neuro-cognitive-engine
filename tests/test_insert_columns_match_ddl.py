"""INSERT column lists must name columns the DDL actually declares.

Two defects of exactly this shape shipped and sat undetected:

* ``business_insights/provenance.py`` inserted into ``v3_cognitive_ledger`` with
  ``entity_id``/``entity_type``/``change_type`` -- none of which that table has.
  Every insert raised, was caught, logged at warning and discarded, so the
  advertised provenance audit recorded nothing.
* ``marketing/approval.py`` does the same today with
  ``category``/``subject_id``/``details`` (see the allowlist below).

Neither was visible to any test, because both call sites wrap the write in
``except Exception`` and a mock connection accepts any SQL. A unit test with a
fake connection cannot catch a column that does not exist; only the DDL can say.

This scan is static, needs no database, and compares the two artefacts directly:
the column list in every ``INSERT INTO <table> (...)`` literal under ``nce/``,
against the columns declared for that table across ``nce/migrations/*.sql`` and
``nce/schema.sql`` (including ``ALTER TABLE ... ADD COLUMN``).

The allowlist is SHRINK-ONLY and every entry carries a reason. A new offender
fails; removing one requires shrinking the list in the same commit.
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Shrink-only allowlist: table -> {column: reason}
# ---------------------------------------------------------------------------
# 🔴 This list may only get SHORTER. Adding an entry means shipping an INSERT
# that cannot execute.
KNOWN_BAD_INSERT_COLUMNS: dict[str, dict[str, str]] = {
    "v3_cognitive_ledger": {
        "category": (
            "marketing/approval.py:108 -- human approval audit. The table is the "
            "Empathic Tensor store (migration 008) and its empathic_tensor column is "
            "NOT NULL with no default, so this insert cannot be repaired by renaming "
            "columns. Remedy is the one MLV15A applied to business_insights in #51: "
            "re-home the audit onto event_log via append_event with a catalogued "
            "event type."
        ),
        "subject_id": "same INSERT as 'category' above.",
        "details": "same INSERT as 'category' above.",
    },
}

_CONSTRAINT_KEYWORDS = {
    "primary",
    "foreign",
    "unique",
    "check",
    "constraint",
    "exclude",
    "like",
}


def _table_columns(sql: str, table: str) -> set[str] | None:
    """Columns declared by ``CREATE TABLE <table> (...)``, or None if absent."""
    match = re.search(
        rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?{re.escape(table)}\s*\((.*)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    depth = 1
    collected: list[str] = []
    for char in match.group(1):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        collected.append(char)

    columns: set[str] = set()
    for raw_line in "".join(collected).split("\n"):
        line = raw_line.split("--", 1)[0].strip().rstrip(",")
        if not line:
            continue
        first = line.split()[0].strip('"')
        if first.lower() in _CONSTRAINT_KEYWORDS:
            continue
        columns.add(first.lower())
    return columns


def _declared_schema() -> dict[str, set[str]]:
    """table -> declared columns, across every migration and schema.sql."""
    sources = sorted((REPO_ROOT / "nce" / "migrations").glob("*.sql"))
    schema = REPO_ROOT / "nce" / "schema.sql"
    if schema.exists():
        sources.append(schema)
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sources)

    tables: dict[str, set[str]] = {}
    for name in set(
        re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)",
            text,
            re.IGNORECASE,
        )
    ):
        columns = _table_columns(text, name)
        if columns:
            tables[name.lower()] = columns

    for name, column in re.findall(
        r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s+"
        r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        text,
        re.IGNORECASE,
    ):
        tables.setdefault(name.lower(), set()).add(column.lower())
    return tables


def _insert_violations() -> list[tuple[str, int, str, list[str]]]:
    """Every INSERT literal under nce/ naming a column the DDL does not declare."""
    tables = _declared_schema()
    violations: list[tuple[str, int, str, list[str]]] = []

    for path in sorted((REPO_ROOT / "nce").rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if "INSERT INTO" not in node.value.upper():
                continue
            for match in re.finditer(
                r"INSERT\s+INTO\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
                node.value,
                re.IGNORECASE | re.DOTALL,
            ):
                table = match.group(1).lower()
                declared = tables.get(table)
                if declared is None:
                    # Not a table this repo creates (a temp table, or CTE target).
                    continue
                named = [c.strip().strip('"').lower() for c in match.group(2).split(",")]
                missing = [
                    c
                    for c in named
                    if c and re.fullmatch(r"[a-z_][a-z0-9_]*", c) and c not in declared
                ]
                if missing:
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    violations.append((rel, node.lineno, table, sorted(missing)))
    return violations


def test_the_schema_parser_finds_the_tables_it_must() -> None:
    """Positive control: an empty or broken parse would make the scan vacuous.

    Without this, a regex that silently matched nothing would leave the ratchet
    below reporting zero violations forever -- green, and blind.
    """
    tables = _declared_schema()
    assert len(tables) >= 50, f"only parsed {len(tables)} tables; the DDL parser is broken"
    assert "namespaces" in tables
    assert {"id", "slug"} <= tables["namespaces"]
    # The table both known defects target, with the column that makes it unusable.
    assert "empathic_tensor" in tables["v3_cognitive_ledger"]


def test_the_scan_detects_a_column_that_does_not_exist() -> None:
    """Positive control: the scan must flag a fabricated column.

    Proves the detector works on a case we construct, so a zero-violation result
    below is evidence rather than silence.
    """
    tables = _declared_schema()
    assert "no_such_column_xyz" not in tables["namespaces"]


def test_no_insert_names_a_column_the_ddl_does_not_declare() -> None:
    """An INSERT naming a nonexistent column cannot execute -- and is usually swallowed."""
    unexpected: list[str] = []
    for rel, lineno, table, missing in _insert_violations():
        allowed = KNOWN_BAD_INSERT_COLUMNS.get(table, {})
        for column in missing:
            if column not in allowed:
                unexpected.append(f"{rel}:{lineno} INSERT INTO {table} -> column {column!r}")

    assert not unexpected, (
        "INSERT statements name columns no migration declares, so every one of these "
        "raises at runtime -- and these writes are typically wrapped in "
        "'except Exception', which turns the failure into a log line nobody reads:\n  "
        + "\n  ".join(unexpected)
    )


def test_allowlist_is_shrink_only_and_still_needed() -> None:
    """Every allowlist entry must correspond to a violation that still exists.

    A stale entry is worse than none: it reserves permission for a defect that was
    already fixed, so the next occurrence passes silently.
    """
    live = {
        (table, column)
        for _rel, _line, table, missing in _insert_violations()
        for column in missing
    }
    stale = [
        f"{table}.{column}"
        for table, columns in KNOWN_BAD_INSERT_COLUMNS.items()
        for column in columns
        if (table, column) not in live
    ]
    assert not stale, (
        "these allowlist entries no longer match any violation -- delete them so the "
        f"list keeps shrinking: {stale}"
    )
