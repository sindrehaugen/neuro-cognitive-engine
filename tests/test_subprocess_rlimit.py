"""Tests for the rlimit confinement wrapper in nce.subprocess_registry.

POSIX-only tests are skip-guarded with ``@pytest.mark.skipif(os.name != "posix", …)``
so this file is safe to collect on Windows (all POSIX tests are skipped; they are
reported as "deferred (POSIX-only, skipped on Windows host)" in the batch gate output).

The binary-hash guard and shell-arg guard tests are platform-independent and always run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from nce.subprocess_registry import (
    _POSIX,
    _make_rlimit_preexec,
    make_confinement_preexec,
)

# ---------------------------------------------------------------------------
# Platform guard helpers
# ---------------------------------------------------------------------------

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="rlimit/resource module is POSIX-only — deferred on Windows host",
)


# ---------------------------------------------------------------------------
# make_confinement_preexec — cross-platform (always runs)
# ---------------------------------------------------------------------------


def test_make_confinement_preexec_returns_none_on_windows() -> None:
    """On non-POSIX platforms make_confinement_preexec must return None (safe no-op)."""
    with patch("nce.subprocess_registry._POSIX", False):
        fn = make_confinement_preexec()
    assert fn is None


def test_make_confinement_preexec_returns_callable_on_posix() -> None:
    """On POSIX platforms make_confinement_preexec must return a callable."""
    if not _POSIX:
        pytest.skip("POSIX not available on this host")
    fn = make_confinement_preexec()
    assert callable(fn)


# ---------------------------------------------------------------------------
# _make_rlimit_preexec — cross-platform gate
# ---------------------------------------------------------------------------


def test_make_rlimit_preexec_none_when_not_posix() -> None:
    """_make_rlimit_preexec returns None when _POSIX is patched to False."""
    with patch("nce.subprocess_registry._POSIX", False):
        fn = _make_rlimit_preexec(cpu_seconds=10, as_bytes=512 * 1024 * 1024)
    assert fn is None


# ---------------------------------------------------------------------------
# Functional confinement tests — POSIX-only
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_cpu_limit_kills_cpu_hog() -> None:
    """A subprocess that burns CPU must be killed before the wall-clock timeout."""
    # 1-second CPU limit; the child spins forever.  The process must exit
    # (killed by SIGXCPU/SIGKILL from the kernel) well within 10 s wall time.
    preexec = make_confinement_preexec(cpu_seconds=1, as_bytes=512 * 1024 * 1024)
    proc = subprocess.Popen(
        [sys.executable, "-c", "while True: pass"],
        preexec_fn=preexec,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("CPU-hog process was NOT killed by rlimit within 10 s wall time")

    # Must exit with a non-zero code (killed by signal).
    assert proc.returncode != 0, (
        f"Expected non-zero exit from CPU-limited process, got {proc.returncode}"
    )


@_POSIX_ONLY
def test_as_limit_prevents_large_allocation() -> None:
    """A subprocess requesting more memory than the AS limit must fail/be killed."""
    # 32 MiB address-space limit; child tries to allocate 128 MiB.
    as_limit = 32 * 1024 * 1024
    preexec = make_confinement_preexec(cpu_seconds=30, as_bytes=as_limit)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os; os.urandom(128 * 1024 * 1024)"],
        preexec_fn=preexec,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("Memory-hog process was NOT killed/constrained by AS rlimit within 10 s")

    # Exit must be non-zero (OOM kill or MemoryError).
    assert proc.returncode != 0, (
        f"Expected non-zero exit from AS-limited process, got {proc.returncode}"
    )


@_POSIX_ONLY
def test_well_behaved_process_completes_normally() -> None:
    """A process that stays within limits must complete successfully."""
    preexec = make_confinement_preexec(cpu_seconds=10, as_bytes=256 * 1024 * 1024)
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('ok')"],
        preexec_fn=preexec,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert b"ok" in stdout


@_POSIX_ONLY
def test_preexec_is_no_op_when_none() -> None:
    """Passing preexec_fn=None to Popen must not raise (Popen accepts None)."""
    with patch("nce.subprocess_registry._POSIX", False):
        preexec = make_confinement_preexec()
    assert preexec is None
    # Popen with preexec_fn=None is the default — must work on POSIX too.
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('no-op ok')"],
        preexec_fn=preexec,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert b"no-op ok" in stdout


# ---------------------------------------------------------------------------
# Binary-hash guard — platform-independent (uses mock)
# ---------------------------------------------------------------------------


def test_verify_binary_safety_rejects_hash_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """_verify_binary_safety must return None when the hash does not match."""
    from nce.net_safety import _verify_binary_safety

    binary = tmp_path / "fake_bin"
    binary.write_bytes(b"not a real binary")
    result = _verify_binary_safety(str(binary), "deadbeef" * 8)  # wrong hash
    assert result is None


def test_verify_binary_safety_accepts_correct_hash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """_verify_binary_safety must return the absolute path for a correct SHA-256."""
    import hashlib

    from nce.net_safety import _verify_binary_safety

    content = b"fake binary content"
    binary = tmp_path / "real_bin"
    binary.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    result = _verify_binary_safety(str(binary), expected)
    assert result is not None
    assert result == str(binary)


def test_verify_binary_safety_rejects_empty_executable() -> None:
    """_verify_binary_safety must return None for an empty executable string."""
    from nce.net_safety import _verify_binary_safety

    assert _verify_binary_safety("", None) is None


def test_verify_binary_safety_rejects_relative_path_with_separator() -> None:
    """_verify_binary_safety must reject relative paths that contain a path separator."""
    from nce.net_safety import _verify_binary_safety

    assert _verify_binary_safety("../evil/bin", "anyhash") is None
