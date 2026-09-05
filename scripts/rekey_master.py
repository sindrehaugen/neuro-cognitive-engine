#!/usr/bin/env python3
"""Rotate ``NCE_MASTER_KEY`` in place -- completely, or not at all.

ADR-0006 records the gap this closes in its own Consequences: *"Key rotation
requires restarting the process with a new environment variable; there is no
live rotation path for the master key itself."*  The 2026-09-02 incident was
exactly that missing path improvised by hand: the signing blob was re-wrapped
under a new master key and the deployment was left holding the old one, for 26
hours, while every container reported ``healthy``.

What this script does, in **one transaction**:

1. Verify the **old** key unwraps the active signing key.  Abort before any
   write if it does not -- re-wrapping data you cannot open destroys it.
2. Re-wrap every ``signing_keys.encrypted_key`` under the new key.
3. Re-wrap every **non-NULL** ``memories.wrapped_dek`` under the new key.
   NULL means "this memory has no envelope-encrypted payload" and is skipped,
   not an error; both counts are reported.
4. **Verify before commit**: read every row back and unwrap it with the **new**
   key.  Any failure rolls the whole transaction back.  Step 4 is the entire
   point of this script -- it is what September did not have.
5. Only then commit, write the new key to its authoritative path with a
   ``.sha256`` sidecar, print the new fingerprint, and print an escrow reminder.

What this script deliberately does **not** do:

* It does not back up, escrow, or derive the master key.  Content is encrypted
  under per-memory DEKs wrapped by this key (``nce.envelope``); that is the same
  property that lets NCE prove deletion, so a recovery copy in the database
  would defeat provable forgetting.  Losing the key crypto-shreds the data
  **by design**.
* It never re-encrypts content.  Only the wrapped DEKs change, so the MongoDB
  payloads stay encrypted under their unchanged DEKs.  That is what keeps the
  work bounded: one AES-GCM unwrap/wrap per key row, not per byte of corpus.
* It never prints, logs, or writes key material anywhere except the
  authoritative key path.  Everything on stdout is a ``sha256(key)[:16]``
  fingerprint, which is not secret.

Usage::

    export NCE_MASTER_KEY=...              # the OLD key (normal resolution)
    export NCE_NEW_MASTER_KEY=...          # or --new-key-file /path/to/newkey
    python scripts/rekey_master.py --dsn "$PG_DSN" --dry-run
    python scripts/rekey_master.py --dsn "$PG_DSN" --key-path deploy/secrets/nce_master_key

``--dry-run`` performs steps 1-4 and then rolls back deliberately, so a
rotation can be rehearsed against a copy of the database with no writes kept.
The new key is never accepted as a command-line argument: that would leave it
in shell history and in ``ps``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import asyncpg

from nce.envelope import unwrap_dek, wrap_dek
from nce.signing import (
    MasterKey,
    SigningKeyDecryptionError,
    decrypt_signing_key,
    master_key_fingerprint,
    rewrap_signing_key,
)

log = logging.getLogger("rekey_master")

_NEW_KEY_ENV = "NCE_NEW_MASTER_KEY"


class RekeyAborted(RuntimeError):
    """The re-key refused to proceed, or rolled back.  Nothing was committed."""


class _DryRun(RuntimeError):
    """Internal: raised after step 4 to force a rollback under ``--dry-run``."""


# ---------------------------------------------------------------------------
# Step 4 -- one row at a time, as module-level functions so a test can inject
# a failure into the verification itself rather than trusting the transaction.
# ---------------------------------------------------------------------------


def _verify_signing_blob(blob: bytes, new_master_key: MasterKey, key_id: str) -> None:
    """Assert the NEW key unwraps a just-written ``signing_keys`` row."""
    try:
        decrypt_signing_key(blob, new_master_key)
    except Exception as exc:  # noqa: BLE001 - any failure here must roll back
        raise RekeyAborted(
            f"signing key {key_id} does not unwrap under the new master key: {exc}"
        ) from exc


def _verify_wrapped_dek(blob: bytes, new_master_key: MasterKey, memory_id: object) -> None:
    """Assert the NEW key unwraps a just-written ``memories.wrapped_dek`` row."""
    try:
        unwrap_dek(blob, new_master_key)
    except Exception as exc:  # noqa: BLE001 - any failure here must roll back
        raise RekeyAborted(
            f"wrapped_dek for memory {memory_id} does not unwrap under the new master key: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# The re-key itself
# ---------------------------------------------------------------------------


async def rekey_all(
    conn: asyncpg.Connection,
    old_master_key: MasterKey,
    new_master_key: MasterKey,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Re-wrap every wrapped blob from *old_master_key* to *new_master_key*.

    Returns a dict of counts.  Raises :class:`RekeyAborted` -- with nothing
    written -- when the old key does not own the data, or when step 4's
    verification with the new key fails on any row.
    """
    old_fp = master_key_fingerprint(old_master_key)
    new_fp = master_key_fingerprint(new_master_key)
    if old_fp == new_fp:
        raise RekeyAborted(
            f"the new master key is identical to the old one (fingerprint={old_fp}); nothing to do."
        )

    # -- Step 1: prove we hold the key this data was written under -----------
    active = await conn.fetchrow("""
        SELECT key_id, encrypted_key
        FROM   signing_keys
        WHERE  status = 'active'
        ORDER  BY created_at DESC
        LIMIT  1
        """)
    if active is None:
        raise RekeyAborted(
            "no active signing key exists, so there is nothing to prove the old key "
            "against. Refusing: a re-key that cannot verify the old key may be "
            "re-wrapping data it does not own. Nothing has been written."
        )
    try:
        decrypt_signing_key(bytes(active["encrypted_key"]), old_master_key)
    except SigningKeyDecryptionError as exc:
        raise RekeyAborted(
            f"ABORT BEFORE ANY WRITE: the old key (fingerprint={old_fp}) does not "
            f"unwrap the active signing key (key_id={active['key_id']}). You are not "
            "holding the key this data was written under; re-wrapping now would "
            "destroy it permanently. Nothing has been written."
        ) from exc

    stats: dict[str, int] = {
        "signing_keys_rewrapped": 0,
        "deks_rewrapped": 0,
        "deks_null_skipped": 0,
        "signing_keys_verified": 0,
        "deks_verified": 0,
    }

    try:
        async with conn.transaction():
            # -- Step 2: every signing key, active and retired ---------------
            for row in await conn.fetch(
                "SELECT key_id, encrypted_key FROM signing_keys ORDER BY key_id"
            ):
                try:
                    new_blob = rewrap_signing_key(
                        bytes(row["encrypted_key"]), old_master_key, new_master_key
                    )
                except SigningKeyDecryptionError as exc:
                    raise RekeyAborted(
                        f"ROLLBACK: signing key {row['key_id']} was not written under "
                        f"the old key (fingerprint={old_fp}); it may predate an earlier "
                        "rotation. Re-wrapping it is impossible and skipping it would "
                        "leave the rotation half-finished. Nothing has been written."
                    ) from exc
                await conn.execute(
                    "UPDATE signing_keys SET encrypted_key = $2 WHERE key_id = $1",
                    row["key_id"],
                    new_blob,
                )
                stats["signing_keys_rewrapped"] += 1

            # -- Step 3: every non-NULL wrapped DEK; NULL is a skip ----------
            for row in await conn.fetch(
                "SELECT id, wrapped_dek FROM memories WHERE wrapped_dek IS NOT NULL"
            ):
                try:
                    dek = unwrap_dek(bytes(row["wrapped_dek"]), old_master_key)
                except SigningKeyDecryptionError as exc:
                    raise RekeyAborted(
                        f"ROLLBACK: wrapped_dek for memory {row['id']} was not written "
                        f"under the old key (fingerprint={old_fp}). Nothing has been "
                        "written."
                    ) from exc
                await conn.execute(
                    "UPDATE memories SET wrapped_dek = $2 WHERE id = $1",
                    row["id"],
                    wrap_dek(dek, new_master_key),
                )
                stats["deks_rewrapped"] += 1

            stats["deks_null_skipped"] = (
                await conn.fetchval("SELECT count(*) FROM memories WHERE wrapped_dek IS NULL")
            ) or 0

            # -- Step 4: VERIFY BEFORE COMMIT, reading back what we wrote ----
            for row in await conn.fetch("SELECT key_id, encrypted_key FROM signing_keys"):
                _verify_signing_blob(bytes(row["encrypted_key"]), new_master_key, row["key_id"])
                stats["signing_keys_verified"] += 1
            for row in await conn.fetch(
                "SELECT id, wrapped_dek FROM memories WHERE wrapped_dek IS NOT NULL"
            ):
                _verify_wrapped_dek(bytes(row["wrapped_dek"]), new_master_key, row["id"])
                stats["deks_verified"] += 1

            if stats["signing_keys_verified"] != stats["signing_keys_rewrapped"] or (
                stats["deks_verified"] != stats["deks_rewrapped"]
            ):
                raise RekeyAborted(
                    "ROLLBACK: verified row count does not match re-wrapped row count "
                    f"({stats}). Rows changed underneath the transaction. Nothing has "
                    "been written."
                )

            if dry_run:
                raise _DryRun

            # -- Step 5 (the DB half): commit on exit from this block --------
    except _DryRun:
        log.warning("[rekey] --dry-run: verification passed, transaction rolled back.")
        stats["committed"] = 0
        return stats
    except RekeyAborted:
        raise
    except Exception as exc:  # noqa: BLE001 - anything unexpected must roll back
        raise RekeyAborted(
            f"ROLLBACK: unexpected failure during re-key; nothing has been written: {exc}"
        ) from exc

    stats["committed"] = 1
    log.info(
        "[rekey] committed: %d signing key(s), %d DEK(s) re-wrapped, %d NULL DEK(s) "
        "skipped; old fingerprint=%s new fingerprint=%s",
        stats["signing_keys_rewrapped"],
        stats["deks_rewrapped"],
        stats["deks_null_skipped"],
        old_fp,
        new_fp,
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_new_key(args: argparse.Namespace) -> MasterKey:
    """Read the new key from a file or an env var -- never from argv."""
    if args.new_key_file:
        raw = Path(args.new_key_file).read_bytes()
        # Match secret_env's normalisation: strip a UTF-8 BOM and whitespace.
        # The 2026-08-27 incident was a BOM in exactly this kind of file.
        text = raw.decode("utf-8-sig").strip()
        if not text:
            raise RekeyAborted(f"{args.new_key_file} is empty.")
        return MasterKey(text.encode("utf-8"))
    value = os.environ.get(args.new_key_env, "").strip()
    if not value:
        raise RekeyAborted(
            f"no new key: set {args.new_key_env} or pass --new-key-file. The new key is "
            "never accepted as a command-line argument (shell history, ps)."
        )
    return MasterKey(value.encode("utf-8"))


def _write_key_path(key_path: Path, new_master_key: MasterKey) -> str:
    """Write the new key and a ``.sha256`` sidecar; return the fingerprint."""
    material = bytes(new_master_key.key_bytes)
    digest = hashlib.sha256(material).hexdigest()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(material)
    try:
        key_path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows/NTFS
        pass
    key_path.with_suffix(key_path.suffix + ".sha256").write_text(
        f"{digest}  {key_path.name}\n", encoding="utf-8"
    )
    return digest[:16]


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = args.dsn or os.environ.get("PG_DSN", "")
    if not dsn:
        print("ERROR: no DSN: pass --dsn or set PG_DSN.", file=sys.stderr)
        return 2

    with MasterKey.from_env() as old_key:
        new_key = _load_new_key(args)
        try:
            old_fp = master_key_fingerprint(old_key)
            new_fp = master_key_fingerprint(new_key)
            print(f"old master key fingerprint: {old_fp}")
            print(f"new master key fingerprint: {new_fp}")
            if not args.yes and not args.dry_run:
                print(
                    "Refusing to re-key without --yes. Re-read the escrow copy of the "
                    "OLD key first: if this rotation is interrupted you need it.",
                    file=sys.stderr,
                )
                return 2

            conn = await asyncpg.connect(dsn)
            try:
                stats = await rekey_all(conn, old_key, new_key, dry_run=args.dry_run)
            finally:
                await conn.close()

            print(
                f"signing keys re-wrapped: {stats['signing_keys_rewrapped']} "
                f"(verified {stats['signing_keys_verified']})"
            )
            print(
                f"wrapped DEKs re-wrapped: {stats['deks_rewrapped']} "
                f"(verified {stats['deks_verified']}); "
                f"NULL DEKs skipped: {stats['deks_null_skipped']}"
            )
            if args.dry_run:
                print("--dry-run: rolled back. No key file written.")
                return 0

            if args.key_path:
                written_fp = _write_key_path(Path(args.key_path), new_key)
                print(f"wrote {args.key_path} (+ .sha256), fingerprint {written_fp}")
            else:
                print(
                    "NOTE: --key-path not given, so no key file was written. The "
                    "database now expects the NEW key; update the authoritative key "
                    "path before restarting anything."
                )
            print(
                "\nESCROW REMINDER -- the rotation is not finished until all three are "
                "true:\n"
                f"  1. the authoritative key path holds the key with fingerprint {new_fp};\n"
                "  2. the single escrow copy (password manager) has been REPLACED, and "
                "its\n"
                "     stored fingerprint matches;\n"
                "  3. every deployment has been restarted and its boot log shows "
                f"fingerprint={new_fp}.\n"
                "Do not add further copies: each one multiplies leak surface against a "
                "key\nwhose whole job is to be the single point of compromise."
            )
            return 0
        finally:
            new_key.zero()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-wrap every NCE wrapped blob under a new master key, atomically."
    )
    parser.add_argument("--dsn", default=None, help="Postgres DSN (default: $PG_DSN).")
    parser.add_argument("--new-key-file", default=None, help="File holding the new master key.")
    parser.add_argument(
        "--new-key-env",
        default=_NEW_KEY_ENV,
        help=f"Env var holding the new master key (default: {_NEW_KEY_ENV}).",
    )
    parser.add_argument(
        "--key-path",
        default=None,
        help="Authoritative key path to write the new key (plus a .sha256 sidecar).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run steps 1-4 and roll back; writes nothing.",
    )
    parser.add_argument("--yes", action="store_true", help="Required for a real re-key.")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except RekeyAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
