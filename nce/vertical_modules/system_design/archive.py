"""
nce/vertical_modules/system_design/archive.py
==============================================
Module 6, Wave 17b (B067h2) — **the retired-archive sweep.**

W17 (:mod:`nce.vertical_modules.system_design.retire`) gave the codebase its
first delete path and made the default a *soft* retire: a node the user removed
from the canvas keeps its rows and gets
``system_design_node_state.status = 'decommissioning'``.  That is recoverable
by design, and it is also unbounded — the retired estate only ever grows.  This
file is the sweep that bounds it, and it is an **archive** sweep rather than a
delete sweep: nothing leaves the database until a copy of it has been written
to object storage *and read back*.

THE ORDERING IS THE WHOLE FILE
-------------------------------
::

    serialise  ->  store  ->  VERIFY READABLE BACK  ->  (event_log + drop)

Only the last two steps share a transaction, and they share it because the
archive key must be durable in ``event_log`` for exactly as long as the rows are
gone: a drop that commits without its key leaves an object nobody can name, and
a key that commits without its drop is merely a harmless duplicate record.

**A drop that trusts a write it did not read back is the defect.**  ``put_object``
returning without raising is a statement about the client's socket, not about
the bucket; a mis-configured endpoint, a silently-wrong bucket policy, or an
eventually-consistent gateway can all absorb a PUT whose object is not there
afterwards.  :func:`_read_back` therefore re-reads the object by key and
compares it to the bytes that were sent, and any mismatch — or any exception on
the way — raises :class:`ArchiveNotReadableError`, which skips the drop and
leaves the node exactly where it was.  The sweep is allowed to do nothing.  It
is not allowed to drop on faith.

Every step of that order is separately RED-tested; see
``tests/test_system_design_archive.py``.

OFF BY DEFAULT
---------------
:data:`SWEEP_ENABLED_ENV` is read through :func:`nce.config._bool_env` at *call*
time — not captured as a ``cfg`` class attribute — so the flag can be flipped in
a test without rebuilding the config singleton, and so a deployment that never
sets it never runs a delete path.  :func:`sweep_enabled` is the only reader.

WHAT IS A CANDIDATE, AND WHY ``NULL`` IS NOT
----------------------------------------------
A candidate is a ``system_design_node_state`` row whose ``status`` is
**exactly** :data:`SWEEP_STATUS` (``'decommissioning'``) and whose ``updated_at``
is older than the retention window.

``status`` is nullable and without a default (migration 061): ``NULL`` means
"we hold data for this node, nobody has declared its lifecycle".  W17 denies on
that and on a missing row, and the whole legacy as-built estate looks like it —
so **absence is not a candidate**, and that one-way door is what protects
equipment nobody has declared.

The door is checked twice and on purpose.  SQL's three-valued logic already
makes ``status = 'decommissioning'`` false for ``NULL``, but that exclusion is
*implicit* — a future editor widening the predicate to ``status IS DISTINCT
FROM 'active'``, or adding an ``OR status IS NULL`` branch for "unmanaged"
nodes, breaks it without touching anything that looks like a guard.  So
:func:`_is_candidate` re-reads the status of every row the query returned, in
Python, before that row is allowed anywhere near the drop.  Both gates are
independently RED-testable; a single gate would leave the other's mutation
green.

INTERRUPT-IDEMPOTENCE COMES FROM THE KEY, NOT FROM A LEDGER
-------------------------------------------------------------
:func:`archive_key` is a pure function of ``(namespace_id, node_label)``.  It
contains no timestamp, no UUID and no attempt counter.  A crash anywhere in the
sequence therefore leaves the *next* run computing the same key: the re-run
overwrites the object it already wrote instead of adding a second one, and the
window between "stored" and "dropped" heals itself rather than accumulating.
A key carrying a run identifier would be equally correct on the happy path and
would leak one orphaned copy per interruption — which is why the determinism is
asserted by its own test rather than left as a property of the format string.

TENANCY
--------
Every statement carries an explicit ``namespace_id`` predicate.  Owner pools
bypass ``FORCE ROW LEVEL SECURITY``, so that predicate — not RLS — is the tenant
boundary, and the archive key embeds the namespace so two tenants holding a
byte-identical node label cannot collide in the bucket.

WHAT THIS FILE DOES **NOT** ADD
--------------------------------
No new ``EventType``.  :data:`ARCHIVE_EVENT_TYPE` is the existing
``'system_design_authored'`` audit type — ``nce/event_types.py`` and
``nce/replay.py`` are a pair (``ForkedReplay`` validates handler coverage on
construction, repo-wide), so a new value is a two-file change outside this
wave.  The archive record is distinguished by its ``op`` param, not by a new
type.

No new tool, route, migration or node type — and therefore none of the twelve
count sites, no ``docs/API.md`` entry and no ``node-ownership.json`` change.

REUSE, NOT REIMPLEMENTATION
-----------------------------
The drop itself is :func:`~nce.vertical_modules.system_design.retire._delete_permanently`,
imported rather than copied.  Its five DELETEs are each obligatory (no foreign
key ties a side-table row to its node — D12), each already has its own RED test
in ``tests/test_system_design_retire.py``, and a second copy here would be a
second thing to keep correct.  The import reaches a private name deliberately:
this module is the only other caller, it lives in the same package, and the
alternative is duplicating the one delete statement set in the codebase.

The sweep's shape — hourly-ish cron tick, archive-then-drop, never row-level
deletes of things that are still referenced, MinIO manifest first — is
``nce/garbage_collector.py``'s Batch 125 partition retention, followed rather
than reinvented.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from nce import config as _config
from nce.config import cfg
from nce.event_log import append_event
from nce.vertical_modules.system_design.retire import (
    _delete_permanently,
    _port_labels_of,
)

log = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_AGENT_ID",
    "ARCHIVE_EVENT_TYPE",
    "ARCHIVE_OP",
    "ARCHIVE_PREFIX",
    "ArchiveNotReadableError",
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_ENV",
    "SWEEP_ENABLED_ENV",
    "SWEEP_STATUS",
    "archive_key",
    "retention_cutoff",
    "run_design_archive_sweep",
    "sweep_enabled",
]

#: The feature flag.  OFF unless explicitly set — this path deletes data.
SWEEP_ENABLED_ENV = "NCE_SYSTEM_DESIGN_ARCHIVE_SWEEP_ENABLED"

#: How long a retired node is kept in the database before it is archived out.
RETENTION_DAYS_ENV = "NCE_SYSTEM_DESIGN_ARCHIVE_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 90

#: The ONLY status this sweep will act on.  ``NULL`` is not this value and an
#: absent row has no value at all; see the module docstring.
SWEEP_STATUS = "decommissioning"

#: Object prefix inside ``cfg.NCE_ANCHOR_BUCKET`` — the same bucket Batch 125's
#: partition manifests use, under a prefix of this module's own.
ARCHIVE_PREFIX = "system_design_archive"

#: Existing event type (see the module docstring — no new ``EventType``).
ARCHIVE_EVENT_TYPE = "system_design_authored"

#: The param that distinguishes an archive record from an authoring record.
ARCHIVE_OP = "archived"

ARCHIVE_AGENT_ID = "system_design_archive_sweep"


class ArchiveNotReadableError(RuntimeError):
    """The stored object could not be read back, or read back different bytes.

    Raised between the store and the drop.  It is not a failure of the sweep so
    much as the sweep working: the node keeps its rows and the next run tries
    again against the same deterministic key.
    """


def sweep_enabled() -> bool:
    """Whether the sweep may run at all.  Default **False**.

    Read at call time rather than bound onto ``cfg`` so that the default is a
    property of this function — the one place a reviewer has to look — and so
    that no import-order accident can turn a delete path on.
    """
    return _config._bool_env(SWEEP_ENABLED_ENV, False)


def retention_days() -> int:
    """The retention window in days; :data:`DEFAULT_RETENTION_DAYS` when unset.

    A value that does not parse, or is not strictly positive, falls back to the
    default rather than to zero: a malformed env var must not silently shorten
    the window to "everything retired is eligible right now".
    """
    raw = os.getenv(RETENTION_DAYS_ENV)
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        parsed = int(raw.strip())
    except (AttributeError, ValueError):
        log.warning("archive sweep: unparseable %s=%r — using default", RETENTION_DAYS_ENV, raw)
        return DEFAULT_RETENTION_DAYS
    if parsed <= 0:
        log.warning("archive sweep: non-positive %s=%r — using default", RETENTION_DAYS_ENV, raw)
        return DEFAULT_RETENTION_DAYS
    return parsed


def retention_cutoff(now: datetime | None = None) -> datetime:
    """Rows updated at or after this instant are inside the window and are kept."""
    reference = now if now is not None else datetime.now(timezone.utc)
    return reference - timedelta(days=retention_days())


def archive_key(namespace_id: UUID | str, node_label: str) -> str:
    """The object name for one node's archive.

    Pure function of tenant + label.  See "INTERRUPT-IDEMPOTENCE" in the module
    docstring: nothing time-varying may enter this string.
    """
    return f"{ARCHIVE_PREFIX}/{namespace_id}/{node_label}.json"


def _is_candidate(status: Any) -> bool:
    """The second, independent read of the one-way door.

    ``None`` — an undeclared lifecycle — is refused here as well as by the
    query's ``status = $2``.  Two gates because a widened query predicate is the
    realistic way this breaks, and a single gate leaves that mutation green.
    """
    return status is not None and status == SWEEP_STATUS


def _jsonable(value: Any) -> Any:
    """Coerce asyncpg row values into something ``json.dumps`` accepts.

    The archive is only recoverable if it is readable, so this is deliberately
    total: anything unrecognised becomes its ``str()`` rather than raising and
    stranding a node that can then never be swept.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _rows_to_json(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [{str(key): _jsonable(val) for key, val in dict(row).items()} for row in rows]


async def _serialise_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    labels: list[str],
) -> bytes:
    """STEP 1 — read everything the drop is about to remove, as bytes.

    Reads the same five tables ``_delete_permanently`` deletes from, over the
    *expanded* label set (a DEVICE's PORTs go with it), so the archive is a
    superset of what disappears rather than a summary of it.  ``sort_keys`` and
    the ordered queries make the encoding deterministic, which is what lets the
    read-back compare by bytes instead of by parsed structure.
    """
    ns = str(ns_uuid)
    nodes = await conn.fetch(
        """
        SELECT * FROM kg_nodes
         WHERE namespace_id = $1::uuid AND label = ANY($2::text[])
         ORDER BY label
        """,
        ns,
        labels,
    )
    state = await conn.fetch(
        """
        SELECT * FROM system_design_node_state
         WHERE namespace_id = $1::uuid AND node_label = ANY($2::text[])
         ORDER BY node_label
        """,
        ns,
        labels,
    )
    geometry = await conn.fetch(
        """
        SELECT * FROM system_design_geometry
         WHERE namespace_id = $1::uuid AND node_label = ANY($2::text[])
           AND version IS NULL
         ORDER BY node_label
        """,
        ns,
        labels,
    )
    capabilities = await conn.fetch(
        """
        SELECT * FROM system_design_device_capabilities
         WHERE namespace_id = $1::uuid AND node_label = ANY($2::text[])
         ORDER BY node_label
        """,
        ns,
        labels,
    )
    edges = await conn.fetch(
        """
        SELECT * FROM kg_edges
         WHERE namespace_id = $1::uuid
           AND (subject_label = ANY($2::text[]) OR object_label = ANY($2::text[]))
         ORDER BY subject_label, predicate, object_label
        """,
        ns,
        labels,
    )

    document = {
        "archive_version": 1,
        "namespace_id": ns,
        "labels": sorted(labels),
        "kg_nodes": _rows_to_json(list(nodes)),
        "system_design_node_state": _rows_to_json(list(state)),
        "system_design_geometry": _rows_to_json(list(geometry)),
        "system_design_device_capabilities": _rows_to_json(list(capabilities)),
        "kg_edges": _rows_to_json(list(edges)),
    }
    return json.dumps(document, sort_keys=True).encode("utf-8")


