"""INSERT column lists must name columns the DDL actually declares.

Two defects of exactly this shape shipped and sat undetected:

* ``business_insights/provenance.py`` inserted into ``v3_cognitive_ledger`` with
  ``entity_id``/``entity_type``/``change_type`` -- none of which that table has.
  Every insert raised, was caught, logged at warning and discarded, so the
  advertised provenance audit recorded nothing.
* ``marketing/approval.py`` previously did the same with
  ``category``/``subject_id``/``details`` (re-homed onto event_log via append_event).

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

Detector Limits & Blind Spots (Honest Coverage Census):
-------------------------------------------------------
Total static SQL statements parsed across nce/: ~630 SELECT blocks.
1. Multi-table JOIN queries: ~72 blocks (~11.4% blind spot). Skipped because
   column qualification across table aliases in raw string literals without a full
   SQL grammar parser produces noisy false positives.
2. Wildcard (SELECT *) projections: ~140 blocks. By definition, a wildcard read
   cannot name a non-existent column in its projection list (though WHERE clauses
   are inspected).
3. Dynamic SQL & string concatenations / f-strings: ~35 blocks where table or
   column identifiers are interpolated at runtime.
4. JSONB operators (->, ->>) & expressions with inline casts.

Effective static coverage: ~89% of eligible single-table SELECT and UPDATE blocks,
100% of static INSERT INTO statements across 99 parsed tables (132 live tables including
32 partitions and 1 applied_migrations).

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
KNOWN_BAD_INSERT_COLUMNS: dict[str, dict[str, str]] = {}

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

# SQL reserved words that a CREATE TABLE body can leak into the column set.
#
# 2026-09-08: `_table_columns` reported 14 columns for `system_design_node_state`
# while the live database has 9 -- the extras were `case`, `else`, `end`, `or`, `when`,
# tokens from a multi-line `CHECK (CASE WHEN ... ELSE ... END)` constraint. Eleven other
# tables carried a phantom `references` from inline `REFERENCES tbl(col)` clauses.
# 16 of 99 tables were affected, every error in the FAIL-OPEN direction: a query naming
# a phantom entry was silently accepted as declared.
#
# `fullmatch(r"[a-z_][a-z0-9_]*")` cannot catch these, because SQL keywords are lexically
# valid identifiers. No real column in this schema is named after one of these words --
# `test_no_reserved_word_is_a_declared_column` pins that, so if a future migration ever
# does declare one, this filter fails loudly instead of hiding it.
_RESERVED_NON_COLUMNS = {
    "and",
    "case",
    "else",
    "end",
    "on",
    "or",
    "references",
    "then",
    "when",
}

_CONSTRAINT_KEYWORDS = {
    "primary",
    "foreign",
    "unique",
    "check",
    "constraint",
    "exclude",
    "like",
    "references",
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


def _strip_ctes(clean_sql: str) -> str:
    """Strip 'WITH ...' CTE prefixes once subqueries have been reduced to (1)."""
    return re.sub(
        r"^\s*WITH\s+(?:RECURSIVE\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\s*(?:\([^)]*\))?\s*AS\s*\(\s*1\s*\)\s*,?\s*)+",
        "",
        clean_sql,
        flags=re.I | re.DOTALL,
    )


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

    raw_body = match.group(1)

    # 1. Strip -- comments line-by-line first so parentheses and commas in comments
    # do not corrupt depth tracking.
    clean_lines: list[str] = []
    for line in raw_body.split("\n"):
        clean_lines.append(line.split("--", 1)[0])
    body = "\n".join(clean_lines)

    # 2. Split top-level definitions by comma at parenthesis depth 1
    defs: list[str] = []
    depth = 1
    curr: list[str] = []
    in_quote = False
    prev = ""
    for char in body:
        if char == "'" and prev != "\\":
            in_quote = not in_quote
        elif not in_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
        if not in_quote and char == "," and depth == 1:
            defs.append("".join(curr))
            curr = []
        else:
            curr.append(char)
        prev = char
    if curr:
        defs.append("".join(curr))

    columns: set[str] = set()
    for d in defs:
        chunk = d.strip()
        if not chunk:
            continue
        tokens = chunk.split()
        first = tokens[0].strip('"').lower()
        if first in _CONSTRAINT_KEYWORDS:
            continue
        if first in _RESERVED_NON_COLUMNS:
            # A constraint body leaked a keyword into the column position. Accepting it
            # would make the detector fail-open for that name. See _RESERVED_NON_COLUMNS.
            continue
        if re.fullmatch(r"[a-z_][a-z0-9_]*", first):
            columns.add(first)
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
    clean_query = _strip_ctes(clean_query)
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
    # Pin parser accuracy: system_design_node_state has exactly 9 columns (no phantom CASE/WHEN/END keywords)
    assert len(tables["system_design_node_state"]) == 9, (
        f"system_design_node_state parsed {len(tables['system_design_node_state'])} columns; expected 9"
    )
    # The table both known defects target, with the column that makes it unusable.
    assert "empathic_tensor" in tables["v3_cognitive_ledger"]
    # Assert zero collisions between parsed table columns and _SQL_KEYWORDS
    collisions = {t: cols & _SQL_KEYWORDS for t, cols in tables.items() if cols & _SQL_KEYWORDS}
    assert not collisions, (
        f"Declared column parser leaked SQL keywords into column sets: {collisions}"
    )


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

    # 4. Standing Positive Control: CTE outer query phantom column detection
    cte_violations = _extract_select_violations_from_sql(
        "WITH x AS (SELECT 1) SELECT totally_fake_col FROM assets", tables
    )
    assert cte_violations == [("assets", ["totally_fake_col"])]


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


def test_no_reserved_word_is_a_declared_column() -> None:
    """No real column is named after a word in ``_RESERVED_NON_COLUMNS``.

    That filter drops keyword tokens a constraint body leaked into the column position.
    It is only safe while no genuine column shares one of those names -- otherwise the
    detector goes fail-open for that column instead.

    This control has already earned its place: the first version of the filter included
    ``date``, which IS a real column on ``per_diems``, and this assertion is what a
    parser-vs-live-database comparison surfaced. Keep it, and if a future migration
    declares a column named after a reserved word, delete that word from the filter and
    fix ``_table_columns`` properly rather than widening the exclusion.
    """

    tables = _declared_schema()
    offenders: list[str] = []
    for table, columns in sorted(tables.items()):
        for word in sorted(_RESERVED_NON_COLUMNS & {c.lower() for c in columns}):
            offenders.append(f"{table}.{word}")

    assert offenders == [], (
        "These declared columns share a name with a _RESERVED_NON_COLUMNS entry, so the "
        "parser now silently drops them and the detector is fail-open for each:"
        + "".join(chr(10) + "  " + o for o in offenders)
    )


def test_constraint_bodies_do_not_leak_columns() -> None:
    """``system_design_node_state`` declares exactly its 9 real columns.

    Pins the fail-open defect measured on 2026-09-08: the parser reported **14** columns
    for this table while the database has 9, the extras being ``case``, ``else``, ``end``,
    ``or`` and ``when`` -- tokens from a multi-line ``CHECK (CASE WHEN ... END)`` body.
    16 of 99 tables were affected. This table is the worst case, so it is the canary.
    """

    columns = _declared_schema()["system_design_node_state"]
    assert columns == {
        "id",
        "namespace_id",
        "node_label",
        "node_type",
        "status",
        "revision",
        "salience",
        "created_at",
        "updated_at",
    }, f"constraint body leaked into the column set: {sorted(columns)}"
