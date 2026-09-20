"""
tests/test_golden_thread_seams_current.py
===========================================
Doc-gate ratchet for docs/_generated/golden_thread_seams.md.

Before this test, nothing in any workflow verified that the committed page
matches scripts/gen_golden_thread_seams.py's output for the current tree.
Unlike scripts/gen_engine_figures.py, that generator ships no ``--check``
mode, and a repo-wide grep for its name outside itself matches nothing --
the same "instrument exists, nothing runs it" shape as engine_figures.md
before ci.yml's D-GEN gate (see that gate's own comment in ci.yml). The
page's own header claims "it cannot go stale"; this test is what makes that
true rather than aspirational.

Pattern mirrors tests/test_docs_engine_guides_ratchet.py's surface.md check:
import the generator module directly (no subprocess spawn), regenerate
against ``--baseline HEAD`` (a git ref, not the working tree -- the
generator's own ``git_show()`` reads the committed blob), and compare,
normalising only the volatile stamp line and CRLF/LF line endings (CRLF
workspace, LF git blobs; the generator itself writes CRLF, see its
``main()``).

MLV16G wave, 2026-09-20 (v1.6 lane G is struck; this wave is unrelated
backfill-importer scope, dispatched directly by ML-orch to close a doc gate
nobody owned). Scope: this file only. Per the dispatch's explicit
constraints: does not touch ``nce/resource_surface/**`` or
``tests/integration/test_golden_thread.py`` (Lane H, live right now), does
not add a ``--check`` mode to the generator (regenerate-and-compare here
instead), and does not edit any ``.github/workflows/*.yml`` file (this test
runs in ci.yml's existing unconditional ``pytest tests/`` sweep with no
workflow change needed).
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SEAMS_DOC = _ROOT / "docs" / "_generated" / "golden_thread_seams.md"

# Mirrors gen_engine_figures.py's normalize_volatile() regex shape for the
# identical "> **Status:** ... **Verified-against:** ... **Last-audited:**
# ..." stamp line this generator's render() also emits (twice, per render()).
# Not imported from there: the two generators are independent scripts with
# no shared module, and the line text differs slightly ("generated" is a
# literal here, not a date) -- copying five lines is lower risk than adding
# a cross-script import for one regex.
_STAMP_RE = re.compile(r"> \*\*Status:\*\*.*?\*\*Last-audited:\*\* generated")


def _strip_stamp(text: str) -> str:
    return _STAMP_RE.sub("", text).strip()


def _load_generator():
    path = _ROOT / "scripts" / "gen_golden_thread_seams.py"
    spec = importlib.util.spec_from_file_location("gen_golden_thread_seams", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _regenerate(baseline: str = "HEAD") -> str:
    """Regenerate the page's content against `baseline` (a git ref, never WORKTREE).

    extract_steps() reads tests/integration/test_golden_thread.py's AST at that
    ref via git_show() -- this is what keeps the test reading exactly what CI
    would read (the committed tree), not any local edit in progress.
    """
    gen = _load_generator()
    steps = gen.extract_steps(str(_ROOT), baseline)
    sha = subprocess.run(
        ["git", "-C", str(_ROOT), "rev-parse", "--short", baseline],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return gen.render(steps, sha)


@pytest.mark.doc_gate
@pytest.mark.skipif(
    not _SEAMS_DOC.exists(), reason="docs/_generated/golden_thread_seams.md not present"
)
def test_golden_thread_seams_doc_matches_the_generator() -> None:
    expected = _regenerate("HEAD")
    actual = _SEAMS_DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert _strip_stamp(actual) == _strip_stamp(expected.replace("\r\n", "\n")), (
        "docs/_generated/golden_thread_seams.md does not match "
        "scripts/gen_golden_thread_seams.py's output for the current tree -- it was "
        "hand-edited, or a step's xfail marker changed in test_golden_thread.py "
        "without regenerating. Regenerate with:\n"
        "  python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD "
        "--out docs/_generated/golden_thread_seams.md"
    )


def test_golden_thread_seams_generator_is_sensitive_to_a_real_change() -> None:
    """Positive control (U18): prove the ratchet above is not vacuous.

    An instrument that has never been shown able to fail is not an
    instrument (K-0) -- five inert ones were found on this estate in two
    days. Rather than a tautological "mutated string != original string"
    check, this renders the real steps once, flips one step's xfail status,
    renders again, and asserts the *generator itself* produces different
    output -- proving render() is actually sensitive to the thing the ratchet
    claims to track, not silently emitting the same page regardless of input.
    """
    gen = _load_generator()
    steps = gen.extract_steps(str(_ROOT), "HEAD")
    assert steps, "no Golden Thread steps found -- test_golden_thread.py's step shape changed"

    baseline_render = gen.render(steps, "deadbee")

    mutated_steps = [dict(s) for s in steps]
    target = mutated_steps[0]
    target["xfail_reason"] = (
        None if target.get("xfail_reason") else "break-999: injected for this test only"
    )
    mutated_render = gen.render(mutated_steps, "deadbee")

    assert mutated_render != baseline_render, (
        "gen_golden_thread_seams.py's render() produced identical output after flipping "
        "one step's xfail status -- the ratchet above would never go red no matter how "
        "stale the committed doc gets."
    )
