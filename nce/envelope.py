"""
NCE envelope-encryption subsystem (Part II.4 — Provable Forgetting).

Data Encryption Key (DEK) lifecycle for per-memory (or per-subject) content
encryption.  The raw content that fans out to MongoDB ``episodes.raw_data`` is
encrypted under a 256-bit DEK; that DEK is itself **wrapped** (envelope-encrypted)
under the environment master key and stored alongside the memory row
(``memories.wrapped_dek`` + ``memories.dek_key_id``).

Crypto reuse
------------
This module deliberately does **not** roll its own key-wrapping crypto.  It
reuses the audited AES-256-GCM envelope already implemented in
:mod:`nce.signing`:

* :func:`nce.signing.encrypt_signing_key` wraps the raw DEK under the master key
  (Argon2id or PBKDF2 @ 600K KDF, per-blob random salt + nonce, versioned wire
  format ``TC3/TC4``).
* :func:`nce.signing.decrypt_signing_key` unwraps it (auto-detecting the wire
  format; raises :class:`~nce.signing.SigningKeyDecryptionError` on a wrong key
  or corrupted blob).
* :class:`nce.signing.SecureKeyBuffer` holds the transient plaintext DEK and
  zeroes it on context exit.

The DEK itself is used for **data** encryption (the memory payload) via plain
AES-256-GCM with a fresh per-call nonce; the wire format is
``b'TCDEK\\x01' || nonce (12) || ciphertext+tag``.

Provable-forgetting property
-----------------------------
Destroying the wrapped DEK (zeroing the ``memories.wrapped_dek`` column) renders
the corresponding ``episodes.raw_data`` ciphertext permanently undecryptable —
the master key alone cannot recover the plaintext.  Wiring the read paths and
``shred_memory`` op is Batch 46; this batch ships only the subsystem + schema.

``NCE_MASTER_KEY`` is environment-only (see :func:`nce.signing.require_master_key`)
and is never read from or written to any database/settings table.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nce.signing import (
    MasterKey,
    MasterKeyMissingError,
    SecureKeyBuffer,
    SigningError,
    SigningKeyDecryptionError,
    canonical_json,
    decrypt_signing_key,
    encrypt_fast_data_key,
    master_key_fingerprint_bytes,
    require_master_key,
)

log = logging.getLogger(__name__)
_TOMBSTONE_IO_LOCK = threading.Lock()
_BACKUP_KEY_FP_LABEL = b"nce-backup-key-fingerprint-v1"

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# AES-256 → 32-byte DEK.
_DEK_SIZE: int = 32
# 96-bit nonce for AES-256-GCM payload encryption (matches signing._NONCE_SIZE).
_NONCE_SIZE: int = 12
# Wire-format prefix for DEK-encrypted payloads.  Distinguishes payload
# ciphertext (this module) from key-wrapping blobs (TC2/TC3/TC4 in signing.py).
_DEK_PAYLOAD_PREFIX: bytes = b"TCDEK\x01"


# ---------------------------------------------------------------------------
# Bounded TTL LRU cache for unwrapped DEKs (zeroes bytearray on eviction)
# ---------------------------------------------------------------------------

_UNWRAPPED_DEK_CACHE_MAXSIZE: int = 512
_UNWRAPPED_DEK_CACHE_TTL_S: float = 300.0
_UNWRAPPED_DEK_CACHE: OrderedDict[tuple[bytes, bytes], tuple[bytearray, float]] = OrderedDict()


def _zero_and_evict_dek(key: tuple[bytes, bytes]) -> None:
    entry = _UNWRAPPED_DEK_CACHE.pop(key, None)
    if entry is not None:
        buf, _ = entry
        for i in range(len(buf)):
            buf[i] = 0


def clear_unwrapped_dek_cache() -> None:
    """Zero and evict all cached unwrapped DEKs (called on shred/forget/key rotation)."""
    for k in list(_UNWRAPPED_DEK_CACHE.keys()):
        _zero_and_evict_dek(k)


def _unwrap_dek_cached(wrapped_dek: bytes, master_key: MasterKey) -> bytes:
    """Unwrap *wrapped_dek* under *master_key* with bounded TTL LRU caching."""
    now = time.monotonic()
    for k, (_, exp) in list(_UNWRAPPED_DEK_CACHE.items()):
        if now > exp:
            _zero_and_evict_dek(k)

    fp = master_key_fingerprint_bytes(master_key)
    digest = hashlib.sha256(wrapped_dek).digest()
    cache_key = (fp, digest)

    entry = _UNWRAPPED_DEK_CACHE.get(cache_key)
    if entry is not None:
        buf, exp = entry
        if now <= exp:
            _UNWRAPPED_DEK_CACHE.move_to_end(cache_key)
            return bytes(buf)
        _zero_and_evict_dek(cache_key)

    dek = unwrap_dek(wrapped_dek, master_key)
    while len(_UNWRAPPED_DEK_CACHE) >= _UNWRAPPED_DEK_CACHE_MAXSIZE:
        oldest_key = next(iter(_UNWRAPPED_DEK_CACHE))
        _zero_and_evict_dek(oldest_key)
    _UNWRAPPED_DEK_CACHE[cache_key] = (bytearray(dek), now + _UNWRAPPED_DEK_CACHE_TTL_S)
    return dek


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EnvelopeError(SigningError):
    """Base class for envelope-encryption errors."""


class DEKDecryptionError(EnvelopeError):
    """Raised when AES-256-GCM payload decryption fails (wrong DEK or corruption)."""


# ---------------------------------------------------------------------------
# DEK lifecycle
# ---------------------------------------------------------------------------


def generate_dek() -> bytes:
    """
    Generate a fresh 256-bit Data Encryption Key.

    Returns a 32-byte CSPRNG key suitable for AES-256-GCM.  Callers should wrap
    the returned bytes in a :class:`~nce.signing.SecureKeyBuffer` so the
    plaintext DEK is zeroed once it has been wrapped under the master key.
    """
    return os.urandom(_DEK_SIZE)


def new_dek_key_id() -> str:
    """
    Return a fresh opaque identifier for a DEK, stored in ``memories.dek_key_id``.

    The id carries no key material — it is an audit/correlation handle so a
    deletion receipt can name which DEK was destroyed without revealing it.
    """
    return f"dek-{uuid.uuid4().hex}"


def wrap_dek(dek: bytes, master_key: MasterKey) -> bytes:
    """
    Wrap (envelope-encrypt) a raw *dek* under the master key.

    Reuses :func:`nce.signing.encrypt_signing_key` — the same AES-256-GCM
    envelope (Argon2id/PBKDF2 KDF, per-blob salt + nonce, versioned wire format)
    that protects signing keys.  The returned bytes are what is stored in
    ``memories.wrapped_dek``.

    Raises :class:`ValueError` if *dek* is not exactly 32 bytes, or if
    *master_key* has been zeroed.
    """
    if len(dek) != _DEK_SIZE:
        raise ValueError(f"DEK must be exactly {_DEK_SIZE} bytes; got {len(dek)}.")
    wrapped = encrypt_fast_data_key(dek, master_key)
    now = time.monotonic()
    fp = master_key_fingerprint_bytes(master_key)
    cache_key = (fp, hashlib.sha256(wrapped).digest())
    while len(_UNWRAPPED_DEK_CACHE) >= _UNWRAPPED_DEK_CACHE_MAXSIZE:
        oldest_key = next(iter(_UNWRAPPED_DEK_CACHE))
        _zero_and_evict_dek(oldest_key)
    _UNWRAPPED_DEK_CACHE[cache_key] = (bytearray(dek), now + _UNWRAPPED_DEK_CACHE_TTL_S)
    return wrapped


def unwrap_dek(wrapped_dek: bytes, master_key: MasterKey) -> bytes:
    """
    Unwrap a *wrapped_dek* produced by :func:`wrap_dek`.

    Reuses :func:`nce.signing.decrypt_signing_key`, which auto-detects the wire
    format and raises :class:`~nce.signing.SigningKeyDecryptionError` on a wrong
    master key or corrupted blob.  Callers should immediately wrap the returned
    bytes in a :class:`~nce.signing.SecureKeyBuffer`.

    Raises :class:`~nce.signing.SigningKeyDecryptionError` on authentication
    failure.
    """
    return decrypt_signing_key(wrapped_dek, master_key)


# ---------------------------------------------------------------------------
# Payload encryption under a DEK
# ---------------------------------------------------------------------------


def encrypt_with_dek(plaintext: bytes, dek: bytes) -> bytes:
    """
    AES-256-GCM encrypt *plaintext* under a raw 32-byte *dek*.

    Wire format: ``b'TCDEK\\x01' || nonce (12) || ciphertext+tag``.  A fresh
    random nonce is generated per call.

    Raises :class:`ValueError` if *dek* is not exactly 32 bytes.
    """
    if len(dek) != _DEK_SIZE:
        raise ValueError(f"DEK must be exactly {_DEK_SIZE} bytes; got {len(dek)}.")
    nonce = os.urandom(_NONCE_SIZE)
    # Wrap the DEK in SecureKeyBuffer so it is zeroed the moment encryption
    # completes — the raw key must not linger past this scope.
    with SecureKeyBuffer(dek) as dek_buf:
        ciphertext_and_tag = AESGCM(bytes(dek_buf)).encrypt(nonce, plaintext, None)
    return _DEK_PAYLOAD_PREFIX + nonce + ciphertext_and_tag


def decrypt_with_dek(blob: bytes, dek: bytes) -> bytes:
    """
    Decrypt a *blob* produced by :func:`encrypt_with_dek` under a raw *dek*.

    Raises :class:`DEKDecryptionError` on authentication failure (wrong DEK,
    truncated/corrupted blob, or a missing/incorrect wire-format prefix).
    """
    if len(dek) != _DEK_SIZE:
        raise ValueError(f"DEK must be exactly {_DEK_SIZE} bytes; got {len(dek)}.")
    if not blob.startswith(_DEK_PAYLOAD_PREFIX):
        raise DEKDecryptionError(
            "payload blob is missing the expected DEK wire-format prefix "
            "(not produced by encrypt_with_dek, or corrupted)."
        )
    tail = blob[len(_DEK_PAYLOAD_PREFIX) :]
    if len(tail) < _NONCE_SIZE + 16:
        raise DEKDecryptionError(f"payload blob is too short to be valid (got {len(blob)} bytes).")
    nonce = tail[:_NONCE_SIZE]
    ct_and_tag = tail[_NONCE_SIZE:]
    # Wrap the DEK in SecureKeyBuffer — zeroed the moment decryption completes,
    # success or failure.
    try:
        with SecureKeyBuffer(dek) as dek_buf:
            return AESGCM(bytes(dek_buf)).decrypt(nonce, ct_and_tag, None)
    except DEKDecryptionError:
        raise
    except Exception as exc:
        raise DEKDecryptionError(
            "AES-GCM authentication failed.  The DEK is wrong or the ciphertext "
            "has been corrupted (or the DEK was destroyed — content is "
            "cryptographically unrecoverable)."
        ) from exc


# ---------------------------------------------------------------------------
# High-level helpers — orchestrate DEK + master-key + back-compat for callers
# ---------------------------------------------------------------------------
#
# These wrap the primitives above so the write path and every read path share
# one implementation of "encrypt the raw payload" / "decrypt it transparently,
# tolerating legacy plaintext rows".  Read paths must hydrate raw content
# through :func:`maybe_decrypt_raw_data` so that:
#   * a row with a wrapped DEK → its ciphertext is unwrapped + decrypted, and
#   * a legacy row (``wrapped_dek IS NULL``) → its plaintext is returned as-is.


def encrypt_raw_data(plaintext: str) -> tuple[bytes, bytes, str]:
    """Encrypt a raw-content *plaintext* under a fresh per-memory DEK.

    Reuses the envelope primitives: generates a DEK, AES-256-GCM-encrypts the
    UTF-8 payload under it, and wraps the DEK under the environment master key.

    Returns ``(ciphertext, wrapped_dek, dek_key_id)`` — ``ciphertext`` goes into
    Mongo ``episodes.raw_data``; ``wrapped_dek`` + ``dek_key_id`` go onto the
    ``memories`` row.  ``NCE_MASTER_KEY`` is reached only via
    :func:`nce.signing.require_master_key` (env-only).
    """
    dek = generate_dek()
    # DEK hygiene: encrypt_with_dek and the wrap path each scope their WORKING
    # copy of the DEK through SecureKeyBuffer, so those copies are zeroed as
    # the operations complete.  The immutable `dek` bytes object itself cannot
    # be zeroed in-place in CPython — the previous `with SecureKeyBuffer(dek):
    # pass` here only created and wiped yet another copy — so the best
    # available hygiene is keeping its scope to exactly this function.
    ciphertext = encrypt_with_dek(plaintext.encode("utf-8"), dek)
    with require_master_key() as master_key:
        wrapped = wrap_dek(dek, master_key)
    return ciphertext, wrapped, new_dek_key_id()


def maybe_decrypt_raw_data(raw_data: object, wrapped_dek: bytes | None) -> str:
    """Return the plaintext raw content, decrypting only when encrypted.

    Back-compat contract (legacy rows predate envelope encryption):
      * ``wrapped_dek`` is ``None``/empty  → *raw_data* is plaintext; coerce to
        ``str`` and return it unchanged.
      * ``wrapped_dek`` is set            → *raw_data* is a DEK-encrypted blob;
        unwrap the DEK under the master key and AES-256-GCM-decrypt it.

    As a defensive fallback, if a ``wrapped_dek`` is present but *raw_data* is
    not a recognised ciphertext blob (e.g. a half-migrated row), the value is
    treated as plaintext rather than raising — so reads never hard-fail.

    Raises :class:`~nce.signing.SigningKeyDecryptionError` if the wrapped DEK
    cannot be unwrapped (wrong/destroyed master key) and
    :class:`DEKDecryptionError` if the ciphertext fails authentication.
    """
    if not wrapped_dek:
        if raw_data is None:
            return ""
        return raw_data if isinstance(raw_data, str) else str(raw_data)

    blob = bytes(raw_data) if isinstance(raw_data, (bytes, bytearray, memoryview)) else raw_data
    if not isinstance(blob, bytes) or not blob.startswith(_DEK_PAYLOAD_PREFIX):
        # wrapped_dek set but payload isn't ciphertext — treat as plaintext.
        if raw_data is None:
            return ""
        return raw_data if isinstance(raw_data, str) else str(raw_data)

    wrapped_bytes = bytes(wrapped_dek)
    with require_master_key() as master_key:
        try:
            dek = _unwrap_dek_cached(wrapped_bytes, master_key)
        except SigningKeyDecryptionError:
            from nce.master_key_ring import previous_master_key

            prev_mk = previous_master_key()
            if prev_mk is None:
                raise
            try:
                dek = _unwrap_dek_cached(wrapped_bytes, prev_mk)
            finally:
                prev_mk.zero()
    # decrypt_with_dek zeroes its working copy of the DEK via SecureKeyBuffer
    # (success or failure).  The immutable `dek` bytes cannot be zeroed
    # in-place — a buffer here would only wipe a fresh copy — so its scope
    # ends with this function.
    return decrypt_with_dek(blob, dek).decode("utf-8")


# ---------------------------------------------------------------------------
# Backup key fingerprinting & persistent shred tombstones (v3.7.0)
# ---------------------------------------------------------------------------


def _is_prod_mode() -> bool:
    from nce.config import cfg

    cfg_dict = getattr(cfg, "__dict__", {})
    if "IS_PROD" in cfg_dict:
        return bool(cfg_dict["IS_PROD"])
    env = os.getenv("NCE_ENV", "").strip().lower()
    if env in {"prod", "production"}:
        return True
    return bool(getattr(cfg, "IS_PROD", False))


_DEFAULT_PROD_BACKUP_DIR = "/var/lib/nce/backups"


def _can_use_dir(path: Path) -> bool:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur.exists() and os.access(str(cur), os.W_OK)


def resolve_backup_dir(backup_dir: str | Path | None = None) -> Path:
    """Resolve the persistent backup directory honoring cfg + env overrides."""
    if backup_dir is not None and str(backup_dir).strip():
        return Path(backup_dir).expanduser().resolve()

    from nce.config import cfg

    env_val = (os.getenv("NCE_BACKUP_DIR") or "").strip()
    if env_val:
        return Path(env_val).expanduser().resolve()

    cfg_dict = getattr(cfg, "__dict__", {})
    raw_cfg = str(
        cfg_dict.get("NCE_BACKUP_DIR") or getattr(cfg, "NCE_BACKUP_DIR", "") or ""
    ).strip()
    is_prod = _is_prod_mode()
    if raw_cfg and (is_prod or raw_cfg != _DEFAULT_PROD_BACKUP_DIR or _can_use_dir(Path(raw_cfg))):
        return Path(raw_cfg).expanduser().resolve()

    if is_prod:
        return Path(_DEFAULT_PROD_BACKUP_DIR)
    return Path(tempfile.gettempdir()) / "nce_backups"


def compute_master_key_hmac_fp() -> str:
    """Return HMAC-SHA256(master_key, b'nce-backup-key-fingerprint-v1') hex digest."""
    with require_master_key() as mk:
        return hmac.new(bytes(mk.key_bytes), _BACKUP_KEY_FP_LABEL, hashlib.sha256).hexdigest()


def _tombstone_signing_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "namespace_id": str(record["namespace_id"]),
        "dek_id": str(record.get("dek_id") or ""),
        "mongo_ids": [str(x) for x in (record.get("mongo_ids") or [])],
        "minio_keys": [str(x) for x in (record.get("minio_keys") or [])],
        "shredded_at": str(record["shredded_at"]),
    }


def sign_shred_tombstone(record: dict[str, Any]) -> str:
    """Sign a zero-PII shred tombstone record with ``NCE_MASTER_KEY``."""
    payload = _tombstone_signing_payload(record)
    canonical = canonical_json(payload)
    with require_master_key() as mk:
        return hmac.new(bytes(mk.key_bytes), canonical, hashlib.sha256).hexdigest()


def verify_shred_tombstone(record: dict[str, Any]) -> bool:
    """Verify the HMAC-SHA256 signature of a shred tombstone record."""
    if not isinstance(record, dict):
        return False
    provided = record.get("hmac_sha256")
    if not isinstance(provided, str) or not provided:
        return False
    try:
        expected = sign_shred_tombstone(record)
    except KeyError:
        return False
    return hmac.compare_digest(provided, expected)


def _atomic_append_jsonl(path: Path, record: dict[str, Any]) -> Path:
    """Safely create parent dirs, append a single JSONL line, and os.fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    with _TOMBSTONE_IO_LOCK:
        fd = os.open(str(path), flags, 0o600)
        try:
            view = memoryview(line)
            written = 0
            while written < len(line):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
    return path


