# Static Detector Limits and Blind Spot Census

## 1. Executive Summary
This document establishes the honest operational boundaries and mathematical census of NCE's static AST analysis instruments:
1. **DDL Column Match Detector (`tests/test_insert_columns_match_ddl.py`)**: Validates SQL statements against declared database schema.
2. **Swallowed Exception Census (`tests/unit/test_swallowed_exception_census.py`)**: Enforces shrink-only governance over broad exception handlers that swallow database writes.

Static analysis in NCE intentionally balances strictness and false-positive avoidance: an instrument that flags false positives gets allowlisted into uselessness. Narrow, 100% truthful detectors with explicit, measured blind spots provide far greater security and reliability guarantees.

---

## 2. SQL DDL Column Match Detector Limits (`tests/test_insert_columns_match_ddl.py`)

### 2.1 Scope & Coverage Metrics
- **Schema Base**: 99 parsed tables from `nce/schema.sql` and `nce/migrations/*.sql` (representing 132 live database relations, accounting for 32 partition tables and 1 `applied_migrations` ledger).
- **Parsed Statements**: ~630 static SQL query blocks identified across Python source files in `nce/`.
- **INSERT Coverage**: 100% of static `INSERT INTO <table> (...)` statements analyzed and verified against table DDL.
- **Single-Table Read/Write Coverage**: ~89% of eligible single-table `SELECT` and `UPDATE` statements inspected.

### 2.2 Blind Spots & Exclusions (Honest Accounting)
1. **Multi-Table JOIN Queries (~72 blocks, ~11.4% of total queries)**:
   - *Reason*: Without a full SQL parser and dialect grammar, resolving unqualified or aliased columns (e.g. `table_a.id`, `table_b.name`) across arbitrary joins produces spurious phantom column errors.
   - *Boundary*: All queries containing explicit `JOIN` clauses are skipped in the projection validation pass.
2. **Wildcard Projections (`SELECT *`, ~140 blocks)**:
   - *Reason*: By definition, a wildcard projection does not name individual column identifiers in its projection list.
   - *Boundary*: Projection validation is skipped; however, any trailing single-table `WHERE` predicates on known tables remain eligible for predicate column validation where parsed.
3. **Dynamic SQL and String Concatenation (~35 blocks)**:
   - *Reason*: Queries constructed via runtime string interpolation (f-strings, `.format()`, `%` formatting) where table names or column lists are variables cannot be statically bound without abstract interpretation.
   - *Boundary*: Skipped when the table name or target column cannot be resolved to an AST constant string.
4. **JSONB Operators and Explicit Type Casts**:
   - *Reason*: Expressions using PostgreSQL operators such as `->`, `->>`, `#>`, or inline casts (`::text`, `::jsonb`) represent compound expressions rather than bare column identifiers.
   - *Boundary*: Projections containing operator tokens are bypassed to prevent false alarms.
5. **CTE (Common Table Expressions) Handling**:
   - *Hardening*: A balanced-parenthesis scanner (`_strip_ctes`) removes `WITH [RECURSIVE] name [(cols)] AS (...)` blocks. This ensures that the primary outer `SELECT` or `UPDATE` statement is extracted and checked against DDL tables, eliminating the historical blind spot where any query beginning with `WITH` was ignored.

---

## 3. Swallowed Exception Census (`tests/unit/test_swallowed_exception_census.py`)

### 3.1 Census Metrics & Governance
- **Starting Direct Baseline**: 21 pinned sites (covering direct `append_event`, `_append_a2a_event`, `emit_graph_write`, and raw SQL write operations).
- **1-Level Indirection Resolution**: Expanded to 26 pinned sites (+5 sites) after incorporating helper write wrapper `record_ledger_audit`.
- **Discovery Floor**: Hard floor set to >= 25 discovered sites.
- **Shrink-Only Contract**: `KNOWN_SWALLOWED_DB_WRITE_SITES` rejects any newly introduced swallowed DB writes. A remediated site must be removed in the same commit, lowering the ceiling.
- **Metadata Standard**: Every entry requires an owner, a reason (>= 60 characters), and a concrete remediation plan (>= 60 characters).

### 3.2 1-Level Indirection Architecture
- **Detection Mechanism**: The AST scanner checks whether an exception block encloses:
  1. Direct database mutation calls (`execute`, `executemany`, `fetch`, `fetchrow`, `fetchval` with write SQL keywords).
  2. Core event emitters (`append_event`, `_append_a2a_event`, `emit_graph_write`).
  3. Immediate module-level helper wrappers that perform transactional database writes (specifically `record_ledger_audit`).
- **Resolved Call Sites**:
  - `nce/vertical_modules/business_insights/ask.py::do_ask_business`
  - `nce/vertical_modules/business_insights/board_pack.py::do_generate_board_pack`
  - `nce/vertical_modules/business_insights/brief.py::do_morning_brief`
  - `nce/vertical_modules/business_insights/radar.py::do_risk_radar`
  - `nce/vertical_modules/business_insights/scenario.py::do_run_scenario`

### 3.3 Census Limitations & Non-Goals
1. **Multi-Level Indirection (>1 call deep)**:
   - The scanner does not construct a global inter-procedural call graph across all vertical modules. Calls nested two or more functions away from the write site are out of scope to avoid deep recursive AST walks and cyclic dependency issues.
2. **Dynamic Invocation / Reflection**:
   - Dynamic calls through `getattr(obj, method)`, `locals()`, or variable function references are not resolved.
3. **Non-Mutating Reads**:
   - Exception blocks enclosing pure read operations (`SELECT`) or computational loops without DB writes are intentionally permitted and excluded from the census.

---

## 4. Maintenance and Ratchet Protocol
Any modification to static scanners must:
1. Include standing positive controls (U18) proving the scanner detects the target defect class.
2. Include negative controls proving the scanner does not flag clean or properly re-raised code.
3. Update this census whenever discovery floors or parser boundaries are modified.
