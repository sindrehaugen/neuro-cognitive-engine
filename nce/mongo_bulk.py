"""Batch MongoDB reads for ``memory_archive`` collections (FIX-021 / FIX-024).

Replaces N+1 ``find_one`` loops with single ``$in`` queries per hydrate call.
"""

from __future__ import annotations

import inspect
import logging
import uuid
import weakref
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

log = logging.getLogger(__name__)

_INDEXED_CREATED_AT_COLLS: set[tuple[int, str]] = set()
_INDEX_FINALIZERS: set[int] = set()


def _discard_indexed_coll(coll_id: int) -> None:
    _INDEX_FINALIZERS.discard(coll_id)
    for k in [item for item in _INDEXED_CREATED_AT_COLLS if item[0] == coll_id]:
        _INDEXED_CREATED_AT_COLLS.discard(k)


async def ensure_created_at_index(collection: Any) -> bool:
    """Idempotently ensure ascending index on ``created_at`` for CoW & reconciliation scans."""
    if collection is None:
        return False
    coll_name = str(getattr(collection, "name", "") or "collection")
    coll_id = id(collection)
    cache_key = (coll_id, coll_name)
    if cache_key in _INDEXED_CREATED_AT_COLLS:
        return True

    create_fn = getattr(collection, "create_index", None)
    if not callable(create_fn):
        return False

    try:
        res = create_fn([("created_at", 1)], background=True)
        if inspect.isawaitable(res):
            await res
    except Exception as exc:
        log.debug("create_index(created_at) skipped for %s: %s", coll_name, exc)
        return False

    _INDEXED_CREATED_AT_COLLS.add(cache_key)
    if coll_id not in _INDEX_FINALIZERS:
        try:
            weakref.finalize(collection, _discard_indexed_coll, coll_id)
            _INDEX_FINALIZERS.add(coll_id)
        except TypeError:
            pass
    return True


