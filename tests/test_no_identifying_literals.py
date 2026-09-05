"""Reintroduction gate: no customer, personal or private-source identifier may
reach the public tree.

WHY THIS EXISTS
---------------
This repository is public. Three separate identifiers were removed from it and
two of them came straight back within hours, because a *port* from the private
tree replays whatever the private tree still contains:

* ``steps-ai`` — removed, then reintroduced by a tree port, then removed again.
* the named customer — removed, then reintroduced by a module port three hours
  later, then removed again.
* 25 private TypeScript source paths in "ported from" docstrings — never
  scrubbed at all, because every scan looked for *company names* and a path is
  not a name.

Each was caught by a person happening to look. None was caught by a check. A
scrub is a one-time event; a port is a recurring one, so the scrub loses. This
test converts "please remember to scrub" into "CI will not let it merge".

THE RULE THAT MATTERS MOST
--------------------------
**The occurrence is the unit, not the literal.** Three times a naive
literal sweep would have broken the build:

* ``Sitter`` — ten of eleven hits are ``tree-sitter``, the parser library and
  its dependency pins.
* ``trimcp`` — appears inside ``007_rename_db_roles.sql``, the migration whose
  entire job is to rename those legacy roles away.
* ``steps-ai`` — the hyphenated form was scrubbed while ``steps_product`` and
  ``steps_d365`` survived untouched.

So every pattern below carries explicit allowances, and each allowance states
*why* it is safe rather than merely listing a path. An allowance that cannot
explain itself is a finding, not an exemption.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# Banned patterns. Each entry: (label, compiled regex, allowances)
#
# An allowance is (path_suffix, reason). The reason is not decoration: it is
# what a reviewer checks. "It is in this file" is not a reason; "this file is
# the migration that removes the legacy name" is.
# --------------------------------------------------------------------------
BANNED: list[tuple[str, re.Pattern[str], tuple[tuple[str, str], ...]]] = [
    (
        "named customer",
        re.compile(r"veidekke", re.I),
        (),
    ),
    (
        "customer hardware (identifying in context)",
        re.compile(r"\b(M4350|PR460X)\b", re.I),
        (),
    ),
    (
        "private fork name",
        re.compile(r"steps[-_]ai\b", re.I),
        (),
    ),
    (
        "private fork module names",
        re.compile(r"\b(steps_product|steps_d365|agreement_sidecar|hr_sidecar|lysning)\b", re.I),
        (),
    ),
    (
        "planning-corpus owner",
        re.compile(r"\bAndreas\b", re.I),
        (
            (
                "nce/config_data/asset-lifecycle.json",
                "config_data is frozen pending a ruling on whether this business "
                "configuration belongs in a public repo at all; scrubbing it "
                "piecemeal would split one decision across two places",
            ),
            (
                "nce/config_data/finago-account-mapping.json",
                "same frozen config_data ruling",
            ),
            (
                "nce/config_data/procurement-tolerances.json",
                "same frozen config_data ruling",
            ),
        ),
    ),
    (
        "private TypeScript source paths",
        re.compile(r"\blib/[A-Za-z0-9_./-]+\.ts\b"),
        (
            (
                "nce/config_data/finago-account-mapping.json",
                "false positive: matches prose inside a _comment field, not a "
                "source path; the file is frozen under the config_data ruling",
            ),
        ),
    ),
]

# Binary and vendored paths are searched by git but are not prose we control.
SKIP_SUFFIXES = (".png", ".jpg", ".gif", ".ico", ".pdf", ".exe", ".dll", ".so", ".lock")

# This file necessarily contains every banned literal in order to test for them.
SELF = "tests/test_no_identifying_literals.py"


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\n")
    return [f for f in out if f and not f.endswith(SKIP_SUFFIXES) and f != SELF]


def _read(path: str) -> str | None:
    try:
        return (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return None


# Patterns whose occurrences are known-outstanding and owned by another wave.
# strict=True: when that wave lands, this XPASSes and CI FAILS until the marker
# is removed -- so the gate cannot silently soften, which is exactly how the
# project's other allowlists decayed.
OUTSTANDING = {
    "private fork module names": (
        "lysning / steps_product / steps_d365 / agreement_sidecar / hr_sidecar are "
        "owned by the trimcp rename wave (they sit alongside build-file renames that "
        "must be CI-gated together, not text-swept). Remove this marker when that "
        "wave lands -- strict=True will force it."
    ),
}


def _case(entry):
    label = entry[0]
    if label in OUTSTANDING:
        return pytest.param(*entry, marks=pytest.mark.xfail(strict=True, reason=OUTSTANDING[label]))
    return pytest.param(*entry)


@pytest.mark.parametrize(
    "label,pattern,allowances", [_case(b) for b in BANNED], ids=[b[0] for b in BANNED]
)
def test_no_identifying_literal_reaches_the_public_tree(
    label: str, pattern: re.Pattern[str], allowances: tuple[tuple[str, str], ...]
) -> None:
    """Fail with the file, line and text so the fix is obvious, not a hunt."""
    allowed = {p for p, _ in allowances}
    findings: list[str] = []

    for path in _tracked_files():
        if path in allowed:
            continue
        text = _read(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                findings.append(f"    {path}:{lineno}: {line.strip()[:110]}")

    assert not findings, (
        f"\n\n{len(findings)} occurrence(s) of a banned {label} reached the tree.\n"
        f"This repository is PUBLIC.\n\n" + "\n".join(findings[:40]) + "\n\n"
        "If this arrived via a port from the private tree, scrub the PRIVATE tree too "
        "-- otherwise the next port reintroduces it. If it is a genuine false positive, "
        "add an allowance to BANNED in this file WITH A REASON that a reviewer can "
        "check; a bare path is not a reason.\n"
    )


def test_the_gate_itself_can_fail() -> None:
    """A gate that cannot fail proves nothing.

    Every banned pattern is exercised against text that must match it. If a
    pattern is mistyped into something unmatchable, this test goes red rather
    than the suite going quietly green forever.
    """
    probes = {
        "named customer": "the Veidekke pilot",
        "customer hardware (identifying in context)": "switch M4350 and PR460X",
        "private fork name": "ported from the steps-ai fork",
        "private fork module names": "backend/steps_product and src/lysning/pages",
        "planning-corpus owner": "from Andreas's reference",
        "private TypeScript source paths": "see lib/finance/events/emit.ts",
    }
    assert set(probes) == {label for label, _, _ in BANNED}, (
        "BANNED and the probe set have diverged -- a pattern was added or renamed "
        "without a probe, so it would never be proven capable of matching."
    )
    for label, pattern, _ in BANNED:
        assert pattern.search(probes[label]), (
            f"pattern for {label!r} failed to match its own probe -- the pattern is "
            f"broken and this gate would pass over real occurrences"
        )


def test_every_allowance_states_a_reason() -> None:
    """An exemption that cannot explain itself is a finding, not an exemption.

    This is the property the project's other allowlists lacked: entries were
    bare paths, so nobody could tell an examined exemption from an unexamined
    one, and a real defect hid behind one for days.
    """
    for label, _, allowances in BANNED:
        for path, reason in allowances:
            assert len(reason.strip()) >= 30, (
                f"allowance {path!r} under {label!r} has no usable reason. State why "
                f"the occurrence is safe, not merely that it exists."
            )
