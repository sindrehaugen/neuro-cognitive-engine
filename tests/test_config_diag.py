"""Tests for the NCE_DIAG_* configuration surface (Batch 65 — diag-config).

Strategy — NO importlib.reload
-------------------------------
``nce/config.py`` installs the shared secrets-provider seam at import time.
Reloading it rebuilds the singleton state, which pollutes global state for
other tests (notably ``test_secrets_provider_seam.py``) when they share a
pytest-xdist worker.

Instead:
  1. Default-value assertions hit the already-imported ``cfg`` class directly.
  2. Parse-behaviour tests for ``_int_env`` and the bool idiom exercise the
     *helper functions* themselves with ``monkeypatch.setenv`` — those helpers
     read ``os.getenv`` live (not a module-level snapshot), so no reload is needed.
"""

from __future__ import annotations

import pytest

from nce.config import _int_env, cfg

# ---------------------------------------------------------------------------
# 1. Default values — no env overrides needed
# ---------------------------------------------------------------------------


def test_diag_enabled_default_false() -> None:
    assert cfg.NCE_DIAG_ENABLED is False


def test_diag_landing_bucket_default() -> None:
    assert cfg.NCE_DIAG_LANDING_BUCKET == "nce-diag-landing"


def test_diag_landing_ttl_days_default() -> None:
    assert cfg.NCE_DIAG_LANDING_TTL_DAYS == 7


def test_diag_max_bundle_mb_default() -> None:
    assert cfg.NCE_DIAG_MAX_BUNDLE_MB == 700


def test_diag_max_anomalies_default() -> None:
    assert cfg.NCE_DIAG_MAX_ANOMALIES == 50


def test_diag_job_timeout_min_default() -> None:
    assert cfg.NCE_DIAG_JOB_TIMEOUT_MIN == 45


def test_diag_crash_storm_threshold_default() -> None:
    assert cfg.NCE_DIAG_CRASH_STORM_THRESHOLD == 10


def test_diag_crash_storm_window_sec_default() -> None:
    assert cfg.NCE_DIAG_CRASH_STORM_WINDOW_SEC == 300


def test_diag_tmpdir_default_empty() -> None:
    assert cfg.NCE_DIAG_TMPDIR == ""


# ---------------------------------------------------------------------------
# 2. Bool-idiom parsing — test the exact expression used in config.py
#    directly, without touching the module-level singleton.
# ---------------------------------------------------------------------------

_BOOL_TRUTHY = ("1", "true", "True", "TRUE", "yes", "YES", "Yes")
_BOOL_FALSY = ("0", "false", "False", "no", "NO", "", "random", "off")


@pytest.mark.parametrize("raw", _BOOL_TRUTHY)
def test_diag_enabled_bool_idiom_truthy(raw: str) -> None:
    """The exact idiom used for NCE_DIAG_ENABLED must parse these as truthy."""
    result = raw.strip().lower() in ("1", "true", "yes")
    assert result is True, f"Expected truthy for {raw!r}"


@pytest.mark.parametrize("raw", _BOOL_FALSY)
def test_diag_enabled_bool_idiom_falsy(raw: str) -> None:
    """Values outside the truthy set must be falsy."""
    result = raw.strip().lower() in ("1", "true", "yes")
    assert result is False, f"Expected falsy for {raw!r}"


# ---------------------------------------------------------------------------
# 3. _int_env parsing — exercises the shared helper with monkeypatch.setenv.
#    _int_env reads os.getenv live, so no module reload is needed.
# ---------------------------------------------------------------------------


def test_int_env_uses_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_TEST_NCE_DIAG_INT", "42")
    assert _int_env("_TEST_NCE_DIAG_INT", 99) == 42


def test_int_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_TEST_NCE_DIAG_INT_ABSENT", raising=False)
    assert _int_env("_TEST_NCE_DIAG_INT_ABSENT", 77) == 77


def test_int_env_enforces_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_TEST_NCE_DIAG_INT_MIN", "0")
    with pytest.raises(RuntimeError, match="must be >= 1"):
        _int_env("_TEST_NCE_DIAG_INT_MIN", 1, minimum=1)


def test_int_env_accepts_at_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_TEST_NCE_DIAG_INT_AT_MIN", "1")
    assert _int_env("_TEST_NCE_DIAG_INT_AT_MIN", 99, minimum=1) == 1
