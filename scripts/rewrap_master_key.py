#!/usr/bin/env python3
"""Re-wrap every master-key-wrapped blob under the current primary key.

This is the step that makes a master-key rotation seamless instead of a coordinated
migration with downtime:

    1. NCE_MASTER_KEY_PREVIOUS=<old>  NCE_MASTER_KEY=<new>   and restart
       -> old blobs still open under PREVIOUS, new writes use the new key
    2. python scripts/rewrap_master_key.py --apply
    3. when this reports 0 blobs off the primary key, drop NCE_MASTER_KEY_PREVIOUS

Step 3 is the point: retiring the old key becomes evidence-based. The 2026-09-07 incident
happened because nobody could answer "which key opens this?" -- and the answer was found by
hand-unwrapping candidate keys hours later.

SAFETY, in the order it matters
-------------------------------
* **Dry run by default.** ``--apply`` is required to write anything.
* **Every new blob is verified to decrypt under the primary key BEFORE the UPDATE**, inside
  the same transaction as that UPDATE. A re-wrap that produced an unopenable blob would be
  indistinguishable from data loss, and it would be discovered long after the old key was
  gone.
* **A row no key opens is REPORTED AND LEFT ALONE.** Never rewritten, never treated as
  corrupt. This is a real state here: two retired signing keys in this database are wrapped
  under ``tests/conftest.py``'s ``"x" * 32``, which no deployment key opens.
* **Idempotent.** A blob already tagged with the primary fingerprint is skipped, so a
  second run writes nothing.
* **Refuses to run as a role that cannot bypass RLS.** Five of the eight registered columns
  are on ``FORCE ROW LEVEL SECURITY`` tables and this sweep sets no namespace context, so such a
  role sees zero rows there and would be told it is safe to drop ``NCE_MASTER_KEY_PREVIOUS`` while
  blobs remain wrapped under the old key. That is permanent loss reached through a green exit
  code, so it is a pre-flight refusal rather than a warning (F10, 2026-09-10).
* Only columns declared in ``nce.master_key_registry.WRAPPED_COLUMNS`` are touched. The six
  hash/signature columns classified there are derived values -- re-wrapping them would
  corrupt them, and a rotation does not invalidate them.

Exit codes
----------
* **0** -- nothing actionable remains. Every blob that any ring key can open is on the
  primary key, so it is safe to drop ``NCE_MASTER_KEY_PREVIOUS``. Permanently unopenable
  rows may still exist and are reported; they do **not** hold the exit code at 1, because
  no key on the ring opens them and ``PREVIOUS`` does not either.
* **1** -- blobs remain off the primary key that a ring key CAN open. Either the dry run
  found work to do, or an ``--apply`` run did not finish it.
* **2** -- error (pre-flight failure, or no key opens a blob during a required decrypt).

🔴 This changed on 2026-09-10. Previously ANY unopenable row forced exit 1 and suppressed
the "safe to drop" line, which made the completion signal unreachable in this deployment --
see the F3 comment in ``main_async``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nce.config import cfg  # noqa: E402
from nce.master_key_registry import (  # noqa: E402
    WRAPPED_COLUMNS,
    WrappedColumn,
    rls_visibility_problem,
)
from nce.master_key_ring import (  # noqa: E402
    NoKeyOpensBlobError,
    blob_is_on_primary,
    decrypt_with_ring,
    master_key_ring,
)
from nce.signing import (  # noqa: E402
    MasterKey,
    decrypt_signing_key,
    encrypt_signing_key,
    master_key_fingerprint,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rewrap")


class ColumnResult:
    """Per-column tally. Kept explicit so the summary cannot silently omit a category."""

    def __init__(self, column: WrappedColumn) -> None:
        self.column = column
        self.on_primary = 0
        self.rewrapped = 0
        self.unopenable: list[str] = []
        self.missing_table = False

    @property
    def needs_action(self) -> int:
        return self.rewrapped + len(self.unopenable)


async def _sweep_column(
    conn: asyncpg.Connection,
    col: WrappedColumn,
    primary: MasterKey,
    ring: list[MasterKey],
    apply: bool,
) -> ColumnResult:
    result = ColumnResult(col)
    # key_columns address the row in the UPDATE; label_columns only name it in the output.
    # dict.fromkeys keeps order and drops duplicates when a label is also a key column.
    select_columns = tuple(dict.fromkeys(col.key_columns + col.label_columns))
    keys = ", ".join(f'"{k}"' for k in select_columns)

    try:
        rows = await conn.fetch(
            f'SELECT {keys}, "{col.column}" AS blob FROM "{col.table}" '  # noqa: S608
            f'WHERE "{col.column}" IS NOT NULL'
        )
    except asyncpg.exceptions.UndefinedTableError:
        # Registered but absent: a fresh database that has not run every migration. Not an
        # error -- but reported, because a silently skipped column is how a rotation loses
        # data.
        result.missing_table = True
        return result

    for row in rows:
        blob = bytes(row["blob"])
        row_id = ", ".join(f"{k}={row[k]!r}" for k in (col.label_columns or col.key_columns))

        if blob_is_on_primary(blob, primary) is True:
            result.on_primary += 1
            continue

        try:
            plaintext, _opened_by = decrypt_with_ring(blob, ring)
        except NoKeyOpensBlobError as exc:
            result.unopenable.append(f"{row_id}: {exc}")
            continue

        new_blob = encrypt_signing_key(plaintext, primary)

        # Verify BEFORE writing. An unopenable re-wrap is indistinguishable from data loss
        # and would surface only after the old key was retired.
        if decrypt_signing_key(new_blob, primary) != plaintext:
            raise RuntimeError(
                f"re-wrap verification FAILED for {col.table}.{col.column} {row_id} -- "
                "refusing to write. Nothing has been changed."
            )

        if apply:
            where = " AND ".join(f'"{k}" = ${i + 2}' for i, k in enumerate(col.key_columns))
            async with conn.transaction():
                await conn.execute(
                    f'UPDATE "{col.table}" SET "{col.column}" = $1 WHERE {where}',  # noqa: S608
                    new_blob,
                    *[row[k] for k in col.key_columns],
                )
        result.rewrapped += 1

    return result


async def foreign_key_blob_count(conn: asyncpg.Connection) -> int:
    """Blobs NOT tagged with the primary key's fingerprint.

    The number to watch before dropping ``NCE_MASTER_KEY_PREVIOUS``. Counts untagged
    (pre-v5) blobs too -- they carry no key identity, so they cannot be assumed current.
    """

    primary = MasterKey.from_env()
    total = 0
    for col in WRAPPED_COLUMNS:
        try:
            rows = await conn.fetch(
                f'SELECT "{col.column}" AS blob FROM "{col.table}" '  # noqa: S608
                f'WHERE "{col.column}" IS NOT NULL'
            )
        except asyncpg.exceptions.UndefinedTableError:
            continue
        total += sum(1 for r in rows if blob_is_on_primary(bytes(r["blob"]), primary) is not True)
    return total


def drop_previous_decision(off_primary_total: int, unopenable_total: int) -> tuple[bool, int]:
    """Return ``(safe_to_drop_previous, exit_code)`` for a finished sweep.

    Pure and separately tested, because getting it wrong is silent: the F3 defect shipped in
    #125, survived review, and was only caught by running the sweep against real data during
    the 2026-09-10 rehearsal. See ``tests/test_rewrap_sweep_drop_decision.py``.

    Only **actionable** blobs -- off the primary key and openable by some key on the ring --
    can require ``NCE_MASTER_KEY_PREVIOUS`` to stay. A blob no ring key opens is not opened by
    ``PREVIOUS`` either, so it must never hold the decision hostage.
    """

    actionable = off_primary_total - unopenable_total
    return (actionable <= 0, 1 if actionable > 0 else 0)


async def _preflight(conn: asyncpg.Connection) -> list[str]:
    """Check every registered column and key column exists BEFORE touching anything.

    The registry's key_columns default was wrong for four of eight columns and this sweep
    died mid-run on `column "id" does not exist`. Failing at row 1 of column 5 is a bad
    place to discover a typo: earlier columns may already have been written. So the shape
    is verified up front, against information_schema, and the run aborts before any UPDATE.
    """

    problems: list[str] = []

    # F10, found by the 2026-09-10 redeploy session. THIS CHECK COMES FIRST because its
    # absence fails in the reassuring direction, which is the only kind of failure that
    # actually destroys data here.
    #
    # `_sweep_column` issues a bare `SELECT ... WHERE "<col>" IS NOT NULL` and never sets a
    # namespace context. FIVE of the eight registered columns live on tables with
    # `relforcerowsecurity` -- memories, pii_redactions, signing_credentials,
    # bridge_subscriptions, d365_integrations. A role without BYPASSRLS therefore sees ZERO
    # rows in those five, with no error and no warning, and the sweep prints
    # "SAFE TO DROP NCE_MASTER_KEY_PREVIOUS" with exit 0 while blobs are still wrapped under
    # the old key. Drop PREVIOUS at that point and they are permanently unopenable -- the
    # exact outcome #122-#130 exist to prevent, reached through a green exit code.
    #
    # Demonstrated on a throwaway with a NOSUPERUSER NOBYPASSRLS role holding SELECT+UPDATE:
    # the sweep reported `re-wrapped: 4, off primary: 0, SAFE TO DROP, exit 0` while the truth
    # from a BYPASSRLS role was `off primary: 9`.
    #
    # This is live risk, not a curiosity: runbook step 4 is `--dsn <live>`, a placeholder the
    # operator fills in, and `nce_app` -- the application role, verified NOSUPERUSER
    # NOBYPASSRLS -- is the natural DSN to reach for. The documented role (`mcp_user`) happens
    # to be safe; nothing made that a requirement until now.
    #
    # `_preflight`'s existing column checks cannot catch it: `information_schema.columns` is
    # not RLS-filtered, so they pass cleanly on a role that can see none of the data. The
    # script already reports a silently skipped column for a missing table, precisely because
    # "a silently skipped column is how a rotation loses data". F10 is that same failure
    # arriving through RLS, and this is the one place the script had not applied its own rule
    # to itself.
    rls_problem = await rls_visibility_problem(conn)
    if rls_problem:
        problems.append(rls_problem)

    for col in WRAPPED_COLUMNS:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1",
            col.table,
        )
        if not rows:
            continue  # table absent entirely -- reported per-column during the sweep
        present = {r["column_name"] for r in rows}
        if col.column not in present:
            problems.append(f"{col.table}.{col.column} is registered but does not exist")
        for key in col.key_columns:
            if key not in present:
                problems.append(
                    f"{col.table}: key column {key!r} does not exist "
                    f"(registry says key_columns={col.key_columns})"
                )
    return problems


async def main_async(apply: bool, dsn: str) -> int:
    ring = master_key_ring()
    primary = ring[0]
    log.info("primary master key fingerprint : %s", master_key_fingerprint(primary))
    if len(ring) > 1:
        log.info("previous (decrypt-only)        : %s", master_key_fingerprint(ring[1]))
    else:
        log.info("previous (decrypt-only)        : <not set>")
    log.info("mode                           : %s", "APPLY" if apply else "DRY RUN")
    log.info("columns from registry          : %d", len(WRAPPED_COLUMNS))
    log.info("")

    conn = await asyncpg.connect(dsn)
    try:
        problems = await _preflight(conn)
        if problems:
            log.error("PRE-FLIGHT FAILED -- nothing was changed:")
            for p in problems:
                log.error("  %s", p)
            return 2
        results = [await _sweep_column(conn, c, primary, ring, apply) for c in WRAPPED_COLUMNS]
        # F5: this is the number nce/master_key_ring.py's module docstring tells the operator
        # to watch before dropping NCE_MASTER_KEY_PREVIOUS -- and until 2026-09-10 it was
        # never called anywhere, so the metric the runbook pointed at was never computed.
        off_primary_total = await foreign_key_blob_count(conn)
    finally:
        await conn.close()

    unopenable_total = 0
    rewrapped_total = 0
    for r in results:
        state = (
            "TABLE ABSENT"
            if r.missing_table
            else (
                f"on-primary={r.on_primary} rewrapped={r.rewrapped} unopenable={len(r.unopenable)}"
            )
        )
        log.info("  %-46s %s", f"{r.column.table}.{r.column.column}", state)
        for detail in r.unopenable:
            log.warning("      UNOPENABLE %s", detail)
        unopenable_total += len(r.unopenable)
        rewrapped_total += r.rewrapped

    log.info("")
    verb = "re-wrapped" if apply else "would re-wrap"
    log.info("%s: %d blob(s)", verb, rewrapped_total)

    # ------------------------------------------------------------------ the drop decision
    #
    # F3, 2026-09-10 rebuild. This block used to gate BOTH the "safe to drop" line and the
    # exit code on `unopenable_total == 0`. Two rows in the live database are permanently
    # unopenable by design -- wrapped under tests/conftest.py's "x" * 32 by the accidental
    # rotations of 2026-09-07, and the runbook correctly says never to "fix" them. So the
    # sweep exited 1 forever and the completion signal could never print: runbook step 5,
    # "drop PREVIOUS when the sweep reports zero off-primary blobs", would have waited
    # forever. #125 existed to make retiring the old key evidence-based, and as shipped the
    # evidence never arrived.
    #
    # The logic error, stated plainly: an unopenable blob is not openable by PREVIOUS
    # EITHER. No key on the ring opens it, PREVIOUS included. So it cannot be a reason to
    # keep PREVIOUS around, and gating the drop decision on it was exactly backwards.
    #
    # What does gate the decision: blobs off the primary key that SOME ring key can open.
    # Those are the rows PREVIOUS is still needed for.
    safe_to_drop, exit_code = drop_previous_decision(off_primary_total, unopenable_total)
    actionable = off_primary_total - unopenable_total

    log.info("blobs off the primary key      : %d", off_primary_total)
    if unopenable_total:
        log.warning(
            "  of which permanently unopenable: %d -- LEFT UNTOUCHED, and they do NOT block "
            "dropping NCE_MASTER_KEY_PREVIOUS, because no key on the ring opens them and "
            "PREVIOUS does not either. Find the key that wraps them, or delete the rows "
            "deliberately -- do NOT rotate to make this go away.",
            unopenable_total,
        )
    log.info("  actionable (a ring key opens)  : %d", actionable)

    if not safe_to_drop:
        if apply:
            log.error(
                "%d blob(s) remain off the primary key after an --apply run. Do NOT drop "
                "NCE_MASTER_KEY_PREVIOUS.",
                actionable,
            )
        else:
            log.info("run again with --apply to re-wrap them.")
        return exit_code

    if rewrapped_total == 0 and not unopenable_total:
        log.info("nothing to do -- every blob is already on the primary key.")
    log.info(
        "SAFE TO DROP NCE_MASTER_KEY_PREVIOUS once every process has restarted%s.",
        f" ({unopenable_total} acknowledged unopenable row(s) remain and are expected)"
        if unopenable_total
        else "",
    )
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write. Without it, report only and change nothing.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN. Defaults to the configured PG_DSN.",
    )
    args = parser.parse_args()
    dsn = args.dsn or cfg.PG_DSN
    try:
        raise SystemExit(asyncio.run(main_async(args.apply, dsn)))
    except NoKeyOpensBlobError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