async def _store(minio_client: Any, bucket: str, key: str, blob: bytes) -> None:
    """STEP 2 — put the object.  Returning from here proves nothing on its own."""

    def _put() -> None:
        minio_client.put_object(
            bucket,
            key,
            io.BytesIO(blob),
            len(blob),
            content_type="application/json",
        )

    await asyncio.to_thread(_put)


async def _read_back(minio_client: Any, bucket: str, key: str, blob: bytes) -> None:
    """STEP 3 — read the object back by key and compare it to what was sent.

    🔴 This is the step the wave exists for.  It runs BEFORE any drop and it
    fails closed: an exception on the way, a short read, or a byte that differs
    all raise :class:`ArchiveNotReadableError`, and the caller keeps the rows.
    """

    def _get() -> bytes:
        response = minio_client.get_object(bucket, key)
        try:
            return bytes(response.read())
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
            release = getattr(response, "release_conn", None)
            if release is not None:
                release()

    try:
        stored = await asyncio.to_thread(_get)
    except Exception as exc:  # noqa: BLE001 — any failure to re-read is fail-closed
        raise ArchiveNotReadableError(
            f"archive object {bucket}/{key} could not be read back: {type(exc).__name__}: {exc}"
        ) from exc

    if stored != blob:
        raise ArchiveNotReadableError(
            f"archive object {bucket}/{key} read back {len(stored)} byte(s) that differ from "
            f"the {len(blob)} byte(s) written — NOT dropping"
        )


