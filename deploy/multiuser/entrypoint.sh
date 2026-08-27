#!/bin/sh
# NCE container entrypoint — boot pre-flight, then exec the real command.
#
# Fix recipe #2 (2026-08-27). `uvicorn --workers N` respawns a worker that dies
# during startup immediately and forever, with no backoff, and its supervisor
# never exits — so the container stays `Up`, Docker's `restart:` backoff never
# engages, and an environmental startup failure (stale image vs migrated DB)
# presents as saturated CPU instead of an error. Validating once here, in a
# process whose exit code we respect, makes the container *exit* on that class
# of failure: visible as a restart count, and subject to the backoff.
#
# Set NCE_PREFLIGHT=0 to skip (debugging, or a command that must run against a
# database the engine cannot fully validate).
set -e

if [ "${NCE_PREFLIGHT:-1}" = "0" ]; then
    echo "[entrypoint] NCE_PREFLIGHT=0 — skipping boot pre-flight." >&2
else
    echo "[entrypoint] Running boot pre-flight..." >&2
    python -m nce.preflight
fi

exec "$@"
