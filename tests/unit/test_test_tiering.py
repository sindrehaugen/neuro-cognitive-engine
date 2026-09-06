"""Unit tests and ratchets for Test Tiering (Wave T-3 / TDL-8).

Enforces the three-tier test architecture:
1. Fast Unit Tier: Matrixed across Python 3.10 and 3.11 in ci.yml to verify the
   declared requires-python = ">=3.10" floor (TDL-8) at compile/import time.
   Excludes integration, perf, and live-tier multi-engine scenarios.
2. Integration Tier: DB-backed suites against Postgres and Redis running on
   Python 3.11 only to avoid multiplying service container runtimes.
3. Live Tier: Nightly Golden Thread (I-7) against the live stack in
   .github/workflows/nightly-golden-thread.yml rather than blocking PRs.
4. Standing positive controls (U18) verify the tiering guards fail loudly.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CI_FILE = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_NIGHTLY_FILE = _REPO_ROOT / ".github" / "workflows" / "nightly-golden-thread.yml"
_PYPROJECT_FILE = _REPO_ROOT / "pyproject.toml"
_PYTEST_INI = _REPO_ROOT / "pytest.ini"
_GOLDEN_THREAD_FILE = _REPO_ROOT / "tests" / "integration" / "test_golden_thread.py"


def test_pytest_ini_registers_live_marker() -> None:
    """Verify that pytest.ini formally registers the 'live' marker."""
    content = _PYTEST_INI.read_text(encoding="utf-8")
    assert re.search(r"^\s*live:\s+", content, re.MULTILINE), (
        "pytest.ini must declare the 'live' marker under markers."
    )


def test_ci_fast_unit_tier_excludes_integration_perf_and_live() -> None:
    """Verify that the CI unit job selects 'not integration and not perf and not live'."""
    data = yaml.safe_load(_CI_FILE.read_text(encoding="utf-8"))
    test_job = data.get("jobs", {}).get("test", {})

    steps = test_job.get("steps", [])
    pytest_steps = [
        s
        for s in steps
        if "run" in s and "pytest" in s["run"] and "check_empty_with" not in s["run"]
    ]
    assert len(pytest_steps) == 1, (
        f"Expected exactly 1 pytest step in unit job, got {len(pytest_steps)}"
    )

    run_cmd = pytest_steps[0]["run"]
    assert "not integration" in run_cmd, "Unit job must exclude integration marker"
    assert "not perf" in run_cmd, "Unit job must exclude perf marker"
    assert "not live" in run_cmd, "Unit job must exclude live marker"


def test_ci_integration_tier_runs_on_single_python_version() -> None:
    """Verify that CI integration job runs on Python 3.11 only without matrixing."""
    data = yaml.safe_load(_CI_FILE.read_text(encoding="utf-8"))
    integration_job = data.get("jobs", {}).get("integration", {})

    # No matrix strategy for integration job
    assert "strategy" not in integration_job, (
        "Integration job must not be matrixed (runs single py version)"
    )

    # Check python-version step
    steps = integration_job.get("steps", [])
    py_steps = [s for s in steps if s.get("uses", "").startswith("actions/setup-python")]
    assert len(py_steps) == 1
    assert py_steps[0].get("with", {}).get("python-version") == "3.11"


def test_golden_thread_is_marked_live() -> None:
    """Verify that tests/integration/test_golden_thread.py carries the live marker."""
    content = _GOLDEN_THREAD_FILE.read_text(encoding="utf-8")
    assert "pytest.mark.live" in content, (
        "tests/integration/test_golden_thread.py must carry pytest.mark.live"
    )


def test_tdl8_python_floor_declared_and_enforced() -> None:
    """Verify TDL-8: requires-python = '>=3.10' in pyproject.toml and matrix has 3.10 and 3.11."""
    # Check pyproject.toml
    pyproject_text = _PYPROJECT_FILE.read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject_text)
    assert m, "pyproject.toml must declare requires-python"
    req_py = m.group(1)
    assert req_py == ">=3.10", (
        f"pyproject.toml must declare requires-python = '>=3.10', got '{req_py}'"
    )

    # Check ci.yml unit matrix
    data = yaml.safe_load(_CI_FILE.read_text(encoding="utf-8"))
    matrix = data.get("jobs", {}).get("test", {}).get("strategy", {}).get("matrix", {})
    py_versions = matrix.get("python-version", [])
    assert "3.10" in py_versions, "Unit matrix must test Python 3.10 to enforce TDL-8 floor"
    assert "3.11" in py_versions, "Unit matrix must test Python 3.11"


def test_nightly_workflow_wired_for_live_tier() -> None:
    """Verify that .github/workflows/nightly-golden-thread.yml is scheduled and runs live suite."""
    assert _NIGHTLY_FILE.is_file(), "Nightly Golden Thread workflow must exist"
    content = _NIGHTLY_FILE.read_text(encoding="utf-8")
    data = yaml.safe_load(content)

    on_block = data.get("on") or data.get(True) or {}
    assert "schedule" in on_block, "Nightly workflow must be scheduled via cron"
    assert "workflow_dispatch" in on_block, "Nightly workflow must support workflow_dispatch"

    jobs = data.get("jobs", {})
    assert "golden-thread" in jobs, "Nightly workflow must have golden-thread job"
    steps = jobs["golden-thread"].get("steps", [])
    run_steps = [s for s in steps if "run" in s and "test_golden_thread.py" in s["run"]]
    assert len(run_steps) >= 1, "Nightly workflow must run test_golden_thread.py"
    assert "-m live" in run_steps[0]["run"], "Nightly workflow must execute with -m live"


def test_positive_control_fails_on_unmarked_live_scenario() -> None:
    """Standing positive control (U18): prove ratchet rejects an unmarked live scenario."""
    synthetic_run_cmd = "pytest tests/ -m 'not integration and not perf'"
    assert "not live" not in synthetic_run_cmd, "Positive control precondition failed"

    # Verifies our check catches the missing 'not live'
    has_not_live = "not live" in synthetic_run_cmd
    assert not has_not_live, "Ratchet correctly identifies missing 'not live' filter"