def append_shred_tombstone(
    *,
    namespace_id: str | uuid.UUID,
    dek_id: str | None,
    mongo_ids: list[str] | tuple[str, ...] = (),
    minio_keys: list[str] | tuple[str, ...] = (),
    shredded_at: datetime | None = None,
    backup_dir: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build, sign, and fsync-append a zero-PII record to ``shred_tombstones.jsonl``."""
    ts = shredded_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)

    base_record: dict[str, Any] = {
        "namespace_id": str(namespace_id),
        "dek_id": str(dek_id or ""),
        "mongo_ids": [str(m) for m in mongo_ids if m],
        "minio_keys": [str(k) for k in minio_keys if k],
        "shredded_at": ts.isoformat(),
    }

    try:
        sig = sign_shred_tombstone(base_record)
    except MasterKeyMissingError:
        if _is_prod_mode():
            raise
        log.warning("NCE_MASTER_KEY is unavailable in dev/test; writing unsigned tombstone line.")
        sig = ""

    record = {**base_record, "hmac_sha256": sig}
    tombstone_path = resolve_backup_dir(backup_dir) / "shred_tombstones.jsonl"
    _atomic_append_jsonl(tombstone_path, record)
    return record, tombstone_path


def append_deferred_shred_record(
    job: dict[str, Any],
    *,
    backup_dir: str | Path | None = None,
) -> Path:
    """Append a deferred post-lease shred job to ``deferred_shreds.jsonl`` with fsync."""
    target = resolve_backup_dir(backup_dir) / "deferred_shreds.jsonl"
    return _atomic_append_jsonl(target, dict(job))
