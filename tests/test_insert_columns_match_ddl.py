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

Extended to ``UPDATE ... SET`` and single-table ``SELECT`` after the same shape
turned up five more times -- including two admin writes that could never succeed
(``trust_dial.py`` and ``admin_handlers/d365.py`` both did
``UPDATE namespaces SET ..., updated_at = NOW()``, and ``namespaces`` has no
``updated_at`` column) and ``resources/watcher.py`` selecting
``name``/``metadata``/``email`` off a table that declares
``display_name``/``attrs``/``ref_id``.

This scan is static, needs no database, and compares the two artefacts directly:
the column references in every ``INSERT INTO <table> (...)``, ``UPDATE <table>
SET col = ...`` and bare single-table ``SELECT a, b FROM <table>`` literal under
``nce/``, against the columns declared for that table across
``nce/migrations/*.sql`` and ``nce/schema.sql`` (including
``ALTER TABLE ... ADD COLUMN``).

The SELECT scan is deliberately NARROW -- it skips anything with a join, an
alias, a star, a cast or a JSONB operator -- because a noisy instrument gets
allowlisted into uselessness. A narrow check that is always right is worth more
than a broad one nobody trusts.

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

# Shrink-only, same contract as the INSERT allowlist above.
KNOWN_BAD_QUERY_COLUMNS: dict[str, dict[str, str]] = {
    "product_catalog": {
        "successor_sku": (
            "product/watchers.py:291 -- queries successor_sku and lifecycle_confidence. "
            "The table lacks both columns; the read is guarded at runtime by an "
            "information_schema.columns check pending the Charter §13 Item 7 storage decision."
        ),
        "lifecycle_confidence": "same query as 'successor_sku' above.",
    },
    "v3_cognitive_ledger": {
        "valid_to": (
            "database/pruning.py:449 -- consistency check checks valid_to IS NOT NULL "
            "on v3_cognitive_ledger. v3_cognitive_ledger (migration 008) is the Empathic "
            "Tensor store and has no valid_to column."
        ),
    },
}

_SQL_KEYWORDS = {
    # Aggregates & Math
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "abs",
    "round",
    "ceil",
    "floor",
    "greatest",
    "least",
    # Strings & Formatting
    "coalesce",
    "nullif",
    "btrim",
    "trim",
    "ltrim",
    "rtrim",
    "lower",
    "upper",
    "length",
    "substr",
    "substring",
    "starts_with",
    "concat",
    "concat_ws",
    "format",
    # Full-Text Search
    "to_tsvector",
    "to_tsquery",
    "plainto_tsquery",
    "phraseto_tsquery",
    "websearch_to_tsquery",
    "ts_rank",
    "ts_rank_cd",
    "similarity",
    "set_limit",
    # JSON / JSONB
    "jsonb_build_array",
    "jsonb_build_object",
    "json_build_array",
    "json_build_object",
    "jsonb_array_elements",
    "jsonb_array_elements_text",
    "jsonb_extract_path",
    "jsonb_extract_path_text",
    "json_array_elements",
    "json_array_elements_text",
    "jsonb_agg",
    "json_agg",
    "jsonb_strip_nulls",
    # Arrays & Ranges
    "array",
    "unnest",
    "string_agg",
    "array_agg",
    "array_to_string",
    "string_to_array",
    "tstzrange",
    "daterange",
    "tsrange",
    "numrange",
    "int4range",
    "int8range",
    # Date / Time
    "now",
    "date_trunc",
    "timezone",
    "extract",
    "epoch",
    "current_date",
    "current_time",
    "current_timestamp",
    "localtime",
    "localtimestamp",
    # UUID & Crypto
    "gen_random_uuid",
    "uuid_generate_v4",
    "digest",
    "hmac",
    "crypt",
    # Postgres internals & Metadata
    "current_setting",
    "set_config",
    "information_schema",
    "columns",
    "table_name",
    "column_name",
    # SQL types
    "text",
    "uuid",
    "int",
    "integer",
    "bigint",
    "smallint",
    "float",
    "numeric",
    "real",
    "double",
    "precision",
    "boolean",
    "bool",
    "jsonb",
    "json",
    "timestamptz",
    "timestamp",
    "date",
    "time",
    "bytea",
    "varchar",
    "char",
    "vector",
    "interval",
    # Standard SQL Keywords
    "select",
    "distinct",
    "from",
    "where",
    "and",
    "or",
    "not",
    "is",
    "in",
    "like",
    "ilike",
    "between",
    "case",
    "when",
    "then",
    "else",
    "end",
    "order",
    "by",
    "asc",
    "desc",
    "nulls",
    "first",
    "last",
    "limit",
    "offset",
    "group",
    "having",
    "as",
    "join",
    "left",
    "right",
    "inner",
    "outer",
    "cross",
    "on",
    "using",
    "union",
    "all",
    "intersect",
    "except",
    "exists",
    "any",
    "some",
    "filter",
    "over",
    "partition",
    "window",
    "row_number",
    "rank",
    "dense_rank",
    "lateral",
    "for",
    "skip",
    "locked",
    "nowait",
    "share",
    "update",
    "set",
    "values",
    "into",
    "insert",
    "delete",
    "returning",
    "conflict",
    "do",
    "nothing",
    "default",
    "with",
    "cast",
    "collate",
    "escape",
    "similar",
    "to",
    "true",
    "false",
    "null",
    "unknown",
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


def _strip_subqueries(sql: str) -> str:
    """Replace all (SELECT ...) subqueries with (1), respecting nested parentheses."""
    result: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] == "(":
            rest = sql[i + 1 :]
            if re.match(r"^\s*SELECT\b", rest, re.I):
                depth = 1
                j = i + 1
                in_quote = False
                while j < n and depth > 0:
                    ch = sql[j]
                    if ch == "'" and (j == 0 or sql[j - 1] != "\\"):
                        in_quote = not in_quote
                    elif not in_quote:
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                    j += 1
                result.append(" (1) ")
                i = j
                continue
        result.append(sql[i])
        i += 1
    return "".join(result)


def _clean_sql_expression(expr: str) -> str:
    """Strip literals, query parameters, typecasts, JSONB keys, and table qualifiers."""
    # 1. Strip string literals: '...'
    cleaned = re.sub(r"'(?:''|[^'])*'", " ", expr)
    # 2. Strip parameter placeholders: $1, %s, %(foo)s
    cleaned = re.sub(r"\$[0-9]+", " ", cleaned)
    cleaned = re.sub(r"%\([A-Za-z0-9_]+\)s", " ", cleaned)
    cleaned = re.sub(r"%[sdefg]", " ", cleaned)
    # 3. Strip typecasts: ::type or ::type[]
    cleaned = re.sub(r"::\s*[a-zA-Z_][a-zA-Z0-9_]*(?:\[\s*\])?", " ", cleaned)
    # 4. Strip JSONB arrow access operators: ->>'foo', ->'foo', #>'...', #>>'...'
    cleaned = re.sub(r"->>?\s*[A-Za-z0-9_]+", " ", cleaned)
    cleaned = re.sub(r"#>>?\s*[A-Za-z0-9_]+", " ", cleaned)
    # 5. Strip numeric literals
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\b", " ", cleaned)
    # 6. Strip table/alias prefix: e.g. "assets.lifecycle_state" -> "lifecycle_state"
    cleaned = re.sub(r"\b[a-zA-Z_][a-zA-Z0-9_]*\.([a-zA-Z_][a-zA-Z0-9_]*)\b", r"\1", cleaned)
    return cleaned


def _extract_column_candidates(expr: str) -> set[str]:
    cleaned = _clean_sql_expression(expr)
    tokens = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", cleaned)
    candidates: set[str] = set()
    for tok in tokens:
        low = tok.lower()
        if low not in _SQL_KEYWORDS:
            candidates.add(low)
    return candidates


def _is_docstring(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> bool:
    parent = parent_map.get(node)
    if isinstance(parent, ast.Expr):
        grandparent = parent_map.get(parent)
        if isinstance(
            grandparent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if grandparent.body and grandparent.body[0] is parent:
                return True
    return False


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


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
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", first.lower()):
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

        parent_map = _build_parent_map(tree)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if _is_docstring(node, parent_map):
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


def _update_violations() -> list[tuple[str, int, str, list[str]]]:
    """``UPDATE <table> SET col = ...`` naming a column the DDL does not declare."""
    tables = _declared_schema()
    out: list[tuple[str, int, str, list[str]]] = []
    for path in sorted((REPO_ROOT / "nce").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        parent_map = _build_parent_map(tree)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if _is_docstring(node, parent_map):
                continue
            if "UPDATE" not in node.value.upper():
                continue
            clean = _strip_subqueries(node.value)
            for m in re.finditer(
                r"UPDATE\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s+SET\s+"
                r"(.*?)(?:\s+WHERE\s+(.*?))?(?:\s+RETURNING|\)|$)",
                clean,
                re.IGNORECASE | re.DOTALL,
            ):
                table = m.group(1).lower()
                declared = tables.get(table)
                if declared is None:
                    continue
                set_part = m.group(2)
                where_part = m.group(3) or ""
                missing = set()
                for assign in re.finditer(r"(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", set_part):
                    col = assign.group(1).lower()
                    if col not in declared:
                        missing.add(col)
                if where_part:
                    where_c = _extract_column_candidates(where_part)
                    missing.update(where_c - declared)
                if missing:
                    out.append(
                        (
                            path.relative_to(REPO_ROOT).as_posix(),
                            node.lineno,
                            table,
                            sorted(missing),
                        )
                    )
    return out


def _extract_select_violations_from_sql(
    query: str, tables: dict[str, set[str]]
) -> list[tuple[str, list[str]]]:
    """Extract undeclared columns in single-table SELECT projections and WHERE clauses from a SQL string."""
    clean_query = _strip_subqueries(query)
    segments = re.split(r"\b(?:UNION|INTERSECT|EXCEPT)(?:\s+ALL)?\b", clean_query, flags=re.I)
    out: list[tuple[str, list[str]]] = []
    for segment in segments:
        if "SELECT" not in segment.upper() or re.search(r"\bJOIN\b", segment, re.I):
            continue
        if re.search(r"\bWITH\b", segment, re.I):
            continue
        for m in re.finditer(
            r"SELECT\s+(.*?)\s+FROM\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s+WHERE\s+(.*?))?"
            r"(?=\s+(?:ORDER\s+BY|GROUP\s+BY|LIMIT|FOR\s+UPDATE|FOR\s+SHARE|FOR\s+NO\s+KEY\s+UPDATE|FOR\s+KEY\s+SHARE|ON\s+CONFLICT|RETURNING)|\)|$)",
            segment,
            re.IGNORECASE | re.DOTALL,
        ):
            sel_part = m.group(1)
            table = m.group(2).lower()
            where_part = m.group(3) or ""
            declared = tables.get(table)
            if declared is None:
                continue
            candidates: set[str] = set()
            for raw in sel_part.split(","):
                item = re.sub(r"^\s*DISTINCT\s+", "", raw, flags=re.I).strip()
                parts = re.split(r"\s+AS\s+", item, flags=re.I)
                col_expr = parts[0].strip()
                if "*" in col_expr:
                    continue
                candidates.update(_extract_column_candidates(col_expr))
            if where_part:
                candidates.update(_extract_column_candidates(where_part))
            missing = candidates - declared
            if missing:
                out.append((table, sorted(missing)))
    return out


def _select_violations() -> list[tuple[str, int, str, list[str]]]:
    """Single-table SELECT statements naming undeclared columns in SELECT, WHERE, or ORDER/GROUP clauses.

    Skips joins, subqueries, and CTEs to ensure every reported violation is a real one.
    """
    tables = _declared_schema()
    out: list[tuple[str, int, str, list[str]]] = []
    for path in sorted((REPO_ROOT / "nce").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        parent_map = _build_parent_map(tree)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if _is_docstring(node, parent_map):
                continue
            for table, missing in _extract_select_violations_from_sql(node.value, tables):
                out.append(
                    (
                        path.relative_to(REPO_ROOT).as_posix(),
                        node.lineno,
                        table,
                        missing,
                    )
                )
    return out


def test_no_update_sets_a_column_the_ddl_does_not_declare() -> None:
    """``UPDATE ... SET missing_col = x`` raises UndefinedColumnError every time.

    Two admin writes shipped like this -- trust_dial.py and admin_handlers/d365.py
    both set ``updated_at`` on ``namespaces``, which has no such column -- so
    setting a trust-dial tier and toggling D365 per namespace both failed outright.
    """
    unexpected = [
        f"{rel}:{line} UPDATE {table} SET {col!r}"
        for rel, line, table, missing in _update_violations()
        for col in missing
        if col not in KNOWN_BAD_QUERY_COLUMNS.get(table, {})
    ]
    assert not unexpected, "UPDATE statements set columns no migration declares:\n  " + "\n  ".join(
        unexpected
    )


def test_no_select_reads_a_column_the_ddl_does_not_declare() -> None:
    """A SELECT of a nonexistent column raises -- and is often inside a try/except."""
    unexpected = [
        f"{rel}:{line} SELECT {col!r} FROM {table}"
        for rel, line, table, missing in _select_violations()
        for col in missing
        if col not in KNOWN_BAD_QUERY_COLUMNS.get(table, {})
    ]
    assert not unexpected, (
        "SELECT statements read columns no migration declares:\n  " + "\n  ".join(unexpected)
    )


def test_query_allowlist_is_shrink_only_and_still_needed() -> None:
    """A stale entry reserves permission for a defect that was already fixed."""
    live = {
        (table, col)
        for finder in (_update_violations, _select_violations)
        for _rel, _line, table, missing in finder()
        for col in missing
    }
    stale = [
        f"{table}.{col}"
        for table, cols in KNOWN_BAD_QUERY_COLUMNS.items()
        for col in cols
        if (table, col) not in live
    ]
    assert not stale, f"delete these stale allowlist entries so the list keeps shrinking: {stale}"


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
    """Positive control: the scan must flag fabricated columns across SELECT projection and WHERE clauses.

    Proves the detector works on constructed cases and historical defects (PR #90),
    so a zero-violation result is evidence rather than silence.
    """
    tables = _declared_schema()
    assert "no_such_column_xyz" not in tables["namespaces"]

    # 1. Fabricated column in SELECT projection
    sel_violations = _extract_select_violations_from_sql(
        "SELECT no_such_col_abc FROM namespaces", tables
    )
    assert sel_violations == [("namespaces", ["no_such_col_abc"])]

    # 2. Fabricated column in WHERE clause
    where_violations = _extract_select_violations_from_sql(
        "SELECT id FROM namespaces WHERE no_such_filter_xyz = 'foo'", tables
    )
    assert where_violations == [("namespaces", ["no_such_filter_xyz"])]

    # 3. Standing Positive Control: cron.py:1896 pre-PR#90 defect
    cron_violations = _extract_select_violations_from_sql(
        """
        SELECT DISTINCT namespace_id, id AS asset_id
        FROM assets
        WHERE status != 'decommissioned'
        LIMIT 100
        """,
        tables,
    )
    assert cron_violations == [("assets", ["status"])]


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
