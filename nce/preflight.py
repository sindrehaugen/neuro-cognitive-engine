"""Boot pre-flight: prove ``NCEEngine`` can start, then exit.

Run from the container entrypoint *before* handing off to the real command::

    python -m nce.preflight && exec uvicorn admin_server:app --workers 2

Why this exists
---------------
``uvicorn --workers N`` runs a supervisor that respawns a dead worker
immediately and forever, with no backoff.  When startup fails for an
environmental reason -- a stale image whose ``EXPECTED_TENANT_RLS_TABLES``
predates the live database, say -- every worker dies in ``connect()`` and is
respawned at once.  The supervisor itself never exits, so the container stays
``Up`` and Docker's ``restart:`` backoff never engages: the failure shows up as
saturated CPU rather than as an error.  That is the 2026-08-27 incident (fix
recipe #2), where two containers burned ~275% CPU silently.

Running the same startup path once, in a process whose exit code the entrypoint
respects, converts that silent loop into a container that exits non-zero on the
first failure -- visible as a restart count, and subject to Docker's backoff.

Faithfulness
------------
The check calls the real :meth:`NCEEngine.connect` rather than a subset of it.
A narrower probe would pass while the workers still crash-looped on whatever it
skipped, which is the failure mode this module exists to remove.  After recipe
#1 made boot-time ownership seeding a single set-based statement, ``connect()``
costs ~5 s, so paying it once up front is cheap.

Exit codes
----------
0
    Startup succeeded; the caller may exec the real command.
1
    Startup failed.  The exception is logged (secrets redacted).
2
    Startup did not finish within ``NCE_PREFLIGHT_TIMEOUT`` seconds.  A hung
    pre-flight must not hold PID 1 open forever -- that would recreate the very
    "container stays Up while nothing works" shape this guards against.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

log = logging.getLogger("nce-preflight")

#: ``scheme://user:password@host`` -- the credential shape a DSN error carries.
_CREDENTIAL_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*)://([^:/@\s]+):[^@/\s]+@")

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_TIMEOUT = 2

#: Seconds allowed for the whole connect() probe. Generous by default: a cold
#: database still has to apply schema.sql and every migration.
DEFAULT_TIMEOUT_SECONDS = 120.0


def _timeout_seconds() -> float:
    """Read ``NCE_PREFLIGHT_TIMEOUT``, falling back to the default."""
    raw = os.environ.get("NCE_PREFLIGHT_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "NCE_PREFLIGHT_TIMEOUT=%r is not a number; using %.0fs",
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        log.warning(
            "NCE_PREFLIGHT_TIMEOUT=%r must be positive; using %.0fs",
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    return value


async def _probe() -> None:
    """Connect the engine exactly as a real service would, then release it."""
    from nce import NCEEngine

    engine = NCEEngine()
    try:
        await engine.connect()
    finally:
        # Always release pools/clients, including on failure: a half-open pool
        # would otherwise keep the pre-flight process alive past its exit path.
        try:
            await engine.disconnect()
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            log.debug("pre-flight cleanup failed (ignored): %s", exc)


def _fallback_redact(text: str) -> str:
    """Strip ``scheme://user:password@host`` credentials, keeping the user.

    Used when the real redactor cannot be imported. The role name is kept
    deliberately: it says which principal failed to connect, and it is not the
    secret.
    """
    return _CREDENTIAL_URL_RE.sub(r"\1://\2:***@", text)


def _redact(text: str) -> str:
    """Redact secrets without making the redactor a prerequisite for reporting.

    ``nce.config`` raises at import time for some environment failures -- for
    example ``NCE_LOAD_DOTENV must be false in production`` -- and those are
    exactly the failures this module exists to report. Importing the real
    redactor eagerly turned such a failure into a raw traceback instead of the
    FATAL line below. When it is unavailable, fall back to stripping
    ``scheme://user:password@host`` credentials, the shape a connection error
    actually carries.
    """
    try:
        from nce.config import redact_secrets_in_text
    except Exception:
        return _fallback_redact(text)
    return redact_secrets_in_text(text)


async def run_preflight() -> int:
    """Run the probe under a timeout and map the outcome to an exit code."""
    timeout = _timeout_seconds()
    try:
        await asyncio.wait_for(_probe(), timeout=timeout)
    except TimeoutError:
        log.critical(
            "FATAL: pre-flight did not complete within %.0fs. Refusing to start; "
            "the container will exit so the restart backoff engages.",
            timeout,
        )
        return EXIT_TIMEOUT
    except Exception as exc:
        log.critical(
            "FATAL: pre-flight startup failure: %s: %s",
            type(exc).__name__,
            _redact(str(exc)),
        )
        log.critical(
            "Refusing to start. This is an environment/deploy failure, not a "
            "request-time error -- rebuild the image or migrate the database. "
            "The container exits so the failure is visible as a restart count "
            "instead of a silent respawn loop."
        )
        return EXIT_STARTUP_FAILED
    log.info("Pre-flight OK: NCEEngine startup path completed. Handing off.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. ``argv`` is accepted for symmetry and currently unused."""
    logging.basicConfig(
        level=os.environ.get("NCE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(run_preflight())


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main())
