"""
tests/test_migration_header_matches_filename.py
=================================================
Instrument gap closed, 2026-09-20: every migration from 081 onward has a
first line that is a comment naming its own file
(`-- 106_product_lifecycle_status_check.sql`), but nothing asserted it. A
renumber -- the single most error-prone operation in this repo -- silently
broke it the same night this was found: PR #352 renamed
`106_sales_deal_participants.sql` to `107_...` and updated every reference
except the file's own first line, which still read `-- 106_...`.

Checked, not assumed: this is NOT a repo-wide convention. Migrations before
081 use a mix of free-form banner comments and prose descriptions with no
filename at all (verified directly: 001, 005, 006, 014-017, 020-022, 025-026,
066, 071 all fail a literal match). The convention starts cleanly at 081
(`081_schema_sql_only_ddl_baseline.sql`, introduced alongside
`scripts/split_schema.py`) and holds without exception through 106 -- 080 is
a one-off boundary case with a trailing `-- RL-H18` suffix on the same line
and is deliberately excluded rather than papered over with a prefix-match
that would also silently accept a stale/wrong filename with extra text
appended. Retroactively rewriting pre-081 headers is out of scope for this
fix (a much larger, unrelated change to unrelated files); this test covers
what the estate actually, consistently does today and forward.

This is not a floor or a ratchet -- there is nothing to shrink or grow for
the covered range, just one fact that must hold for every file in it,
forever: pure discovered-set equality, no hand-maintained count.
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MIGRATIONS_DIRS = [
    _REPO_ROOT / "nce" / "migrations",
    _REPO_ROOT / "nce" / "migrations" / "optional",
]
_CONVENTION_STARTS_AT = 81


def _migration_files() -> list[pathlib.Path]:
    files = []
    for d in _MIGRATIONS_DIRS:
        for path in sorted(d.glob("*.sql")):
            m = re.match(r"^(\d{3})_", path.name)
            if m and int(m.group(1)) >= _CONVENTION_STARTS_AT:
                files.append(path)
    return files


def test_discovery_floor_finds_migration_files() -> None:
    """Guard the guard: if the glob above ever stops matching anything, the
    main test below would vacuously pass on zero files."""
    files = _migration_files()
    assert len(files) >= 25, (
        f"Only {len(files)} migration files (>= {_CONVENTION_STARTS_AT}) discovered -- "
        "the glob or the numeric filter is broken, not the estate"
    )


def test_every_migration_header_matches_its_own_filename() -> None:
    """Every migration's first line must be `-- <its own filename>`."""
    mismatches: list[str] = []
    for path in _migration_files():
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        expected = f"-- {path.name}"
        if first_line != expected:
            mismatches.append(f"{path}: header is {first_line!r}, expected {expected!r}")

    assert not mismatches, (
        "Migration file(s) whose first-line header no longer matches their own "
        "filename (stale after a renumber?):\n" + "\n".join(mismatches)
    )


def test_positive_control_catches_a_stale_header(tmp_path: pathlib.Path) -> None:
    """Prove the check actually fires: a synthetic file whose header names a
    different filename than its own must be caught, not silently accepted."""
    stale_dir = tmp_path / "migrations"
    stale_dir.mkdir()
    stale_file = stale_dir / "999_renamed_but_header_not_updated.sql"
    stale_file.write_text("-- 998_old_name_before_renumber.sql\nSELECT 1;\n", encoding="utf-8")

    first_line = stale_file.read_text(encoding="utf-8").splitlines()[0].strip()
    expected = f"-- {stale_file.name}"
    assert first_line != expected, "positive control's synthetic fixture is not actually stale"
