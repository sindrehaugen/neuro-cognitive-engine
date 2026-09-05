"""Every repo-anchored `path:line` citation in ``docs/engines/`` must resolve (**D52**).

``docs/engines/`` is **24 files** and was gated by nothing: grepping `tests/`,
`scripts/` and `.github/` for any of their names returned zero. That is the same
shape as D14 (`verify_docs_links_and_syntax.py` run by nothing) and D19
(`multi_tenancy.md` gated by nothing), and it is why `sales-user.md` still
documented a DealRoom return shape that PR #187 had removed -- in the file an
integrator builds against -- for a full PR cycle.

🔴 **Be precise about what this gate does and does not do, because overselling a
gate is how it gets switched off.**

It catches exactly one thing: a citation whose *file* is gone, or whose *line
range runs past the end of that file*. Measured when it was written: 58
repo-anchored citations, **0 missing files, 4 past EOF** -- `economy.py:196-270`
against a 260-line file, `write_routing.py:26-178` against 177,
`from_quote.py:256-426` against 421, and `validation_queries.py:561-647` against
475. All four were real drift and all four are fixed in the same commit.

It does **NOT** catch semantic drift -- a citation that still resolves while the
prose around it describes behaviour the code no longer has. That was D51's defect
and no cheap gate finds it. Nothing here should be read as a claim that these
docs are correct.

**Scope: repo-anchored citations only.** ``docs/engines/`` also carries ~79 bare
filename citations (`peppol.py:47`, `matching.py:532`) that name no directory.
Those are unresolvable by construction, not stale -- a first pass at this
measurement counted all 79 as broken and was wrong. They are left alone
deliberately: a gate that guesses which `events.py` was meant would be a gate
that reports drift where there is none.

Guard-the-guard: ``_MIN_CITATIONS`` fails loudly if the pattern stops matching,
because zero citations all vacuously resolve.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_ENGINE_DOCS = _REPO / "docs" / "engines"

#: Top-level directories that make a citation repo-anchored, i.e. resolvable
#: without guessing. A bare `foo.py:12` names no directory and is out of scope.
_ROOTS = (
    "nce/",
    "tests/",
    "docs/",
    "scripts/",
    "src/",
    "admin/",
    "vertical_modules/",
    "_internal/",
    "go/",
    "deploy/",
)

_CITATION_RE = re.compile(
    r"`((?:" + "|".join(re.escape(r) for r in _ROOTS) + r")[A-Za-z0-9_./-]*"
    r"\.(?:py|sql|json|yml|yaml|md|ts|tsx|go|html)):(\d+)(?:[-–,](\d+))?`"
)

# Measured: 58 repo-anchored citations across the 24 engine docs. Only ever
# grows as these guides are extended; a drop means the pattern stopped matching,
# and every citation then vacuously resolves.
_MIN_CITATIONS = 45


def _citations() -> list[tuple[Path, str, int, int]]:
    """(doc, cited path, first line, last line) for every repo-anchored citation."""
    found: list[tuple[Path, str, int, int]] = []
    for doc in sorted(_ENGINE_DOCS.glob("*.md")):
        text = doc.read_text(encoding="utf-8")
        for m in _CITATION_RE.finditer(text):
            lo = int(m.group(2))
            hi = int(m.group(3)) if m.group(3) else lo
            found.append((doc, m.group(1), lo, hi))
    return found


def test_engine_docs_are_present() -> None:
    """The directory this gate exists for must actually be there."""
    docs = sorted(_ENGINE_DOCS.glob("*.md"))
    assert len(docs) >= 20, (
        f"only {len(docs)} *.md files under {_ENGINE_DOCS} -- expected the 24 engine "
        "guides. A moved or renamed directory would leave this gate scanning nothing."
    )


def test_citation_discovery_floor() -> None:
    """A silenced pattern finds nothing, and nothing vacuously resolves."""
    found = _citations()
    assert len(found) >= _MIN_CITATIONS, (
        f"only {len(found)} repo-anchored path:line citations matched across "
        f"{_ENGINE_DOCS} but at least {_MIN_CITATIONS} are expected -- the citation "
        "pattern is not matching, which would leave the check below vacuously green."
    )


def test_every_cited_file_exists() -> None:
    offenders = [
        f"{doc.name} cites `{path}:{lo}` -- no such file"
        for doc, path, lo, _hi in _citations()
        if not (_REPO / path).is_file()
    ]
    assert not offenders, "\n  ".join(["stale citations in docs/engines/:", *offenders])


def test_no_citation_runs_past_the_end_of_its_file() -> None:
    """A range whose end is past EOF is drift, unambiguously and cheaply."""
    offenders: list[str] = []
    line_counts: dict[str, int] = {}
    for doc, path, lo, hi in _citations():
        target = _REPO / path
        if not target.is_file():
            continue  # reported by the sibling test; do not double-count
        if path not in line_counts:
            line_counts[path] = len(target.read_text(encoding="utf-8").splitlines())
        n = line_counts[path]
        if hi > n:
            offenders.append(f"{doc.name} cites `{path}:{lo}-{hi}` but that file has {n} lines")
    assert not offenders, "\n  ".join(["citations past end-of-file in docs/engines/:", *offenders])