async def _record_and_drop(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    node_type: str,
    key: str,
    bucket: str,
    labels: list[str],
) -> dict[str, Any]:
    """STEP 4 — the archive key into ``event_log``, then the drop, one transaction.

    The order inside the transaction is key-then-drop so that a constraint
    failure on the ``event_log`` insert (WORM triggers, signing key, sequence
    contention) aborts before anything is removed.  Both statements commit or
    neither does, which is what makes "the rows are gone" and "the object has a
    name" the same fact.
    """
    async with conn.transaction():
        await append_event(
            conn=conn,
            namespace_id=ns_uuid,
            agent_id=ARCHIVE_AGENT_ID,
            event_type=ARCHIVE_EVENT_TYPE,
            params={
                "op": ARCHIVE_OP,
                "node_label": label,
                "node_type": node_type,
                "archive_bucket": bucket,
                "archive_key": key,
                "archived_labels": sorted(labels),
            },
        )
        return await _delete_permanently(conn, ns_uuid, [label], {label: node_type})


async def _fetch_candidates(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    cutoff: datetime,
) -> list[asyncpg.Record]:
    """Retired rows past the window, for one tenant.

    ``status = $2`` is gate one of the one-way door (``NULL`` is not equal to
    anything, so an undeclared lifecycle never appears here) and
    ``updated_at < $3`` is the window.  The ``namespace_id`` predicate is the
    tenant boundary — owner pools bypass FORCE RLS.
    """
    return list(
        await conn.fetch(
            """
            SELECT node_label, node_type, status, updated_at
              FROM system_design_node_state
             WHERE namespace_id = $1::uuid
               AND status       = $2
               AND updated_at   < $3
             ORDER BY node_label
            """,
            str(ns_uuid),
            SWEEP_STATUS,
            cutoff,
        )
    )


