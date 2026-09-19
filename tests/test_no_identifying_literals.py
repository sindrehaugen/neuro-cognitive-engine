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

H-10, HASHED HOST TOKENS
-------------------------
``BANNED`` above works because every pattern is safe to publish: a customer name,
a hardware model, a fork name. **A private host-repo module or directory name is
different in kind** -- writing it into ``BANNED`` as plaintext, to ban it, would
publish the exact private-source-tree structure the ban exists to keep out of a
public repo. The instrument would leak the thing it protects.

``_BANNED_HOST_TOKEN_HASHES`` stores ``sha256(lowercased token)`` instead of the
token itself. ``_candidate_tokens()`` extracts every identifier-like run from a
line of tracked source (and every contiguous ``/``-joined sub-slice of it, so a
bare filename and a fully-qualified path both produce the same hash regardless
of which one appears), hashes each candidate, and checks it against the set. A
match is reported by file and line number only -- **never the matched text or
the source line**, because this repository's CI logs are public: printing the
match would republish the secret somewhere this file's own plaintext BANNED list
does not (BANNED's patterns are already in the source, so echoing them back
costs nothing extra; a hashed token's whole point is that it is nowhere in the
source, including in a CI log).

Every token below was verified against the citing lane's own ledger or a merged
PR's own diff before being added -- never taken from a summary or a peer's
transcription (S1). Two candidates from the same brief were deliberately left
out: see ``test_fx_and_vaer_are_deliberately_not_banned`` for why.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().lower().encode("utf-8")).hexdigest()


# Host module/directory/filename tokens (H-10), hashed -- see module docstring.
# 14 tokens: 2 private host source-tree directory names and 12 distinctive
# private host module/ADR basenames, cited by Lane F while reading the host
# tree for shape (its own ledger entries and/or a merged PR's own diff). Each
# was verified against the citing lane's own report before being hashed here --
# never taken from a summary or a peer's transcription (S1).
#
# Deliberately NOT plaintext anywhere in this file, including in comments: the
# full token list, the citing evidence for each, and the one candidate from the
# original brief that was investigated and deliberately excluded (a public
# vendor name that turned out to have 18 legitimate, non-identifying uses
# already in this tree -- see test_no_banned_host_token_reaches_the_public_tree's
# docstring for the general shape of that finding) are recorded in the lane's
# own private ledger, not committed here. Writing any of it into an explanatory
# comment in this public file would be the exact leak this file exists to
# prevent -- an earlier draft of this comment did precisely that before this
# wave caught and rewrote it.
_BANNED_HOST_TOKEN_HASHES: frozenset[str] = frozenset(
    {
        "47d7b97c5ac22bf60f6dbe7463d026a64bf29dd50a000a17d06ea3de114bc5b5",
        "62686f49010afcd7408ceac4029975eaa15bfa5ea591896a600fe663975455b6",
        "043d5adf80c2d7693c322f6319c5264b6c8081ef2ebd6138041a039e2e4508bd",
        "5bff5c79b8861b45115332ee3fe714bd16b8e73d5d4c17233ec049ea0788c9e7",
        "b576f05666094443b4f1ffdcd8623079a6b86679fa45f4818fa65b430099240a",
        "94d57a8c81b288511812e6e654acf692fe746c6aad091e6ec5ee040a77614267",
        "221c2235699efe7327b163657eec7f50baa81b75d2cc0571480eec96898b6450",
        "0be57de0cf9c41ada63492dec1c7fd7a24b5d9edf9ff1cfbbc60a7f92006bc6a",
        "84e389c7ffc67b5ed47c02a3c4e81af5391383d737aab10152cda9d17d7faf13",
        "21711cc15461e8b6145ab596aaa33b8e5b9543ce498eb4e079f74d6a81fe7984",
        "4857734b150e5caf18dee19e263b02a81c50255340520d693a1605f7dbdc68f9",
        "52576dd26cc7888b1f34bfe893a5a9a0651c6742e1fe9afbcf6c1337a30be962",
        "551bcb1b66befb74b479d0e5f0ed72ee130b4e4b620f965d0217bbd9c35118fc",
    }
)

# Test-only probe hashes -- deliberately fake tokens, safe to publish, used only
# to prove the tokenizer + hash-matching machinery actually works (see
# test_the_hashed_gate_itself_can_fail). Never overlaps _BANNED_HOST_TOKEN_HASHES.
_PROBE_TOKEN_HASHES: frozenset[str] = frozenset({_token_hash("synthetic-h10-fake-hosttoken-zzz9")})


