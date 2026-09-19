"""tests/unit/test_ruff_w605_ratchet.py — invalid escape sequences must fail lint.

An invalid escape sequence in a non-raw string literal (e.g. ``"\\|"``, meant as
a regex alternation but written without the ``r`` prefix) is only a
``DeprecationWarning`` on Python 3.10 -- every worktree in this estate runs
3.10, so it is invisible locally by construction. On 3.12 (the deployed
runtime; see ci.yml's own matrix comment) it is a hard ``SyntaxError``: the
module fails to import at all.

Cost lane E a full CI round TWICE the same night: a docstring in
``nce/vertical_modules/marketing/resources.py`` contained
``grep -rn "CASE_STUDY\\|TESTIMONIAL\\|CONTENT_ASSET"`` in a plain string. The
module failing to parse meant every AST-walking ratchet that reads it
collapsed too -- 19 failures that looked unrelated, from one character, on a
defect class a local `pytest` run on 3.10 could never surface.

``pyproject.toml``'s ``[tool.ruff.lint]`` already customises ``select`` (it is
NOT ruff's bare defaults) but did not include ``W605`` -- ``"E"`` covers
pycodestyle's E-prefixed codes only, not its separate W-prefixed ones. Fixed by
adding ``"W605"`` explicitly, additive to the existing list (confirmed
zero violations on this tree with `ruff check --select W605 .` before adding
it, run and reported before any fix was needed).
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"


def _ruff_lint_select() -> list[str]:
    """Extract [tool.ruff.lint]'s select list without a TOML parser -- this
    repo's floor is Python 3.10 (tomllib is 3.11+) and nothing else here
    depends on `tomli`. A plain regex on the one line that matters is simpler
    than adding a dependency for a single-file, single-key read.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^select\s*=\s*\[([^\]]*)\]', text)
    assert match, f"Could not find a `select = [...]` line under [tool.ruff.lint] in {_PYPROJECT}"
    return [
        item.strip().strip('"').strip("'") for item in match.group(1).split(",") if item.strip()
    ]


def test_w605_is_selected_in_ruff_config() -> None:
    select = _ruff_lint_select()
    assert "W605" in select, (
        "W605 (invalid-escape-sequence) is missing from [tool.ruff.lint].select in "
        f"{_PYPROJECT} -- this is what let a bad docstring escape ("
        "grep -rn \"CASE_STUDY\\|TESTIMONIAL\\|CONTENT_ASSET\") pass every 3.10 "
        "worktree and every un-selected lint run, then fail as a hard SyntaxError "
        "on the 3.12 CI job."
    )


def test_positive_control_invalid_escape_sequence_fails_ruff(tmp_path: pathlib.Path) -> None:
    """Proves W605 is actually wired into this repo's real ruff invocation, not
    merely present in a list nobody reads. Runs the real `ruff` binary against
    this repo's real pyproject.toml on a synthetic file containing exactly the
    defect class that broke marketing/resources.py -- mutation-proof by
    construction: this IS the real tool checking a real violation, not a
    re-implementation of ruff's own logic that could drift from it.
    """
    bad_file = tmp_path / "bad_escape.py"
    # The two characters backslash-pipe, unescaped, inside a plain string --
    # exactly the shape that broke marketing/resources.py's docstring.
    bad_file.write_text('pattern = "CASE_STUDY\\|TESTIMONIAL"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--config", str(_PYPROJECT), str(bad_file)],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert result.returncode != 0, (
        f"A file with an invalid escape sequence was NOT flagged by ruff -- W605 is "
        f"not actually enforced despite being in pyproject.toml.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "W605" in result.stdout, (
        f"ruff failed the file but not for W605 -- wrong rule caught it, or the rule "
        f"code changed.\nstdout: {result.stdout}"
    )


def test_positive_control_a_raw_string_with_the_same_pattern_is_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """The mirror case: the actual fix for this defect class (an `r"..."` raw
    string) must NOT be flagged -- proving W605 catches the real defect, not
    every backslash-pipe combination regardless of how it's written.
    """
    good_file = tmp_path / "good_escape.py"
    good_file.write_text('pattern = r"CASE_STUDY\\|TESTIMONIAL"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--config", str(_PYPROJECT), str(good_file)],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    assert result.returncode == 0, (
        f"A correctly-written raw string was flagged -- W605 is over-broad.\n"
        f"stdout: {result.stdout}"
    )
