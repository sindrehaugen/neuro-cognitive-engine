"""Thread-safe registry for active child subprocesses to prevent zombie/orphaned processes."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager

log = logging.getLogger("nce.subprocess_registry")

# ---------------------------------------------------------------------------
# Resource-limit wrapper (POSIX-only — Linux worker containers)
# ---------------------------------------------------------------------------
# These limits are applied in the child process via preexec_fn so they only
# affect the launched subprocess, not the parent Python process.
#
# Default limits are intentionally conservative for untrusted document
# conversion tools (LibreOffice, MPXJ).  Override via env vars:
#
#   NCE_SUBPROCESS_CPU_SECONDS   — RLIMIT_CPU  soft+hard (default 120 s)
#   NCE_SUBPROCESS_AS_BYTES      — RLIMIT_AS   soft+hard (default 2 GiB)
#
# On non-POSIX platforms (Windows) this module is a safe no-op.
# ---------------------------------------------------------------------------

_POSIX = os.name == "posix"

# Limit defaults (may be overridden at call time).
_DEFAULT_CPU_SECONDS: int = int(os.environ.get("NCE_SUBPROCESS_CPU_SECONDS", "120"))
_DEFAULT_AS_BYTES: int = int(os.environ.get("NCE_SUBPROCESS_AS_BYTES", str(2 * 1024**3)))


def _make_rlimit_preexec(cpu_seconds: int, as_bytes: int) -> Callable[[], None] | None:
    """Return a ``preexec_fn`` that applies CPU and address-space limits.

    Returns *None* on non-POSIX platforms so the caller can pass it
    directly to ``subprocess.Popen(preexec_fn=…)`` — Popen ignores None.
    """
    if not _POSIX:
        return None

    import resource  # POSIX-only; import here to keep module importable on Windows

    def _apply() -> None:
        # RLIMIT_CPU: CPU seconds.  When the soft limit is hit the process
        # receives SIGXCPU; when the hard limit is hit it is killed (SIGKILL).
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))  # type: ignore[attr-defined]
        # RLIMIT_AS: virtual address space (bytes).  malloc/mmap beyond this
        # fails with ENOMEM, preventing memory-bomb documents from exhausting
        # the host.
        resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))  # type: ignore[attr-defined]

    return _apply


def make_confinement_preexec(
    *,
    cpu_seconds: int = _DEFAULT_CPU_SECONDS,
    as_bytes: int = _DEFAULT_AS_BYTES,
) -> Callable[[], None] | None:
    """Public factory — returns a ``preexec_fn`` for use with :func:`subprocess.Popen`.

    The returned callable applies RLIMIT_CPU and RLIMIT_AS in the child
    process.  On Windows the return value is *None* (safe no-op).

    Example::

        proc = subprocess.Popen(cmd, preexec_fn=make_confinement_preexec())
    """
    return _make_rlimit_preexec(cpu_seconds=cpu_seconds, as_bytes=as_bytes)


_lock = threading.Lock()
_active_processes: set[subprocess.Popen] = set()


def register_process(proc: subprocess.Popen) -> None:
    """Register a running subprocess."""
    with _lock:
        _active_processes.add(proc)
        log.debug("Registered subprocess PID=%d", proc.pid)


def unregister_process(proc: subprocess.Popen) -> None:
    """Unregister a finished/terminated subprocess."""
    with _lock:
        _active_processes.discard(proc)
        log.debug("Unregistered subprocess PID=%d", proc.pid)


@contextmanager
def tracked_process(proc: subprocess.Popen) -> Generator[subprocess.Popen, None, None]:
    """Context manager to register and automatically unregister a subprocess."""
    register_process(proc)
    try:
        yield proc
    finally:
        unregister_process(proc)


def terminate_all() -> None:
    """Terminate and kill all registered subprocesses.

    Called during server shutdown or task cancellation to ensure no child
    processes are left running (preventing zombie/orphaned processes).
    """
    with _lock:
        procs = list(_active_processes)
        _active_processes.clear()

    if not procs:
        return

    log.info("Terminating %d active child subprocess(es)...", len(procs))

    # Send SIGTERM (terminate) to all processes
    for proc in procs:
        try:
            if proc.poll() is None:
                log.info("Sending terminate to subprocess PID=%d", proc.pid)
                proc.terminate()
        except OSError as e:
            log.debug("Failed to terminate subprocess PID=%d: %s", proc.pid, e)

    # Wait briefly for processes to exit
    for proc in procs:
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            pass

    # Send SIGKILL (kill) to any processes still alive
    for proc in procs:
        try:
            if proc.poll() is None:
                log.warning("Subprocess PID=%d did not terminate; killing it.", proc.pid)
                proc.kill()
                proc.wait()
        except OSError as e:
            log.debug("Failed to kill subprocess PID=%d: %s", proc.pid, e)