def stamp_mongo_document(
    doc: dict[str, Any],
    *,
    saga_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a copy of *doc* stamped with UTC ``created_at`` and ``saga_id``."""
    ts = created_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    out = dict(doc)
    out.setdefault("created_at", ts)
    out.setdefault("ingested_at", ts)
    if not out.get("saga_id"):
        out["saga_id"] = str(saga_id) if saga_id else str(uuid.uuid4())
    return out


_MAX_REFS: int = 10_000
_BATCH_SIZE: int = 500
_QUERY_TIMEOUT_MS: int = 5_000
_ALLOWED_FIELDS: frozenset[str] = frozenset({"raw_data", "raw_code"})


def _safe_object_id(key: str) -> ObjectId | None:
    """Parse a hex string into ObjectId; return None if invalid."""
    try:
        return ObjectId(key)
    except Exception:
        return None


def normalize_payload_ref(payload_ref: str | ObjectId | None) -> str | None:
    if payload_ref is None:
        return None
    if isinstance(payload_ref, ObjectId):
        return str(payload_ref)
    return str(payload_ref)


def _normalize_and_validate_refs(refs: Iterable[str | ObjectId | None]) -> list[ObjectId]:
    """Deduplicate, normalize, and validate a sequence of payload refs.

    Raises ValueError if the number of unique refs exceeds _MAX_REFS.
    """
    uniq: dict[str, None] = {}
    for ref in refs:
        key = normalize_payload_ref(ref)
        if not key or key in uniq:
            continue
        uniq[key] = None

    if not uniq:
        return []
    if len(uniq) > _MAX_REFS:
        raise ValueError(f"Too many payload refs: {len(uniq)} exceeds limit of {_MAX_REFS}")

    oids: list[ObjectId] = []
    for key in uniq:
        oid = _safe_object_id(key)
        if oid is None:
            log.warning(
                "Invalid Mongo payload_ref (prefix=%s): not a valid ObjectId",
                key[:8],
            )
        else:
            oids.append(oid)
    return oids


async def _fetch_field_by_refs(
    collection,
    refs: Iterable[str | ObjectId | None],
    *,
    field: str,
    decode_bytes: bool = True,
) -> dict[str, str]:
    """Map episode/code ``_id`` (str) → *field* value.

    By default the value is coerced to ``str`` (legacy behaviour).  Pass
    ``decode_bytes=False`` to preserve a ``bytes`` payload verbatim — required
    by callers that must hand DEK-encrypted ciphertext (Part II.4) to
    :func:`nce.envelope.maybe_decrypt_raw_data` without corrupting it via
    ``str(bytes)``.
    """
    if field not in _ALLOWED_FIELDS:
        raise ValueError(f"field {field!r} is not allowed. Allowed: {sorted(_ALLOWED_FIELDS)}")

    oids = _normalize_and_validate_refs(refs)
    if not oids:
        return {}

    out: dict[str, str] = {}
    coll_name = getattr(collection, "name", "collection")
    for i in range(0, len(oids), _BATCH_SIZE):
        batch = oids[i : i + _BATCH_SIZE]
        try:
            cursor = collection.find(
                {"_id": {"$in": batch}},
                projection={field: 1},
                max_time_ms=_QUERY_TIMEOUT_MS,
            )
            async for doc in cursor:
                rid = normalize_payload_ref(doc.get("_id"))
                if not rid:
                    continue
                raw = doc.get(field)
                if raw is None:
                    out[rid] = ""
                elif isinstance(raw, (bytes, bytearray, memoryview)) and not decode_bytes:
                    out[rid] = bytes(raw)  # type: ignore[assignment]
                else:
                    out[rid] = raw if isinstance(raw, str) else str(raw)
        except Exception as exc:
            log.error(
                "Batch Mongo hydrate failed batch=%d/%d (%s): %s",
                i // _BATCH_SIZE + 1,
                (len(oids) + _BATCH_SIZE - 1) // _BATCH_SIZE,
                coll_name,
                type(exc).__name__,
            )

    return out


async def fetch_episodes_raw_by_ref(
    db,
    refs: Iterable[str | ObjectId | None],
    *,
    field: str = "raw_data",
    decode_bytes: bool = True,
) -> dict[str, str]:
    """Map episode ``_id`` (str) → ``raw_data`` (or *field*) text.

    ``decode_bytes=False`` preserves a ``bytes`` ciphertext payload so callers
    can decrypt it (Part II.4); see :func:`_fetch_field_by_refs`.
    """
    return await _fetch_field_by_refs(db.episodes, refs, field=field, decode_bytes=decode_bytes)


async def fetch_episode_previews_by_ref(
    db,
    refs: Iterable[str | ObjectId | None],
    *,
    max_preview_len: int = 200,
) -> dict[str, str]:
    """Map episode ``_id`` (str) → short preview (summary, else raw_data)."""
    oids = _normalize_and_validate_refs(refs)
    if not oids:
        return {}

    out: dict[str, str] = {}
    coll = db.episodes
    coll_name = getattr(coll, "name", "episodes")
    for i in range(0, len(oids), _BATCH_SIZE):
        batch = oids[i : i + _BATCH_SIZE]
        try:
            cursor = coll.find(
                {"_id": {"$in": batch}},
                projection={"summary": 1, "raw_data": 1},
                max_time_ms=_QUERY_TIMEOUT_MS,
            )
            async for doc in cursor:
                rid = normalize_payload_ref(doc.get("_id"))
                if not rid:
                    continue
                text = doc.get("summary") or doc.get("raw_data")
                out[rid] = ("" if text is None else str(text))[:max_preview_len]
        except Exception as exc:
            log.error(
                "Batch Mongo preview hydrate failed batch=%d/%d (%s): %s",
                i // _BATCH_SIZE + 1,
                (len(oids) + _BATCH_SIZE - 1) // _BATCH_SIZE,
                coll_name,
                type(exc).__name__,
            )

    return out


async def fetch_code_files_raw_by_ref(
    db,
    refs: Iterable[str | ObjectId | None],
    *,
    field: str = "raw_code",
) -> dict[str, str]:
    """Map code_files ``_id`` (str) → ``raw_code`` (or *field*) text."""
    return await _fetch_field_by_refs(db.code_files, refs, field=field)
