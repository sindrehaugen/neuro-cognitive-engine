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
* Only columns declared in ``nce.master_key_registry.WRAPPED_COLUMNS`` are touched. The six
  hash/signature columns classified there are derived values -- re-wrapping them would
  corrupt them, and a rotation does not invalidate them.

Exit codes: 0 clean, 1 blobs remain off the primary key (dry run, or unopenable rows), 2 error.
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
from nce.master_key_registry import WRAPPED_COLUMNS, WrappedColumn  # noqa: E402
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
    keys = ", ".join(f'"{k}"' for k in col.key_columns)

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
        row_id = ", ".join(f"{k}={row[k]!r}" for k in col.key_columns)

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


async def _preflight(conn: asyncpg.Connection) -> list[str]:
    """Check every registered column and key column exists BEFORE touching anything.

    The registry's key_columns default was wrong for four of eight columns and this sweep
    died mid-run on `column "id" does not exist`. Failing at row 1 of column 5 is a bad
    place to discover a typo: earlier columns may already have been written. So the shape
    is verified up front, against information_schema, and the run aborts before any UPDATE.
    """

    problems: list[str] = []
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
    if unopenable_total:
        log.error(
            "%d blob(s) no key on the ring opens. LEFT UNTOUCHED. Find the key that wraps "
            "them, or delete the rows deliberately -- do not rotate to make this go away.",
            unopenable_total,
        )
    if apply and not unopenable_total and not rewrapped_total:
        log.info("nothing to do -- every blob is already on the primary key.")
    if apply and rewrapped_total and not unopenable_total:
        log.info("safe to drop NCE_MASTER_KEY_PREVIOUS once every process has restarted.")

    return 1 if (unopenable_total or (rewrapped_total and not apply)) else 0


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