def _candidate_tokens(line: str) -> set[str]:
    """Extract every identifier/path-like candidate token from a line, lowercased.

    A single maximal run of ``[A-Za-z0-9_./-]`` is split on ``/`` and ``.`` into
    segments, and every contiguous slice of segments (rejoined with ``/``) is a
    candidate. This lets one hash set catch a token whether it appears as a bare
    filename (``example_module.py`` -> segments include ``example_module``), a
    qualified path under a directory (``some/dir/example_module.py`` -> segments
    include ``some/dir``), or a plain identifier with no path at all. (Using a
    fake example here deliberately -- an illustration built from a real banned
    token would itself be the leak this file exists to prevent.)
    """
    candidates: set[str] = set()
    for run in re.finditer(r"[A-Za-z][A-Za-z0-9_./-]*[A-Za-z0-9]", line):
        text = run.group(0).lower()
        candidates.add(text)
        parts = [p for p in re.split(r"[/.]", text) if p]
        for i in range(len(parts)):
            for j in range(i + 1, len(parts) + 1):
                candidates.add("/".join(parts[i:j]))
    return candidates


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RED by design, not a bug in the check: running this for real found 13 "
        "genuine occurrences, all shaped by the same real tension, not 13 unrelated "
        "leaks. Most are Lane F's own Q-25 AGPL-compliance disclaimers ('not a port "
        "of the host's <module>.py', 'read for shape only') -- the exact citation "
        "that PROVES independent re-implementation is also a citation of the private "
        "path. Banning it outright would break an ML-orch-endorsed, multi-PR-wide "
        "compliance convention; allowing it silently would defeat H-10. One "
        "occurrence is materially different and more serious: a pre-charter file "
        "says code was 'lifted from' the private tree, not read for shape -- a "
        "possible real Q-25 violation, not just a naming leak, flagged separately "
        "and urgently to ML-orch/Sindre rather than folded into this count. "
        "Left RED (not silently allowlisted) until that gets a ruling: XPASS "
        "fails CI the day either the disclaimer convention gets a stated exception "
        "shape or the tree is actually scrubbed -- whichever the ruling picks."
    ),
)
def test_no_banned_host_token_reaches_the_public_tree() -> None:
    """H-10: no hashed host module/directory/filename token may reach the tree.

    Never prints the matched text or the source line -- see module docstring on
    hashed tokens for why (this repository's CI logs are public).
    """
    findings: list[str] = []
    for path in _tracked_files():
        text = _read(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            candidates = {_token_hash(tok) for tok in _candidate_tokens(line)}
            if candidates & _BANNED_HOST_TOKEN_HASHES:
                findings.append(f"    {path}:{lineno}")

    assert not findings, (
        f"\n\n{len(findings)} occurrence(s) of a banned host-identifying token "
        "reached the tree (H-10). This repository is PUBLIC. Content withheld from "
        "this message -- CI logs are public too. Location(s):\n"
        + "\n".join(findings[:40])
        + "\n\nOpen each file at the given line locally to see what matched. If it "
        "arrived via a port from the private tree, scrub the private tree too."
    )


def test_the_hashed_gate_itself_can_fail() -> None:
    """A gate that cannot fail proves nothing (same property as test_the_gate_itself_can_fail,
    exercised through the hash path instead of the plaintext-regex path).

    The probe directory below is entirely synthetic -- it deliberately does not
    reuse any real banned token, even as a "realistic-looking" example, since
    that would risk the exact plaintext-leak-via-comment mistake this test
    exists to keep out of the committed file.
    """
    probe_line = "see synthetic-h10-probe-dir/synthetic-h10-fake-hosttoken-zzz9.py for shape"
    candidates = {_token_hash(tok) for tok in _candidate_tokens(probe_line)}
    assert candidates & _PROBE_TOKEN_HASHES, (
        "The hashed-token tokenizer did not extract and match its own safe probe token "
        "-- this gate would silently pass over real occurrences."
    )
    assert not (candidates & _BANNED_HOST_TOKEN_HASHES), (
        "Positive-control fixture bug: the probe line accidentally also matched a real "
        "banned host token."
    )


def test_probe_and_banned_host_token_hashes_do_not_overlap() -> None:
    """Guard against a copy-paste mistake making the positive control vacuous."""
    assert not (_PROBE_TOKEN_HASHES & _BANNED_HOST_TOKEN_HASHES)


def test_fx_and_vaer_are_deliberately_not_banned() -> None:
    """Documents a real decision, not an oversight: 'fx.py' and 'vaer.py' were in
    the evidenced host-token brief (Lane F's own PR #260 diff cites both) but are
    NOT banned here.

    Both are too common/short to be identifying on their own -- 'fx' is an
    ordinary finance abbreviation and 'vaer' is the ordinary Norwegian word for
    weather -- and NCE has its own, legitimate, already-merged
    ``nce/pricing/fx.py`` (Wave F-15) that a bare-token ban on 'fx' would
    immediately false-positive on. Unlike the tokens actually hashed above,
    which are distinctive enough that their only plausible source in this tree
    is a direct port, 'fx' and 'vaer' need their private host directory prefix
    to be identifying at all, and that directory is already banned on its own.
    """
    assert (REPO_ROOT / "nce" / "pricing" / "fx.py").exists(), (
        "This test's own reasoning depends on nce/pricing/fx.py being real NCE code, "
        "not a hypothetical -- re-derive the decision if this file has moved or gone."
    )
    assert _token_hash("fx") not in _BANNED_HOST_TOKEN_HASHES
    assert _token_hash("vaer") not in _BANNED_HOST_TOKEN_HASHES


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
OUTSTANDING: dict[str, str] = {}


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
