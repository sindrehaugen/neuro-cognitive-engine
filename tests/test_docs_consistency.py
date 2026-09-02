"""``docs/multi_tenancy.md`` must agree with the code it describes.

The doc states the number of tenant-scoped RLS tables in nine separate places --
two headings, three prose sentences, an ASCII diagram cell, two table cells and a
derived subtraction -- plus a per-domain inventory whose Count column sums to the
same number. Nothing referenced the file from ``tests/``, so it reached round 3
self-contradicting, was hand-corrected twice, and a later merge had to move ten
sites again, two of which were prose found only by grepping the number.

One invariant, one file: every stated tenant-table count equals
``len(EXPECTED_TENANT_RLS_TABLES)``. Site patterns are anchored on their
surrounding words, and the test asserts a floor on how many sites it found -- a
reworded doc that silences a pattern fails loudly instead of passing vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

from nce.event_log import EXPECTED_TENANT_RLS_TABLES

DOC = Path(__file__).resolve().parents[1] / "docs" / "multi_tenancy.md"

# (label, pattern) -- each pattern captures one stated tenant-table count.
_COUNT_SITES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "heading/list `EXPECTED_TENANT_RLS_TABLES` (N tables)",
        re.compile(r"`EXPECTED_TENANT_RLS_TABLES`\s*\((\d+) [Tt]ables\)"),
    ),
    (
        "section 2a prose '(N tables), rather than'",
        re.compile(r"\((\d+) tables\), rather than"),
    ),
    (
        "section 2a prose 'validate all N tenant tables'",
        re.compile(r"validate all (\d+) tenant tables"),
    ),
    (
        "schema-surface ASCII diagram tenant cell",
        re.compile(r"EXPECTED_TENANT_RLS_TABLES.*\n.*?\((\d+) Tables\)"),
    ),
    (
        "section 2b prose 'lacks N tables (COUNT - 42)'",
        re.compile(r"lacks \d+ tables \((\d+)\s*[−-]\s*\d+\)"),
    ),
    (
        "section 2b comparison table, authoritative row",
        re.compile(
            r"`EXPECTED_TENANT_RLS_TABLES` \(\[`nce/event_log\.py`\][^|]*\|\s*\*\*(\d+)\*\*"
        ),
    ),
    (
        "section 2c heading 'Inventory of the N Tenant RLS Tables'",
        re.compile(r"Inventory of the (\d+) Tenant RLS Tables"),
    ),
    (
        "section 2c prose 'The N tables in'",
        re.compile(r"The (\d+) tables in `EXPECTED_TENANT_RLS_TABLES`"),
    ),
)

# Sites the patterns above matched when this test was written. A doc edit that
# drops below this is not allowed to pass: the missing site is either a deleted
# claim (update the floor deliberately) or a reworded one the pattern no longer
# sees, which would make this gate blind exactly the way the FK ratchet was.
_MIN_SITES = 9

# The sibling doc describing the SAME invariant from the same constant. It was
# outside this gate and had drifted to 62 in three places while the gated file
# stayed correct at 64.
DOC_DB_ARCH = Path(__file__).resolve().parents[1] / "docs" / "database_architecture.md"

_DB_ARCH_COUNT_SITES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "RLS surface prose 'encompasses N tables'",
        re.compile(r"encompasses (\d+) tables defined in `EXPECTED_TENANT_RLS_TABLES`"),
    ),
    (
        "validator prose 'against the N tables in'",
        re.compile(r"against the (\d+) tables in `EXPECTED_TENANT_RLS_TABLES`"),
    ),
    (
        "relrowsecurity assertion 'enabled on all N tenant tables'",
        re.compile(r"enabled on all (\d+) tenant tables"),
    ),
)

_DB_ARCH_MIN_SITES = 3


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _inventory_section(doc: str) -> str:
    """Section 2c: the per-domain inventory table."""
    after = doc.split("### 2c.", 1)
    assert len(after) == 2, "docs/multi_tenancy.md no longer has a '### 2c.' inventory section"
    return after[1].split("\n---", 1)[0]


def test_multi_tenancy_doc_states_the_real_tenant_table_count() -> None:
    expected = len(EXPECTED_TENANT_RLS_TABLES)
    doc = _doc_text()

    found: list[tuple[str, int, int]] = []  # (label, line number, stated count)
    for label, pattern in _COUNT_SITES:
        for m in pattern.finditer(doc):
            found.append((label, doc.count("\n", 0, m.start(1)) + 1, int(m.group(1))))

    assert len(found) >= _MIN_SITES, (
        f"only {len(found)} tenant-count sites matched in {DOC.name} but at least "
        f"{_MIN_SITES} are expected -- a claim was deleted or reworded past its "
        "pattern, which would leave this gate blind. Matched: "
        + ", ".join(f"{lbl}@L{ln}" for lbl, ln, _ in found)
    )

    wrong = [(lbl, ln, got) for lbl, ln, got in found if got != expected]
    assert not wrong, (
        f"{DOC.name} states the wrong tenant-table count; "
        f"len(EXPECTED_TENANT_RLS_TABLES) is {expected}. Wrong sites: "
        + "; ".join(f"line {ln} says {got} ({lbl})" for lbl, ln, got in wrong)
    )


def test_database_architecture_doc_states_the_real_tenant_table_count() -> None:
    """The sibling doc drifted BECAUSE this ratchet only covered one file.

    ``docs/multi_tenancy.md`` was corrected to 64 and stayed correct — it is
    gated. ``docs/database_architecture.md`` describes the *same* invariant, from
    the same ``EXPECTED_TENANT_RLS_TABLES``, and had drifted to **62** in three
    places with nothing to catch it. A gate scoped to one file leaves its
    siblings exactly as unprotected as they were before the gate existed, and
    the doc that names the same constant is the first place to look.
    """
    expected = len(EXPECTED_TENANT_RLS_TABLES)
    doc = DOC_DB_ARCH.read_text(encoding="utf-8")

    found: list[tuple[str, int, int]] = []
    for label, pattern in _DB_ARCH_COUNT_SITES:
        for m in pattern.finditer(doc):
            found.append((label, doc.count("\n", 0, m.start(1)) + 1, int(m.group(1))))

    assert len(found) >= _DB_ARCH_MIN_SITES, (
        f"only {len(found)} tenant-count sites matched in {DOC_DB_ARCH.name} but at "
        f"least {_DB_ARCH_MIN_SITES} are expected -- a claim was deleted or reworded "
        "past its pattern, which would leave this gate blind. Matched: "
        + ", ".join(f"{lbl}@L{ln}" for lbl, ln, _ in found)
    )

    wrong = [(lbl, ln, got) for lbl, ln, got in found if got != expected]
    assert not wrong, (
        f"{DOC_DB_ARCH.name} states the wrong tenant-table count; "
        f"len(EXPECTED_TENANT_RLS_TABLES) is {expected}. Wrong sites: "
        + "; ".join(f"line {ln} says {got} ({lbl})" for lbl, ln, got in wrong)
    )


def test_multi_tenancy_inventory_table_covers_every_tenant_table() -> None:
    """The Count column and the named tables in section 2c are the same claim."""
    expected = len(EXPECTED_TENANT_RLS_TABLES)
    section = _inventory_section(_doc_text())

    rows = re.findall(r"^\| \*\*[^|]+\*\* \| (\d+) \| ([^|]+) \|", section, re.M)
    assert rows, "section 2c inventory table has no parsable domain rows"

    per_domain_total = sum(int(count) for count, _ in rows)
    assert per_domain_total == expected, (
        f"section 2c Count column sums to {per_domain_total} but "
        f"len(EXPECTED_TENANT_RLS_TABLES) is {expected}"
    )

    listed = {name for _, names in rows for name in re.findall(r"`([a-z0-9_]+)`", names)}
    missing = sorted(set(EXPECTED_TENANT_RLS_TABLES) - listed)
    extra = sorted(listed - set(EXPECTED_TENANT_RLS_TABLES))
    assert not missing and not extra, (
        "section 2c inventory does not match EXPECTED_TENANT_RLS_TABLES. "
        f"In code but not documented: {missing or 'none'}. "
        f"Documented but not in code: {extra or 'none'}."
    )
