"""Migration-number uniqueness and gap ratchet (janitor pass 7, K-H6).

Found live: on 2026-09-18, Lane F's #239 and Lane A's #240 independently
added ``nce/migrations/085_geodata_osm_elements.sql`` and
``nce/migrations/085_legal_entity_register.sql`` -- both lanes correctly took
"the next number" off ``main`` as it stood when each branched, and neither
could see the other. ML-orch ruled the collision by fiat (lowest PR number
keeps the number; #240 moved to 086) so nobody had to wait, but that was a
one-off resolution, not a check. This is now the normal failure mode of a
multi-lane programme building in parallel, not a rare accident, and nothing
in the estate previously asserted migration numbers are unique.

**Why the existing per-migration completeness check in
``docs/database_architecture.md`` never caught it:** that check asserts every
migration *file has a row* in the doc -- a completeness check, not a
uniqueness one. Two files can share a numeric prefix and both have rows and
both pass. The only symptom of a real collision is a merge conflict, and only
if the two PRs happen to touch a common file; if they sort differently or
touch disjoint files (as #239/#240 did -- different filenames, same prefix)
there is no symptom at all until something applies migrations in numeric
order and the ordering is ambiguous between the two same-numbered files.

Numbers are derived from the directory listing itself (never a hand-maintained
list, for the same reason ``scripts/gen_surface_table.py``'s engine discovery
was rewritten off a hard-coded list -- a new migration cannot be omitted by
forgetting to update a count file).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIRS = (_ROOT / "nce" / "migrations", _ROOT / "nce" / "migrations" / "optional")

_PREFIX_RE = re.compile(r"^(\d{3})_")

# Shrink-only allowlist of gaps in the numeric sequence that are known and
# explained, not silent omissions. Documented independently in
# docs/vertical_engines/ENGINE_STATUS.md's "SQL migrations" row -- this list
# and that prose must describe the same gaps, or one of them is stale.
#
# 010 is NOT here: 010_citus_sharding.sql exists under
# nce/migrations/optional/, and _discover_migration_numbers scans both
# directories into one combined numbering, so 010 is correctly not a gap at
# all -- it would only appear here if a future change stopped scanning the
# optional/ directory, which the discovery-floor test above would also catch.
_KNOWN_GAPS: dict[int, str] = {
    2: "never allocated (owner: pre-v1.6 history, reason unknown -- gap predates this ratchet)",
    9: "never allocated (owner: pre-v1.6 history, reason unknown -- gap predates this ratchet)",
    59: "never allocated (owner: pre-v1.6 history, reason unknown -- gap predates this ratchet)",
    # Reserved by open PRs not yet merged on this branch's base -- this
    # branch (Lane F Wave F-12, migration 089) is unstacked from both, so
    # its own tree shows them as gaps rather than filled numbers. Owners
    # per ML-orch's collision ruling: 87 -> Lane D (PR #242,
    # assets_shell_product), 88 -> Lane B (PR #245, sales_resource_tables).
    # 99 was reserved by PR #330 (Lane E) and 100 by this same lane's own
    # PR #332 (Wave B-15, procurement_deal_registrations) -- both have since
    # merged, so neither is a gap on this rebased branch. Removed per the
    # shrink-only rule rather than left stale.
    #
    # 104 is Lane E's (PR #336, system_design_design_requests) -- both this
    # lane's own B-13 (customer_invoices, this PR) and #336 raced onto 104;
    # ML-orch's ruling kept 104 with #336 (claimed first, rebased onto it
    # twice already) and renumbered this lane's own migration to 105. This
    # branch is unstacked from #336, so its own tree shows 104 as a gap
    # until #336 merges -- remove this entry then, per the shrink-only rule.
    104: "reserved by PR #336 (Lane E, system_design_design_requests) -- not yet merged on this branch's base",
}


def _discover_migration_numbers(migrations_dirs: tuple[Path, ...]) -> dict[int, list[str]]:
    """Map numeric prefix -> list of filenames carrying it, across every
    given directory. A prefix mapping to more than one filename is a
    collision; the caller decides what to do with that."""
    by_number: dict[int, list[str]] = {}
    for directory in migrations_dirs:
        if not directory.is_dir():
            continue
        for f in sorted(directory.glob("*.sql")):
            match = _PREFIX_RE.match(f.name)
            if not match:
                continue
            num = int(match.group(1))
            by_number.setdefault(num, []).append(f.name)
    return by_number


def test_discovery_floor_finds_a_sane_number_of_migrations() -> None:
    """U18 pattern: if the glob or the directory path broke, the checks below
    would find zero collisions and zero gaps and pass vacuously. Assert a
    floor well below the measured 81 files at charter issue."""
    by_number = _discover_migration_numbers(_MIGRATIONS_DIRS)
    assert len(by_number) >= 70, (
        f"Only {len(by_number)} distinct migration numbers found across "
        f"{[str(d) for d in _MIGRATIONS_DIRS]} -- the directory glob likely "
        f"broke, not the estate having shrunk to this size."
    )


def test_every_migration_number_is_unique() -> None:
    by_number = _discover_migration_numbers(_MIGRATIONS_DIRS)
    collisions = {num: names for num, names in by_number.items() if len(names) > 1}
    assert not collisions, (
        "Duplicate migration numbers found -- two files claim the same "
        "numeric prefix, exactly the #239/#240 collision this ratchet exists "
        "to catch. Renumber all but one file per collision, lowest PR number "
        "keeps the number (ML-orch's ruling on 2026-09-18):\n  "
        + "\n  ".join(f"{num:03d}: {sorted(names)}" for num, names in sorted(collisions.items()))
    )


def test_gaps_in_the_sequence_are_all_explained() -> None:
    """Exact-match against the shrink-only allowlist, not a subset check in
    either direction: a NEW unexplained gap must fail loudly (a migration
    was silently skipped or misnumbered), and a KNOWN gap that got filled
    must also fail loudly (this allowlist is now stale and must shrink)."""
    by_number = _discover_migration_numbers(_MIGRATIONS_DIRS)
    numbers = sorted(by_number)
    full_range = set(range(numbers[0], numbers[-1] + 1))
    actual_gaps = full_range - set(numbers)

    unexplained = actual_gaps - set(_KNOWN_GAPS)
    assert not unexplained, (
        f"Unexplained gap(s) in the migration sequence: {sorted(unexplained)}. "
        f"Either a migration was silently skipped/misnumbered, or this is a "
        f"legitimate new gap that needs an owner+reason entry in "
        f"_KNOWN_GAPS above."
    )

    stale_allowlist_entries = set(_KNOWN_GAPS) - actual_gaps
    assert not stale_allowlist_entries, (
        f"_KNOWN_GAPS lists gap(s) that no longer exist: "
        f"{sorted(stale_allowlist_entries)}. A migration now fills this "
        f"number -- shrink the allowlist (it is shrink-only, per the "
        f"RESOURCE_SURFACE_EXEMPTIONS precedent)."
    )


def test_collision_and_gap_checks_fire_on_synthetic_input(tmp_path: Path) -> None:
    """Positive control (K-H6): both checks above operate on a real directory
    listing, not a mocked object, so this control builds a real temporary
    directory with a synthetic duplicate and a synthetic unexplained gap and
    confirms _discover_migration_numbers surfaces both -- proving the
    detection logic itself is not vacuous, independent of whatever main's
    current state happens to be."""
    (tmp_path / "001_a.sql").write_text("-- a", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("-- b", encoding="utf-8")
    (tmp_path / "002_c.sql").write_text("-- c (duplicate of 002)", encoding="utf-8")
    (tmp_path / "004_d.sql").write_text("-- d (003 is an unexplained gap)", encoding="utf-8")

    by_number = _discover_migration_numbers((tmp_path,))

    collisions = {num: names for num, names in by_number.items() if len(names) > 1}
    assert collisions == {2: ["002_b.sql", "002_c.sql"]}, (
        "Synthetic duplicate at 002 was not detected -- the collision check is vacuous."
    )

    numbers = sorted(by_number)
    full_range = set(range(numbers[0], numbers[-1] + 1))
    actual_gaps = full_range - set(numbers)
    assert actual_gaps == {3}, "Synthetic gap at 003 was not detected -- the gap check is vacuous."
