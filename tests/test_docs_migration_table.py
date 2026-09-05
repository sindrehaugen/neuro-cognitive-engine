"""``docs/database_architecture.md``'s per-migration table must list every file
in ``nce/migrations/`` (recursively -- ``optional/`` is a real, legitimate
migration directory, not a stray) and name no file that does not exist.

D13's history: this table drifted, was fixed by hand once, and drifted again.
A hand-edit that is not backed by a gate does not stay fixed -- that is the
whole reason this file exists instead of a third hand-edit.

Two ways this kind of gate goes vacuously green, both guarded against here:

  * a reworded heading or changed link format silences the row-matching regex,
    the parser finds zero rows, both set-comparisons pass on empty sets, and
    the gate is green forever -- guarded by ``_MIN_TABLE_ROWS``, a floor on how
    many rows the parser found;
  * a typo'd migrations path makes ``rglob`` discover nothing, so every
    (zero) discovered file trivially "has a row" -- guarded by
    ``_MIN_MIGRATION_FILES``, a floor on how many files were discovered on
    disk.

The table's rows are markdown links of the form
``[`NNN_name.sql`](https://github.com/.../nce/migrations/<relative path>)``.
The regex parses the **href**, not the backticked label -- only the href
carries ``optional/``, and parsing the label is how a gate gets built that
cannot see the one case (``optional/010_citus_sharding.sql``) that is exactly
right today.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "database_architecture.md"
MIGRATIONS_DIR = REPO_ROOT / "nce" / "migrations"

# Matches a table row's markdown link and captures the path relative to
# nce/migrations/ from the HREF -- e.g. "057_telemetry_samples.sql" or
# "optional/010_citus_sharding.sql". Anchored on the fixed GitHub blob prefix
# so a reworded row (different prose, different columns) still matches so
# long as it is still a link into nce/migrations/; only a changed *link
# format* can silence it, which is exactly what the floor below catches.
_ROW_LINK_RE = re.compile(
    r"\[`[^`]+\.sql`\]\("
    r"https://github\.com/sindrehaugen/NCE/blob/main/nce/migrations/([A-Za-z0-9_./-]+\.sql)"
    r"\)"
)

# 🔴 RE-MEASURED, and the first number written here was wrong -- which is the
# reason this floor is tight rather than generous. The authoring pass recorded
# "32 linked rows"; running the pattern against the file returns **60**, equal
# to the file count, with both set comparisons passing. A floor derived from a
# mis-measured count is a floor that does not gate: 30 would have stayed green
# while HALF this table silently stopped matching.
#
# Set just under the true count. Rows are only ever ADDED to this table -- the
# gate below fails the moment a migration has no row -- so a drop past this
# floor is either a deliberate deletion (move the floor in the same commit, and
# say why) or a reworded link format the pattern no longer sees, which is the
# blindness D13 is filed for.
_MIN_TABLE_ROWS = 58

# Measured: nce/migrations/ (recursively, including optional/) holds **60**
# *.sql files. A typo'd MIGRATIONS_DIR or a directory rename makes rglob()
# return zero, and zero files all vacuously "have a row" -- this floor makes
# that loud instead of silent.
#
# Tight rather than generous, but honest about its limit: this floor catches
# GROSS silencing (0, or a large drop), not a one-file miss. A glob that stops
# descending into optional/ returns 59, which clears 58 -- that case is caught
# by test_optional_010_citus_sharding_is_matched_correctly below, which is why
# that test is a named assertion and not a comment. Migrations are never deleted
# here (the ledger keys on filename, and missing_from_image() exists to alarm
# when a database has applied one this image lacks), so any count that goes DOWN
# is news.
_MIN_MIGRATION_FILES = 58


def _table_rows(doc_text: str) -> list[str]:
    """Relative-to-nce/migrations/ paths named by the table's row links."""
    return [m.group(1) for m in _ROW_LINK_RE.finditer(doc_text)]


def _discovered_migration_files() -> list[str]:
    """Relative-to-nce/migrations/ paths of every *.sql file on disk."""
    return [p.relative_to(MIGRATIONS_DIR).as_posix() for p in MIGRATIONS_DIR.rglob("*.sql")]


def test_migration_table_row_count_floor() -> None:
    """The row-link pattern must still be matching a realistic number of rows.

    Without this floor, a reworded heading or a changed link format silences
    the pattern entirely -- the parser finds zero rows, the "every file has a
    row" and "every row names a file" assertions below both pass vacuously on
    empty sets, and the gate is green forever no matter how far the table
    drifts.
    """
    rows = _table_rows(DOC.read_text(encoding="utf-8"))
    assert len(rows) >= _MIN_TABLE_ROWS, (
        f"only {len(rows)} migration-table row links matched in {DOC.name} but "
        f"at least {_MIN_TABLE_ROWS} are expected -- the row-link pattern was "
        "silenced by a reworded heading or a changed link format, which would "
        "leave this gate blind. Matched paths: " + ", ".join(sorted(rows))
    )


def test_migration_file_discovery_floor() -> None:
    """rglob() must still be finding a realistic number of files on disk.

    Without this floor, a typo'd MIGRATIONS_DIR (or a renamed directory) makes
    rglob() return nothing, and zero discovered files trivially "all have a
    row" -- the missing-from-table assertion below would then pass no matter
    how badly the table has drifted.
    """
    files = _discovered_migration_files()
    assert len(files) >= _MIN_MIGRATION_FILES, (
        f"only {len(files)} *.sql files discovered under {MIGRATIONS_DIR} "
        f"(recursively) but at least {_MIN_MIGRATION_FILES} are expected -- "
        "migration discovery is broken (wrong path, or rglob returned "
        "nothing), which would leave this gate blind."
    )


def test_every_migration_file_has_a_table_row() -> None:
    files = set(_discovered_migration_files())
    rows = set(_table_rows(DOC.read_text(encoding="utf-8")))

    missing = sorted(files - rows)
    assert not missing, (
        f"{len(missing)} file(s) under {MIGRATIONS_DIR} have no row in "
        f"{DOC.name}'s per-migration table: " + ", ".join(missing)
    )


def test_every_table_row_names_a_file_that_exists() -> None:
    files = set(_discovered_migration_files())
    rows = set(_table_rows(DOC.read_text(encoding="utf-8")))

    dangling = sorted(rows - files)
    assert not dangling, (
        f"{len(dangling)} row(s) in {DOC.name}'s per-migration table name a "
        f"file that does not exist under {MIGRATIONS_DIR}: " + ", ".join(dangling)
    )


def test_optional_010_citus_sharding_is_matched_correctly() -> None:
    """The exact case that fooled a naive gate: a legitimate `optional/` file.

    `optional/010_citus_sharding.sql` is a real migration in a real
    subdirectory. It must be discovered by rglob(), matched by the row-link
    regex (via its href, which carries `optional/`), and therefore never
    reported as missing from the table.
    """
    files = set(_discovered_migration_files())
    rows = set(_table_rows(DOC.read_text(encoding="utf-8")))

    assert "optional/010_citus_sharding.sql" in files, (
        "expected nce/migrations/optional/010_citus_sharding.sql to exist on "
        "disk -- has it moved or been renamed?"
    )
    assert "optional/010_citus_sharding.sql" in rows, (
        "docs/database_architecture.md's row for 010_citus_sharding.sql no "
        "longer links to optional/010_citus_sharding.sql -- has its href "
        "changed?"
    )
    assert "optional/010_citus_sharding.sql" not in (files - rows), (
        "optional/010_citus_sharding.sql was reported as missing from the "
        "table -- the row-link regex is not matching its href correctly"
    )
