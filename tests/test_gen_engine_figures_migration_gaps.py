"""
tests/test_gen_engine_figures_migration_gaps.py
==================================================
K-consolidation: scripts/gen_engine_figures.py's migration-gap categories
(2026-09-20).

Before this test, nothing verified extract_migration_stats()'s gap
computation was CORRECT -- only that docs/_generated/engine_figures.md's
committed content matched a fresh regen (ci.yml's D-GEN gate), which
proves internal consistency, not correctness. A generator that
consistently computes the wrong thing passes that gate forever.

The finding this closes: `010_citus_sharding.sql` exists under
`nce/migrations/optional/` but has no file under the base
`nce/migrations/` directory. Two previously-disagreeing consumers both
read from the SAME underlying fact but described it differently --
docs/_generated/engine_figures.md's main table row called 010 a bare
"gap" (correct only for "missing from base", not stated as such), while
ENGINE_STATUS.md's hand-written sentence said "010 exists only under
optional/" (correct, but hardcoded -- unverified by any regen). Neither
was a computation bug; both were answering "is 010 a gap" without the
qualifier that makes either answer true. Fixed by deriving BOTH
categories explicitly (never_allocated_nums: no file anywhere;
optional_only_nums: base-missing but optional-covered) so a single
computation feeds every consumer and the two can no longer re-diverge.
"""

from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_generator():
    path = _ROOT / "scripts" / "gen_engine_figures.py"
    spec = importlib.util.spec_from_file_location("gen_engine_figures", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gen = _load_generator()
extract_migration_stats = _gen.extract_migration_stats
render_migration_gaps_display = _gen.render_migration_gaps_display

_REPO = "."
_BASELINE = "HEAD"


def test_010_is_optional_only_not_never_allocated() -> None:
    """Post-clean-slate migration split: Core base migrations 001..034 are strictly
    contiguous, so 010 is present in base (010_event_log_sig_version.sql) and
    neither optional_only_nums nor never_allocated_nums contains 010."""
    stats = extract_migration_stats(_REPO, _BASELINE)
    assert 10 not in stats["optional_only_nums"]
    assert 10 not in stats["never_allocated_nums"]


def test_never_allocated_and_optional_only_are_disjoint() -> None:
    """A number cannot be both "no file anywhere" and "covered by an
    optional file" -- the two categories partition missing_prefixes, they
    do not overlap. A regression here would double-count or drop a gap."""
    stats = extract_migration_stats(_REPO, _BASELINE)
    never = set(stats["never_allocated_nums"])
    optional_only = set(stats["optional_only_nums"])
    assert not (never & optional_only), (
        f"never_allocated_nums and optional_only_nums overlap: {never & optional_only}"
    )
    assert never | optional_only == set(stats["missing_prefixes"]), (
        "the two categories must exactly partition missing_prefixes (the "
        "base-only gap list) -- a number falling into neither, or into "
        "both, means the categorisation logic itself is wrong"
    )


def test_known_true_gaps_are_never_allocated() -> None:
    """Post-clean-slate migration split: Core migrations 001..034 are strictly
    contiguous with zero gaps (confirmed by tests/test_migration_number_uniqueness.py's
    empty _KNOWN_GAPS). Both ratchets must agree that there are zero base gaps."""
    stats = extract_migration_stats(_REPO, _BASELINE)
    assert stats["never_allocated_nums"] == []
    assert stats["optional_only_nums"] == []


def test_gaps_display_names_both_categories_distinctly() -> None:
    """When base migrations are contiguous, gaps_display reports 'no gaps',
    while synthetic inputs with both categories name both distinctly."""
    stats = extract_migration_stats(_REPO, _BASELINE)
    assert stats["gaps_display"] == "no gaps"
    synthetic_display = render_migration_gaps_display([2, 9, 59], [10])
    assert "never allocated" in synthetic_display
    assert "`010`" in synthetic_display
    assert "only under" in synthetic_display
    assert "`nce/migrations/optional/`" in synthetic_display


def test_render_omits_optional_clause_when_none_exist() -> None:
    """Positive control (the converse case): if a future wave fills 010 in
    base too (or removes the optional/ file), optional_only_nums becomes
    empty and the rendered phrase must drop the "exists only under
    optional/" clause rather than emit an empty or malformed one. Proven
    with a synthetic input, not by waiting for that day to actually
    arrive."""
    display = render_migration_gaps_display([2, 9, 59], [])
    assert "only under" not in display
    assert "`002`" in display and "`009`" in display and "`059`" in display


def test_render_omits_never_allocated_clause_when_none_exist() -> None:
    """The other converse: every base-missing number is optional-covered --
    "no gaps" must be said outright, not "gaps at (empty)"."""
    display = render_migration_gaps_display([], [10])
    assert display.startswith("no gaps")
    assert "`010`" in display


def test_render_pluralises_the_optional_clause_correctly() -> None:
    """Two-vs-one wording is a real distinction a reader notices; a
    hard-coded singular would misdescribe a future multi-number case."""
    one = render_migration_gaps_display([2], [10])
    many = render_migration_gaps_display([2], [10, 11])
    assert "exists only under" in one
    assert "exist only under" in many
