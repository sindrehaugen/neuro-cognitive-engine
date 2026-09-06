"""Unit tests and standing positive controls for scripts/check_empty_with.py.

Audited and hardened under Phase 4 Wave T-5 (Ratchets with Holes).

Scope & Unobserved Surfaces (T-5 Q3):
1. What this ratchet CANNOT see:
   - Dynamic context managers evaluated via exec/eval or loaded dynamically.
   - Context managers whose enter/exit methods have side effects outside the body,
     where an empty body was intentionally designed (rare, discouraged pattern).
   - Only checks files passed on CLI or default paths (nce, server.py, admin_server.py, start_worker.py).
2. What it does when it fires wrongly (T-5 Q2):
   - Previously: Swallowed syntax/read errors silently.
   - Hardened: Reports SYNTAX_ERROR / FILE_READ_ERROR as loud violations (exit code 1).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_empty_with import check_file, main


def test_detects_empty_sync_with_pass(tmp_path: Path) -> None:
    test_file = tmp_path / "empty_sync.py"
    test_file.write_text(
        "with open('foo'):\n    pass\n",
        encoding="utf-8",
    )
    violations = check_file(test_file)
    assert len(violations) == 1
    assert "EMPTY_WITH: with" in violations[0].message
    assert violations[0].line == 1


def test_detects_empty_async_with_ellipsis(tmp_path: Path) -> None:
    test_file = tmp_path / "empty_async.py"
    test_file.write_text(
        "async def run():\n    async with session() as s:\n        ...\n",
        encoding="utf-8",
    )
    violations = check_file(test_file)
    assert len(violations) == 1
    assert "EMPTY_WITH: async with" in violations[0].message
    assert violations[0].line == 2


def test_detects_docstring_only_with(tmp_path: Path) -> None:
    test_file = tmp_path / "docstring_only.py"
    test_file.write_text(
        "with lock:\n    '''just a docstring'''\n",
        encoding="utf-8",
    )
    violations = check_file(test_file)
    assert len(violations) == 1
    assert "EMPTY_WITH: with lock" in violations[0].message


def test_passes_on_non_empty_with(tmp_path: Path) -> None:
    test_file = tmp_path / "valid_with.py"
    test_file.write_text(
        "with open('foo') as f:\n    data = f.read()\n",
        encoding="utf-8",
    )
    violations = check_file(test_file)
    assert violations == []


# ---------------------------------------------------------------------------
# Phase 4 Wave T-5 Standing Positive Controls (U18 / T-5 Q1 & Q2)
# ---------------------------------------------------------------------------


def test_positive_control_empty_with_exit_code_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standing positive control (U18 / T-5 Q1): prove main() exits 1 on empty with."""
    test_file = tmp_path / "offender.py"
    test_file.write_text(
        "with context_manager():\n    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["check_empty_with.py", str(test_file)])
    exit_code = main()
    assert exit_code == 1, f"Expected exit code 1 on empty with, got {exit_code}"


def test_positive_control_syntax_error_reported_loudly(
    tmp_path: Path,
) -> None:
    """Standing positive control (U18 / T-5 Q2): prove syntax error is not silently swallowed."""
    test_file = tmp_path / "broken_syntax.py"
    test_file.write_text(
        "def bad_syntax(\n",
        encoding="utf-8",
    )
    violations = check_file(test_file)
    assert len(violations) == 1
    assert "SYNTAX_ERROR" in violations[0].message