async def archive_and_drop_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    minio_client: Any,
    ns_uuid: UUID,
    label: str,
    node_type: str,
    *,
    bucket: str | None = None,
) -> dict[str, Any]:
    """serialise -> store -> verify readable back -> (event_log + drop).

    The four steps are four calls in this order and nothing may be moved between
    them.  Returns the ``_delete_permanently`` receipt with the archive key
    added.  Raises :class:`ArchiveNotReadableError` — before any drop — when the
    object cannot be re-read.
    """
    target_bucket = bucket if bucket is not None else cfg.NCE_ANCHOR_BUCKET
    port_labels = await _port_labels_of(conn, ns_uuid, [label] if node_type == "DEVICE" else [])
    labels = [label] + [p for p in port_labels if p != label]

    blob = await _serialise_node(conn, ns_uuid, labels)
    key = archive_key(ns_uuid, label)
    await _store(minio_client, target_bucket, key, blob)
    await _read_back(minio_client, target_bucket, key, blob)

    receipt = await _record_and_drop(conn, ns_uuid, label, node_type, key, target_bucket, labels)
    receipt["archive_key"] = key
    receipt["archive_bucket"] = target_bucket
    return receipt


async def _sweep_namespace(
    pg_pool: asyncpg.Pool,
    minio_client: Any,
    ns_uuid: UUID,
    cutoff: datetime,
    bucket: str,
    result: dict[str, Any],
) -> None:
    """One tenant's pass.  Each node is independent; one failure never stops the rest."""
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_uuid) as conn:
        rows = await _fetch_candidates(conn, ns_uuid, cutoff)
        result["examined"] += len(rows)
        for row in rows:
            status = row["status"]
            if not _is_candidate(status):
                # Gate two.  Unreachable while gate one is intact — and that is
                # the point: it is what a widened query predicate runs into.
                result["skipped_not_candidate"] += 1
                log.warning(
                    "archive sweep: query returned ns=%s label=%s with status=%r — skipping",
                    ns_uuid,
                    row["node_label"],
                    status,
                )
                continue
            label = str(row["node_label"])
            node_type = str(row["node_type"])
            try:
                await archive_and_drop_node(
                    conn, minio_client, ns_uuid, label, node_type, bucket=bucket
                )
            except ArchiveNotReadableError as exc:
                result["skipped_unreadable"] += 1
                log.error("archive sweep: %s", exc)
            except Exception as exc:  # noqa: BLE001 — one bad node must not stop the sweep
                result["failed"] += 1
                log.error(
                    "archive sweep: ns=%s label=%s failed: %s: %s",
                    ns_uuid,
                    label,
                    type(exc).__name__,
                    exc,
                )
            else:
                result["archived"] += 1


