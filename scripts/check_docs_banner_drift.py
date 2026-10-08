"""
scripts/check_docs_banner_drift.py
=====================================
Computes whether each doc's `**Verified-against:** <sha>` banner claim still
holds, instead of trusting it. Reuses the method lane E used by hand in
`_internal/work-docs/mlv16-orchestration/DOCS_PAGES_STALENESS_MEASUREMENT_2026-09-21.md`
(measured `main=968d803`: 130 banner-carrying docs, 77 with a dead SHA, 45
genuinely stale by drift, 4 not assessable, 1 current) -- this script is that
one-off measurement turned into something that runs again.

THE BANNER, AND WHY THE DATE FIELD IS NOT TRUSTED
-----------------------------------------------------
`> **Status:** ... · **Verified-against:** <sha> (<branch>) · **Last-audited:**
<date>`. E's measurement found `ENGINE_STATUS.md` and `docs/adr/
0008-append-only-ticket-action-log.md` both claim TODAY's date and are both
already stale -- the date is when someone looked, not evidence the doc is
still true. Only the SHA-to-current-paths comparison below is trusted.

THREE CATEGORIES, NOT ONE PASS/FAIL
---------------------------------------
- MISSING_SHA: the claimed SHA does not resolve in this clone's history
  (typically a pre-squash commit, unresolvable by design -- 77 of 130 docs
  claim the exact same dead SHA, `7304330`, per E's measurement: one root
  cause, not 77 separate problems). This is NOT the same as clean and must
  never be reported as such.
- NOT_ASSESSABLE: the doc cites no file-level source path this script can
  check (a bare directory reference does not count) -- reported by name, not
  silently dropped.
- Resolvable: `git diff --name-only <claimed-sha>..<against> -- <cited
  paths>` decides STALE (something changed) vs CURRENT (nothing did).

Docs under `docs/_generated/` are already covered by their own generator
`--check` flags (or, where no `--check` exists, a regenerate-and-diff pytest
under the `doc_gate` marker) -- see this same workflow's other steps and
`tests/test_golden_thread_seams_current.py` / `test_engine_status_current.py`.
Those generators stamp their own output's `Verified-against` with the SHA
that was current AT GENERATION TIME, so by the time this script runs the doc
looks "just-claimed-but-already-behind" by construction -- not drift, the
generator's own convention (E's report calls this out explicitly). Rather
than special-case the path, this script's date-field pattern requires an
ISO date; those docs write `**Last-audited:** generated` instead of a date,
so they fail the banner regex and are silently excluded from this script's
130-doc population -- the same population E measured by hand, reproduced
mechanically here.

NON-BLOCKING, ON PURPOSE
----------------------------
This script's exit code is always 0. 130 docs against a moving `main` would
be permanently red if wired as a gate -- the same Q-17 reasoning already
governing `docs/_generated/waves_landed.md`'s advisory-only pytest
counterpart in this same workflow file, and a permanently-red check is worth
less than no check at all. This is a report, run nightly
(`.github/workflows/doc-drift-nightly.yml`), not a per-PR gate -- wiring it
into `ci.yml` was explicitly rejected for exactly that reason.

This script never edits a banner or rewrites a SHA. Rewriting 77 claims to
look current would assert an audit nobody performed, on a public site.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO, "docs")

# `**Status:** shipped · **Verified-against:** becc2ff (main) · **Last-audited:** 2026-09-18`
# Some docs carry an inline caveat between the branch and the audit date, e.g.
# `(main) -- **section 10 patched only, see warning there** · **Last-audited:**
# ...` (docs/engines/system-design-admin.md) -- the non-greedy `.*?` absorbs it
# without needing to parse it. The date group requires an ISO date on purpose:
# generator-stamped docs write `**Last-audited:** generated` instead, which
# deliberately fails to match (see module docstring).
BANNER_RE = re.compile(
    r"\*\*Status:\*\*\s*(?P<status>[^\n·]+?)\s*·\s*"
    r"\*\*Verified-against:\*\*\s*`?(?P<sha>[0-9a-f]{7,40})`?\s*\((?P<branch>[^)]+)\)"
    r"(?:\s*[—-]\s*.*?)?\s*·\s*"
    r"\*\*Last-audited:\*\*\s*(?P<date>\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# File-level source paths cited in a doc's prose -- a bare directory mention
# (no filename) does not match on purpose; E spot-checked that miss as real,
# not a regex bug (docs/vertical_engines/02-product-engine.md).
PATH_RE = re.compile(r"\b(?:nce|scripts|tests)/[A-Za-z0-9_\-./]+\.(?:py|sql|ya?ml|json)\b")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", REPO, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _sha_resolves(sha: str) -> bool:
    return _git("rev-parse", "--verify", f"{sha}^{{commit}}").returncode == 0


def _changed_paths(sha: str, against: str, paths: list[str]) -> list[str]:
    res = _git("diff", "--name-only", f"{sha}..{against}", "--", *paths)
    if res.returncode != 0:
        # A path git itself doesn't recognise (renamed/deleted before `sha`,
        # or a typo in the doc's own prose) -- treat as changed rather than
        # silently dropping it; a doc citing a dead path is not "current".
        return list(paths)
    return [line for line in res.stdout.splitlines() if line.strip()]


@dataclass
class DocResult:
    path: str
    sha: str
    branch: str
    date: str
    category: str
    cited: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def _iter_docs() -> list[str]:
    out = []
    for root, _dirs, files in os.walk(DOCS_DIR):
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.join(root, f))
    return sorted(out)


def measure(against: str) -> list[DocResult]:
    results: list[DocResult] = []
    for abs_path in _iter_docs():
        rel_path = os.path.relpath(abs_path, REPO).replace("\\", "/")
        with open(abs_path, encoding="utf-8") as fh:
            content = fh.read()

        m = BANNER_RE.search(content)
        if not m:
            continue  # no three-field banner -- not part of this population

        sha, branch, date = m.group("sha"), m.group("branch"), m.group("date")
        cited = sorted(set(PATH_RE.findall(content)))

        if not _sha_resolves(sha):
            results.append(DocResult(rel_path, sha, branch, date, "MISSING_SHA", cited))
            continue

        if not cited:
            results.append(DocResult(rel_path, sha, branch, date, "NOT_ASSESSABLE", cited))
            continue

        changed = _changed_paths(sha, against, cited)
        category = "STALE" if changed else "CURRENT"
        results.append(DocResult(rel_path, sha, branch, date, category, cited, changed))

    return results


def render(results: list[DocResult], against: str) -> str:
    lines: list[str] = []
    lines.append("# Doc banner drift report")
    lines.append("")
    lines.append(
        f"Computed against `{against}`. {len(results)} docs carry the three-field "
        "`Verified-against` banner; this is the checkable population, not every "
        "doc in `docs/`."
    )
    lines.append("")

    stale = [r for r in results if r.category == "STALE"]
    missing = [r for r in results if r.category == "MISSING_SHA"]
    not_assessable = [r for r in results if r.category == "NOT_ASSESSABLE"]
    current = [r for r in results if r.category == "CURRENT"]

    lines.append(
        f"**{len(stale)} STALE | {len(missing)} MISSING_SHA (claimed SHA does not "
        f"resolve -- not the same as clean) | {len(not_assessable)} NOT_ASSESSABLE "
        f"(no file-level path cited) | {len(current)} CURRENT**"
    )
    lines.append("")

    if stale:
        lines.append("## STALE -- ranked by changed/cited paths, highest first")
        lines.append("")
        lines.append("```")
        for r in sorted(stale, key=lambda r: (-len(r.changed), -len(r.cited), r.path)):
            lines.append(f"{r.path:<70} {r.sha} / {r.date}   {len(r.changed)}/{len(r.cited)}")
        lines.append("```")
        lines.append("")

    if missing:
        lines.append("## MISSING_SHA -- claimed SHA unresolvable (not clean, not assessable as-is)")
        lines.append("")
        lines.append("```")
        for r in sorted(missing, key=lambda r: r.path):
            lines.append(f"{r.path:<70} {r.sha}")
        lines.append("```")
        lines.append("")

    if not_assessable:
        lines.append("## NOT_ASSESSABLE -- no file-level path cited")
        lines.append("")
        lines.append("```")
        for r in sorted(not_assessable, key=lambda r: r.path):
            lines.append(f"{r.path:<70} {r.sha} / {r.date}")
        lines.append("```")
        lines.append("")

    if current:
        lines.append("## CURRENT")
        lines.append("")
        lines.append("```")
        for r in sorted(current, key=lambda r: r.path):
            lines.append(
                f"{r.path:<70} {r.sha} / {r.date}   {len(r.cited)} cited paths, none changed"
            )
        lines.append("```")
        lines.append("")

    lines.append(
        "_Report only -- no banner is edited by this script. A `MISSING_SHA` or "
        "`STALE` doc needs a human to re-audit and restamp it; rewriting the SHA "
        "here would assert an audit nobody performed."
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--against",
        default="HEAD",
        help="Git ref to diff each doc's claimed SHA against (default: HEAD).",
    )
    args = parser.parse_args()

    results = measure(args.against)
    report = render(results, args.against)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    # Advisory only -- see module docstring. Never fails the job.
    return 0


if __name__ == "__main__":
    sys.exit(main())
