"""Ledger of applied SQL migrations.

``NCEEngine._apply_pg_migrations`` used to execute every file in
``nce/migrations/`` on every boot, relying on each one being individually
idempotent, with nothing recording what had run. Three consequences, all of
which bit in production on 2026-08-27 (fix recipe #2, item 3):

* all 54 files re-ran on every start, so boot cost grew with migration count;
* there was no way to ask what version a database was at, which is what made a
  stale-image-against-migrated-database skew invisible;
* a single non-idempotent file was fatal forever, not once -- exactly what
  migration ``041`` did when it granted on a sequence that a pre-BIGSERIAL
  database does not have.

This module owns the ledger table and the checksum convention. The DDL lives
here rather than in ``nce/schema.sql`` on purpose: the ledger has to exist
before any migration can be recorded, and a second copy of the definition is
the drift that recipe #1 item D is about.

Checksums are taken over **LF-normalised** text. The repository is developed on
Windows with ``core.autocrlf=true``, so the same migration blob is CRLF in one
working tree and LF in another; hashing raw bytes would make every file look
changed on the other platform and re-apply the whole set every boot -- the cost
this ledger exists to remove.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

#: Single source of truth for the ledger table. Idempotent; safe on every boot.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS applied_migrations (
    filename   TEXT        PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_UPSERT_SQL = """
INSERT INTO applied_migrations (filename, checksum, applied_at)
VALUES ($1, $2, now())
ON CONFLICT (filename)
DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = now()
"""


def migration_checksum(sql: str) -> str:
    """Return the checksum of a migration's *content*, ignoring line endings.

    Also ignores trailing whitespace at end of file, so a checkout that adds or
    strips a final newline does not present as a changed migration.
    """
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


async def ensure_ledger(conn: Any) -> None:
    """Create the ledger table if it does not exist."""
    await conn.execute(LEDGER_DDL)


async def applied_checksums(conn: Any) -> dict[str, str]:
    """Return ``{filename: checksum}`` for every recorded migration.

    A missing table is treated as an empty ledger rather than an error, so a
    database that predates the ledger simply re-applies everything once.
    """
    try:
        rows = await conn.fetch("SELECT filename, checksum FROM applied_migrations")
    except Exception as exc:
        log.warning("[PG] could not read applied_migrations (%s); assuming empty ledger", exc)
        return {}
    return {row["filename"]: row["checksum"] for row in rows}


async def record_applied(conn: Any, filename: str, checksum: str) -> None:
    """Record ``filename`` as applied at ``checksum``.

    Must run inside the same transaction as the migration itself: a recorded
    row for a migration that did not commit would skip it forever.
    """
    await conn.execute(_UPSERT_SQL, filename, checksum)


def migration_version(filename: str) -> int | None:
    """The numeric prefix of a migration filename, or None when unnumbered."""
    digits = ""
    for ch in filename:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def highest_version(filenames: Iterable[str]) -> int | None:
    """The highest numeric prefix across ``filenames``, or None if none carry one."""
    versions = [v for v in (migration_version(f) for f in filenames) if v is not None]
    return max(versions) if versions else None


def missing_from_image(
    image_filenames: Iterable[str], recorded_filenames: Iterable[str]
) -> list[str]:
    """Migrations the database has applied that this image does not contain.

    This is the version-skew direction that actually breaks a deployment: an
    image at commit X against a database migrated by commit Y > X. Its RLS
    allowlist predates tables that now exist, so ``verify_rls_catalog_consistency``
    fails closed -- and the error it prints reads like a code bug ("add to
    EXPECTED_TENANT_RLS_TABLES") rather than a stale deploy.

    The opposite direction needs no check: an image carrying migrations the
    database lacks applies them itself during startup.
    """
    known = set(image_filenames)
    return sorted(f for f in recorded_filenames if f not in known)


def should_skip(filename: str, checksum: str, applied: dict[str, str]) -> bool:
    """True when this exact file content is already recorded as applied.

    A *changed* file is deliberately re-applied rather than refused: migrations
    in this repo are edited in place when they turn out not to be idempotent
    (``041`` again), and refusing to boot on a legitimately corrected migration
    would be worse than re-running an idempotent one. The caller logs the
    re-application so the change is visible.
    """
    return applied.get(filename) == checksum