async def run_design_archive_sweep(
    pg_pool: asyncpg.Pool,
    minio_client: Any,
    *,
    namespaces: list[UUID] | None = None,
    now: datetime | None = None,
    bucket: str | None = None,
) -> dict[str, Any]:
    """Archive and drop retired system-design nodes past the retention window.

    Off unless :data:`SWEEP_ENABLED_ENV` is set: the disabled return is
    ``{"enabled": False, ...}`` with every counter at zero and **no connection
    acquired**, so a deployment that never sets the flag never touches the
    delete path at all.

    Returns counters: ``examined``, ``archived``, ``skipped_not_candidate``,
    ``skipped_unreadable``, ``failed``.
    """
    result: dict[str, Any] = {
        "enabled": False,
        "examined": 0,
        "archived": 0,
        "skipped_not_candidate": 0,
        "skipped_unreadable": 0,
        "failed": 0,
    }
    if not sweep_enabled():
        return result
    result["enabled"] = True

    if minio_client is None:
        log.warning(
            "archive sweep: no MinIO client — nothing can be archived, so nothing is dropped"
        )
        return result

    target_bucket = bucket if bucket is not None else cfg.NCE_ANCHOR_BUCKET
    cutoff = retention_cutoff(now)

    if namespaces is None:
        async with pg_pool.acquire(timeout=30.0) as conn:
            rows = await conn.fetch("SELECT id FROM namespaces")
        namespaces = [row["id"] for row in rows]

    for ns_uuid in namespaces:
        try:
            await _sweep_namespace(pg_pool, minio_client, ns_uuid, cutoff, target_bucket, result)
        except Exception as exc:  # noqa: BLE001 — one bad tenant must not stop the sweep
            result["failed"] += 1
            log.error(
                "archive sweep: namespace %s failed: %s: %s", ns_uuid, type(exc).__name__, exc
            )

    log.info("archive sweep complete (cutoff=%s): %s", cutoff.isoformat(), result)
    return result
