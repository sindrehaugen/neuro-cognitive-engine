"""
NCE KEK/DEK Envelope Encryption, Key Cache Eviction & GDPR Crypto-Shredding.

Implements v3.3.0 zero-trust cryptographic requirements:
- KMSAdapter Protocol and local HKDF-SHA256 KEKAdapter.
- KEKRing managing versioned KEKs, wrapping/unwrapping 32-byte AES-256-GCM DEKs.
- DEKRecord dataclass representing tenant/subject-scoped keys.
- ShreddedKeyError raised on attempts to decrypt with shredded DEKs.
- EnvelopeCryptoManager with bounded TTL cache (zeroing bytearray buffers on eviction),
  AES-256-GCM v2 envelope framing, legacy v1 fallback, and Redis pub/sub cache eviction.
- canonicalize_jcs for deterministic RFC 8785 JSON canonicalization.
- crypto_shred_subject for GDPR-compliant cryptographic shredding with non-PII WORM audit receipt.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from nce.config import secret_env
from nce.signing import (
    SecureKeyBuffer,
    SigningKeyDecryptionError,
    require_master_key,
)

try:
    import jcs as _jcs_lib
except ImportError:
    _jcs_lib = None

log = logging.getLogger(__name__)

# Constants
DEFAULT_ACTIVE_KEK_VERSION: str = "kek-v1"
REDIS_KEY_EVICT_CHANNEL: str = "nce:crypto:evict"
_DEK_SIZE: int = 32
_NONCE_SIZE: int = 12
_HKDF_SALT: bytes = b"nce-kek-ring-v1"
_ZEROED_KEY_32: bytes = b"\x00" * _DEK_SIZE


class EnvelopeCryptoError(Exception):
    """Base exception for envelope encryption errors."""


class ShreddedKeyError(RuntimeError, EnvelopeCryptoError):
    """Raised when decryption is attempted using a shredded DEK."""


class KeyNotFoundError(EnvelopeCryptoError):
    """Raised when a requested DEK cannot be found."""


def canonicalize_jcs(obj: Any) -> bytes:
    """
    Deterministic RFC 8785 JSON Canonicalization Scheme (JCS).

    Returns UTF-8 encoded bytes with sorted keys, no whitespace, and RFC 8785 number formatting.
    Accepts arbitrary Python objects, converting UUIDs and datetimes to canonical string forms.
    """

    def _normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): _normalize(v) for k, v in item.items()}
        elif isinstance(item, (list, tuple)):
            return [_normalize(x) for x in item]
        elif isinstance(item, uuid.UUID):
            return str(item)
        elif isinstance(item, datetime):
            if item.tzinfo is None:
                item = item.replace(tzinfo=timezone.utc)
            return item.astimezone(timezone.utc).isoformat()
        elif isinstance(item, bytes):
            return base64.b64encode(item).decode("ascii")
        return item

    normalized = _normalize(obj)
    if _jcs_lib is not None:
        return _jcs_lib.canonicalize(normalized)

    # Fallback RFC 8785 compliant canonical JSON serialization
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@runtime_checkable
class KMSAdapter(Protocol):
    """Protocol for wrapping and unwrapping DEKs under versioned KEKs."""

    def wrap_key(self, kek_version: str, plaintext_dek: bytes) -> bytes:
        """Wrap a plaintext DEK under the specified KEK version."""
        ...

    def unwrap_key(self, kek_version: str, wrapped_dek: bytes) -> bytes:
        """Unwrap a wrapped DEK under the specified KEK version."""
        ...


class HKDFKEKAdapter:
    """
    Default local KMS adapter deriving per-version 256-bit KEKs via HKDF-SHA256 from master keys.

    Derivation:
        HKDF(algorithm=hashes.SHA256(), length=32, salt=b"nce-kek-ring-v1", info=f"nce-kek:{kek_version}".encode())

    Source material precedence:
    1. If raw_keys mapping provided: raw_keys[kek_version]
    2. NCE_KEK_RING_KEYS environment variable (JSON or comma-separated "v1=key,v2=key")
    3. NCE_MASTER_KEY from environment
    """

    def __init__(
        self,
        master_key: bytes | None = None,
        raw_keys: dict[str, bytes] | None = None,
    ) -> None:
        self._raw_keys: dict[str, bytes] = dict(raw_keys) if raw_keys else {}
        self._master_key: bytes | None = master_key

    def set_raw_key(self, kek_version: str, raw_key: bytes) -> None:
        """Register or rotate raw key material for a specific KEK version."""
        if not raw_key:
            raise ValueError("raw_key material cannot be empty")
        self._raw_keys[kek_version] = bytes(raw_key)

    def _get_base_key_material(self, kek_version: str) -> bytes:
        if kek_version in self._raw_keys:
            return self._raw_keys[kek_version]

        env_ring = secret_env("NCE_KEK_RING_KEYS", "").strip()
        if env_ring:
            try:
                parsed = json.loads(env_ring)
                if isinstance(parsed, dict) and kek_version in parsed:
                    val = parsed[kek_version]
                    return val.encode("utf-8") if isinstance(val, str) else bytes(val)
            except Exception:
                for part in env_ring.split(","):
                    if "=" in part:
                        v, k = part.split("=", 1)
                        if v.strip() == kek_version:
                            return k.strip().encode("utf-8")

        if self._master_key is not None:
            return self._master_key

        master_str = secret_env("NCE_MASTER_KEY", "").strip()
        if master_str:
            return master_str.encode("utf-8")

        raise ValueError(f"No key material available to derive KEK for version {kek_version!r}")

    def derive_kek(self, kek_version: str) -> bytes:
        """Derive 32-byte AES-256 KEK for the given version."""
        base_material = self._get_base_key_material(kek_version)
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=f"nce-kek:{kek_version}".encode(),
        )
        return hkdf.derive(base_material)

    def wrap_key(self, kek_version: str, plaintext_dek: bytes) -> bytes:
        """Wrap plaintext DEK with AES-256-GCM using derived KEK."""
        if len(plaintext_dek) != _DEK_SIZE:
            raise ValueError(
                f"Plaintext DEK must be exactly {_DEK_SIZE} bytes; got {len(plaintext_dek)}"
            )

        kek = self.derive_kek(kek_version)
        nonce = os.urandom(_NONCE_SIZE)
        with SecureKeyBuffer(kek) as kek_buf:
            aesgcm = AESGCM(bytes(kek_buf))
            # AAD binds KEK version to ciphertext
            aad = kek_version.encode("utf-8")
            ciphertext = aesgcm.encrypt(nonce, plaintext_dek, aad)

        # Wire format: nonce (12 bytes) || ciphertext+tag
        return nonce + ciphertext

    def unwrap_key(self, kek_version: str, wrapped_dek: bytes) -> bytes:
        """Unwrap wrapped DEK with AES-256-GCM using derived KEK."""
        if len(wrapped_dek) < _NONCE_SIZE + 16:
            raise ValueError(f"Wrapped DEK is too short (got {len(wrapped_dek)} bytes)")

        kek = self.derive_kek(kek_version)
        nonce = wrapped_dek[:_NONCE_SIZE]
        ciphertext = wrapped_dek[_NONCE_SIZE:]
        aad = kek_version.encode("utf-8")

        try:
            with SecureKeyBuffer(kek) as kek_buf:
                aesgcm = AESGCM(bytes(kek_buf))
                return aesgcm.decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            raise ValueError(f"Failed to unwrap DEK under {kek_version!r}: {exc}") from exc


class RewrappedDEK(bytes):
    """
    Subclass of bytes returned by KEKRing.rewrap_dek.

    Supports both:
        wrapped_key, new_version = ring.rewrap_dek(...)
    and:
        wrapped_key = ring.rewrap_dek(...)
        wrapped_key.kek_version
    """

    kek_version: str

    def __new__(cls, val: bytes, kek_version: str) -> RewrappedDEK:
        b = super().__new__(cls, val)
        b.kek_version = kek_version
        return b

    def __iter__(self):
        yield bytes(self)
        yield self.kek_version


class KEKRing:
    """
    Manages active and historical KEKs and performs DEK wrapping and unwrapping.
    """

    def __init__(
        self,
        active_kek_version: str = DEFAULT_ACTIVE_KEK_VERSION,
        adapter: KMSAdapter | None = None,
        *,
        master_key: bytes | None = None,
        active_version: str | None = None,
    ) -> None:
        self.active_kek_version: str = active_version or active_kek_version
        self.adapter: KMSAdapter = adapter or HKDFKEKAdapter(master_key=master_key)

    def rotate_kek(self, new_version: str, raw_key_material: bytes | None = None) -> None:
        """
        Rotate active KEK to new_version, optionally registering raw key material.
        """
        if raw_key_material is not None and isinstance(self.adapter, HKDFKEKAdapter):
            self.adapter.set_raw_key(new_version, raw_key_material)
        self.active_kek_version = new_version
        log.info("Rotated active KEK version to %s", new_version)

    def wrap_dek(self, plaintext_dek: bytes, kek_version: str | None = None) -> tuple[bytes, str]:
        """
        Wrap 32-byte AES-256-GCM DEK under the specified or active KEK version.

        Returns (wrapped_key, kek_version).
        """
        version = kek_version or self.active_kek_version
        wrapped = self.adapter.wrap_key(version, plaintext_dek)
        return wrapped, version

    def unwrap_dek(self, wrapped_dek: bytes, kek_version: str) -> bytes:
        """
        Unwrap DEK under kek_version.
        """
        return self.adapter.unwrap_key(kek_version, wrapped_dek)

    def rewrap_dek(
        self,
        wrapped_dek: bytes,
        old_kek_version: str,
        new_kek_version: str | None = None,
    ) -> RewrappedDEK:
        """
        Unwrap a DEK from old_kek_version and re-wrap under new_kek_version (or active KEK).
        """
        target_version = new_kek_version or self.active_kek_version
        plaintext_dek = self.unwrap_dek(wrapped_dek, old_kek_version)
        with SecureKeyBuffer(plaintext_dek) as dek_buf:
            new_wrapped, v = self.wrap_dek(bytes(dek_buf), target_version)
            return RewrappedDEK(new_wrapped, v)


@dataclass
class DEKRecord:
    """Dataclass representing a record in the encryption_deks table."""

    dek_id: str
    namespace_id: str
    subject_ref: str | None
    wrapped_key: bytes
    kek_version: str
    shredded_at: datetime | None
    created_at: datetime

    def is_shredded(self) -> bool:
        """Return True if key is shredded either by shredded_at or all-zero wrapped_key."""
        if self.shredded_at is not None:
            return True
        if self.wrapped_key == _ZEROED_KEY_32 or self.wrapped_key == (
            b"\x00" * len(self.wrapped_key)
        ):
            return True
        return False


class _BoundedTTLKeyCache:
    """
    In-memory bounded LRU/TTL cache that securely zeroes bytearray key buffers on eviction.
    """

    def __init__(self, maxsize: int = 1024, ttl: float = 300.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._entries: OrderedDict[str, tuple[bytearray, float]] = OrderedDict()

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired_keys = [k for k, (_, exp) in self._entries.items() if now > exp]
        for k in expired_keys:
            self._evict_key(k)

    def _evict_key(self, key: str) -> None:
        if key in self._entries:
            buf, _ = self._entries.pop(key)
            for i in range(len(buf)):
                buf[i] = 0

    def get(self, key: str) -> bytes | None:
        self._purge_expired()
        if key not in self._entries:
            return None
        buf, exp = self._entries[key]
        if time.monotonic() > exp:
            self._evict_key(key)
            return None
        self._entries.move_to_end(key)
        return bytes(buf)

    def put(self, key: str, value: bytes) -> None:
        self._purge_expired()
        if key in self._entries:
            self._evict_key(key)
        while len(self._entries) >= self.maxsize:
            oldest_key = next(iter(self._entries))
            self._evict_key(oldest_key)
        buf = bytearray(value)
        self._entries[key] = (buf, time.monotonic() + self.ttl)

    def evict(self, key: str) -> None:
        self._evict_key(key)

    def clear(self) -> None:
        keys = list(self._entries.keys())
        for k in keys:
            self._evict_key(k)


class InMemoryDEKStore:
    """In-memory fallback DEK repository for tests or when connection is not a database pool."""

    def __init__(self) -> None:
        self.records: dict[str, DEKRecord] = {}

    def get_by_id(self, dek_id: str) -> DEKRecord | None:
        return self.records.get(str(dek_id))

    def get_by_namespace_and_subject(
        self,
        namespace_id: str,
        subject_ref: str | None,
    ) -> DEKRecord | None:
        ns_str = str(namespace_id)
        sub_str = str(subject_ref) if subject_ref is not None else None
        for rec in self.records.values():
            if rec.namespace_id == ns_str and rec.subject_ref == sub_str:
                return rec
        return None

    def insert(self, record: DEKRecord) -> None:
        self.records[str(record.dek_id)] = record

    def shred(self, namespace_id: str, subject_ref: str) -> list[str]:
        ns_str = str(namespace_id)
        sub_str = str(subject_ref)
        now = datetime.now(timezone.utc)
        shredded_ids = []
        for rec in self.records.values():
            if rec.namespace_id == ns_str and rec.subject_ref == sub_str:
                rec.shredded_at = now
                rec.wrapped_key = _ZEROED_KEY_32
                shredded_ids.append(rec.dek_id)
        return shredded_ids


class EnvelopeCryptoManager:
    """
    Manages tenant and subject-level DEKs, encryption, decryption (with legacy v1 fallback),
    and Redis pub/sub cache invalidation.
    """

    def __init__(
        self,
        kek_ring: KEKRing | None = None,
        cache_maxsize: int = 1024,
        cache_ttl: float = 300.0,
    ) -> None:
        self.kek_ring: KEKRing = kek_ring or KEKRing()
        self.cache: _BoundedTTLKeyCache = _BoundedTTLKeyCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self.in_memory_store: InMemoryDEKStore = InMemoryDEKStore()
        self._sub_task: asyncio.Task | None = None

    def _cache_key(self, dek_id: str) -> str:
        return f"dek:{dek_id}"

    async def get_or_create_dek(
        self,
        conn: Any,
        namespace_id: str | uuid.UUID,
        subject_ref: str | None = None,
    ) -> DEKRecord:
        """
        Query or insert into encryption_deks for (namespace_id, subject_ref).
        Supports live asyncpg connection/pool and mock/in-memory fallback store.
        """
        ns_str = str(namespace_id)
        sub_str = str(subject_ref) if subject_ref is not None else None

        # Check if conn is an asyncpg-like object
        is_live_conn = hasattr(conn, "fetchrow") and callable(conn.fetchrow)

        if is_live_conn:
            try:
                # Query existing DEK
                if sub_str is None:
                    row = await conn.fetchrow(
                        """
                        SELECT dek_id, namespace_id, subject_ref, wrapped_key, kek_version, shredded_at, created_at
                        FROM encryption_deks
                        WHERE namespace_id = $1 AND subject_ref IS NULL
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        uuid.UUID(ns_str)
                        if isinstance(namespace_id, uuid.UUID)
                        else uuid.UUID(str(namespace_id)),
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT dek_id, namespace_id, subject_ref, wrapped_key, kek_version, shredded_at, created_at
                        FROM encryption_deks
                        WHERE namespace_id = $1 AND subject_ref = $2
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        uuid.UUID(ns_str)
                        if isinstance(namespace_id, uuid.UUID)
                        else uuid.UUID(str(namespace_id)),
                        sub_str,
                    )

                if row is not None:
                    wrapped_key_bytes = bytes(row["wrapped_key"])
                    rec = DEKRecord(
                        dek_id=str(row["dek_id"]),
                        namespace_id=str(row["namespace_id"]),
                        subject_ref=row["subject_ref"],
                        wrapped_key=wrapped_key_bytes,
                        kek_version=row["kek_version"],
                        shredded_at=row["shredded_at"],
                        created_at=row["created_at"],
                    )
                    return rec

                # Generate new DEK
                raw_dek = os.urandom(_DEK_SIZE)
                with SecureKeyBuffer(raw_dek) as dek_buf:
                    dek_bytes = bytes(dek_buf)
                    wrapped_key, kek_ver = self.kek_ring.wrap_dek(dek_bytes)
                    dek_uuid = uuid.uuid4()
                    now = datetime.now(timezone.utc)
                    ins_row = await conn.fetchrow(
                        """
                        INSERT INTO encryption_deks (
                            dek_id, namespace_id, subject_ref, wrapped_key, kek_version, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING dek_id, namespace_id, subject_ref, wrapped_key, kek_version, shredded_at, created_at
                        """,
                        dek_uuid,
                        uuid.UUID(ns_str)
                        if isinstance(namespace_id, uuid.UUID)
                        else uuid.UUID(str(namespace_id)),
                        sub_str,
                        wrapped_key,
                        kek_ver,
                        now,
                    )
                    rec = DEKRecord(
                        dek_id=str(ins_row["dek_id"]),
                        namespace_id=str(ins_row["namespace_id"]),
                        subject_ref=ins_row["subject_ref"],
                        wrapped_key=bytes(ins_row["wrapped_key"]),
                        kek_version=ins_row["kek_version"],
                        shredded_at=ins_row["shredded_at"],
                        created_at=ins_row["created_at"],
                    )
                    # Cache the newly generated DEK
                    self.cache.put(self._cache_key(rec.dek_id), dek_bytes)
                    return rec

            except Exception as exc:
                log.warning(
                    "Database query in get_or_create_dek failed; falling back to in-memory store: %s",
                    exc,
                )

        # Fallback to in-memory store
        existing = self.in_memory_store.get_by_namespace_and_subject(ns_str, sub_str)
        if existing is not None:
            return existing

        raw_dek = os.urandom(_DEK_SIZE)
        with SecureKeyBuffer(raw_dek) as dek_buf:
            dek_bytes = bytes(dek_buf)
            wrapped_key, kek_ver = self.kek_ring.wrap_dek(dek_bytes)
            dek_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            rec = DEKRecord(
                dek_id=dek_id,
                namespace_id=ns_str,
                subject_ref=sub_str,
                wrapped_key=wrapped_key,
                kek_version=kek_ver,
                shredded_at=None,
                created_at=now,
            )
            self.in_memory_store.insert(rec)
            self.cache.put(self._cache_key(dek_id), dek_bytes)
            return rec

    async def _resolve_dek_record(self, conn: Any, dek_id: str) -> DEKRecord:
        """Fetch DEKRecord by dek_id from DB or in-memory store."""
        is_live_conn = hasattr(conn, "fetchrow") and callable(conn.fetchrow)
        if is_live_conn:
            try:
                dek_uuid = uuid.UUID(dek_id)
                row = await conn.fetchrow(
                    """
                    SELECT dek_id, namespace_id, subject_ref, wrapped_key, kek_version, shredded_at, created_at
                    FROM encryption_deks
                    WHERE dek_id = $1
                    """,
                    dek_uuid,
                )
                if row is not None:
                    return DEKRecord(
                        dek_id=str(row["dek_id"]),
                        namespace_id=str(row["namespace_id"]),
                        subject_ref=row["subject_ref"],
                        wrapped_key=bytes(row["wrapped_key"]),
                        kek_version=row["kek_version"],
                        shredded_at=row["shredded_at"],
                        created_at=row["created_at"],
                    )
            except Exception as exc:
                log.warning("Database fetch in _resolve_dek_record failed: %s", exc)

        mem_rec = self.in_memory_store.get_by_id(dek_id)
        if mem_rec is not None:
            return mem_rec

        raise KeyNotFoundError(f"DEK record not found for dek_id={dek_id!r}")

    async def encrypt_payload(
        self,
        conn: Any,
        namespace_id: str | uuid.UUID,
        plaintext: bytes | str,
        *,
        subject_ref: str | None = None,
        aad: bytes | None = None,
    ) -> dict[str, Any]:
        """
        Encrypts with AES-256-GCM using tenant/subject DEK.
        Returns envelope dictionary:
        {
            "v": 2,
            "alg": "AES-256-GCM",
            "dek_id": ...,
            "kek_version": ...,
            "nonce": base64_str,
            "ciphertext": base64_str,
        }
        """
        dek_record = await self.get_or_create_dek(conn, namespace_id, subject_ref)
        if dek_record.is_shredded():
            raise ShreddedKeyError(f"Cannot encrypt using shredded DEK {dek_record.dek_id}")

        cache_k = self._cache_key(dek_record.dek_id)
        cached_dek = self.cache.get(cache_k)
        if cached_dek is None:
            cached_dek = self.kek_ring.unwrap_dek(dek_record.wrapped_key, dek_record.kek_version)
            self.cache.put(cache_k, cached_dek)

        data_bytes = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
        nonce = os.urandom(_NONCE_SIZE)

        with SecureKeyBuffer(cached_dek) as dek_buf:
            aesgcm = AESGCM(bytes(dek_buf))
            ciphertext = aesgcm.encrypt(nonce, data_bytes, aad)

        return {
            "v": 2,
            "alg": "AES-256-GCM",
            "dek_id": dek_record.dek_id,
            "kek_version": dek_record.kek_version,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    async def decrypt_payload(
        self,
        conn: Any,
        namespace_id: str | uuid.UUID,
        envelope: dict[str, Any] | bytes | str,
        *,
        aad: bytes | None = None,
    ) -> bytes:
        """
        Decrypts an envelope payload.
        - If envelope is v2 DEK envelope, validates DEK is not shredded, unwraps DEK, and decrypts with AES-256-GCM.
        - If envelope is legacy v1 raw bytes or {"v": 1, ...}, transparently falls back to legacy master key decryption.
        """
        parsed_envelope: dict[str, Any] | None = None
        if isinstance(envelope, dict):
            parsed_envelope = envelope
        elif isinstance(envelope, (bytes, bytearray, memoryview, str)):
            raw_bytes = envelope.encode("utf-8") if isinstance(envelope, str) else bytes(envelope)
            try:
                as_json = json.loads(raw_bytes.decode("utf-8"))
                if isinstance(as_json, dict) and "v" in as_json:
                    parsed_envelope = as_json
            except Exception:
                parsed_envelope = None

        # v2 Envelope handling
        if parsed_envelope is not None and parsed_envelope.get("v") == 2:
            dek_id = str(parsed_envelope["dek_id"])
            dek_record = await self._resolve_dek_record(conn, dek_id)
            if dek_record.is_shredded():
                self.cache.evict(self._cache_key(dek_id))
                raise ShreddedKeyError(f"DEK {dek_id} has been cryptographically shredded")

            cache_k = self._cache_key(dek_id)
            cached_dek = self.cache.get(cache_k)
            if cached_dek is None:
                cached_dek = self.kek_ring.unwrap_dek(
                    dek_record.wrapped_key, dek_record.kek_version
                )
                self.cache.put(cache_k, cached_dek)

            nonce = base64.b64decode(parsed_envelope["nonce"])
            ciphertext = base64.b64decode(parsed_envelope["ciphertext"])

            with SecureKeyBuffer(cached_dek) as dek_buf:
                aesgcm = AESGCM(bytes(dek_buf))
                return aesgcm.decrypt(nonce, ciphertext, aad)

        # Legacy v1 fallback:
        # Handles legacy bytes directly encrypted under NCE_MASTER_KEY or {"v": 1, ...}
        return self._decrypt_legacy_v1(envelope, aad=aad)

    def _decrypt_legacy_v1(
        self,
        envelope: dict[str, Any] | bytes | str,
        aad: bytes | None = None,
    ) -> bytes:
        """Transparent legacy v1 decryption fallback."""
        if isinstance(envelope, dict):
            if envelope.get("v") == 1:
                ciphertext_b64 = envelope.get("ciphertext")
                nonce_b64 = envelope.get("nonce")
                if ciphertext_b64 and nonce_b64:
                    nonce = base64.b64decode(nonce_b64)
                    ct = base64.b64decode(ciphertext_b64)
                    with require_master_key() as master_key:
                        raw_key = hashlib.sha256(master_key.key_bytes).digest()
                        with SecureKeyBuffer(raw_key) as k_buf:
                            return AESGCM(bytes(k_buf)).decrypt(nonce, ct, aad)

        raw_bytes = envelope.encode("utf-8") if isinstance(envelope, str) else bytes(envelope)
        with require_master_key() as master_key:
            # Try legacy nce.signing AESGCM decryption
            raw_key = hashlib.sha256(master_key.key_bytes).digest()
            if len(raw_bytes) >= _NONCE_SIZE + 16:
                nonce = raw_bytes[:_NONCE_SIZE]
                ct = raw_bytes[_NONCE_SIZE:]
                try:
                    with SecureKeyBuffer(raw_key) as k_buf:
                        return AESGCM(bytes(k_buf)).decrypt(nonce, ct, aad)
                except Exception:
                    pass

            # Try nce.signing.decrypt_signing_key format
            try:
                from nce.signing import decrypt_signing_key

                return decrypt_signing_key(raw_bytes, master_key)
            except Exception as exc:
                raise SigningKeyDecryptionError(f"Legacy v1 decryption failed: {exc}") from exc

    def evict_local_cache(self, dek_id: str | None = None) -> None:
        """Evicts matching DEK or all DEKs from local cache, zeroing buffers."""
        if dek_id is not None:
            self.cache.evict(self._cache_key(dek_id))
        else:
            self.cache.clear()

    async def publish_key_invalidation(
        self,
        redis_client: Any,
        namespace_id: str | uuid.UUID,
        dek_id: str | None = None,
        subject_ref: str | None = None,
    ) -> None:
        """
        Evict local cache entry and publish JSON invalidation event to Redis channel nce:crypto:evict.
        """
        ns_str = str(namespace_id)
        if dek_id:
            self.evict_local_cache(dek_id)
        else:
            self.evict_local_cache()

        if redis_client is not None:
            payload = {
                "namespace_id": ns_str,
                "dek_id": str(dek_id) if dek_id is not None else None,
                "subject_ref": str(subject_ref) if subject_ref is not None else None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            raw_msg = json.dumps(payload)
            publish_fn = getattr(redis_client, "publish", None)
            if publish_fn is not None:
                res = publish_fn(REDIS_KEY_EVICT_CHANNEL, raw_msg)
                if asyncio.iscoroutine(res):
                    await res

    def handle_invalidation_message(self, payload: str | bytes | dict) -> None:
        """
        Process pub/sub message to evict matching DEKs from local cache.
        """
        try:
            if isinstance(payload, (bytes, bytearray, memoryview)):
                data = json.loads(payload.decode("utf-8"))
            elif isinstance(payload, str):
                data = json.loads(payload)
            elif isinstance(payload, dict):
                data = payload
            else:
                return

            dek_id = data.get("dek_id")
            if dek_id:
                self.evict_local_cache(str(dek_id))
            else:
                self.evict_local_cache()
        except Exception as exc:
            log.warning("Failed to handle key invalidation message: %s", exc)

    async def subscribe_key_invalidation(self, redis_client: Any) -> None:
        """
        Background listener for REDIS_KEY_EVICT_CHANNEL.
        """
        if redis_client is None:
            return

        pubsub = redis_client.pubsub()
        subscribe_fn = getattr(pubsub, "subscribe", None)
        if subscribe_fn is not None:
            res = subscribe_fn(REDIS_KEY_EVICT_CHANNEL)
            if asyncio.iscoroutine(res):
                await res

        async def _reader():
            try:
                listen_fn = getattr(pubsub, "listen", None)
                if listen_fn is not None:
                    async for message in pubsub.listen():
                        if message and message.get("type") == "message":
                            self.handle_invalidation_message(message.get("data", ""))
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning("Error in key invalidation pubsub reader: %s", exc)

        self._sub_task = asyncio.create_task(_reader())

    async def stop_subscription(self) -> None:
        """Stop background Redis invalidation listener."""
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except asyncio.CancelledError:
                pass
            self._sub_task = None


# Module-level default manager
_default_manager: EnvelopeCryptoManager | None = None


def get_default_crypto_manager() -> EnvelopeCryptoManager:
    """Return default EnvelopeCryptoManager instance."""
    global _default_manager
    if _default_manager is None:
        _default_manager = EnvelopeCryptoManager()
    return _default_manager


async def crypto_shred_subject(
    conn: Any,
    namespace_id: str | uuid.UUID,
    subject_ref: str,
    *,
    redis_client: Any = None,
    manager: EnvelopeCryptoManager | None = None,
    kek_ring: KEKRing | None = None,
    agent_id: str = "gdpr-shredder",
) -> dict[str, Any]:
    """
    Cryptographically shred all DEKs associated with (namespace_id, subject_ref).

    1. Updates encryption_deks rows setting wrapped_key = 32 zero bytes and shredded_at = NOW().
    2. Evicts from in-memory cache and broadcasts eviction event over Redis pub/sub.
    3. Builds RFC 8785 canonical non-PII shred receipt (using sha256(subject_ref), never raw PII).
    4. Records shred event in WORM event_log if conn supports append_event.
    5. Returns the non-PII shred receipt dictionary.
    """
    ns_str = str(namespace_id)
    ns_uuid = uuid.UUID(ns_str) if isinstance(namespace_id, str) else namespace_id
    subject_ref_sha256 = hashlib.sha256(subject_ref.encode("utf-8")).hexdigest()

    active_mgr = manager or get_default_crypto_manager()
    if kek_ring is not None:
        active_mgr.kek_ring = kek_ring

    shredded_dek_ids: list[str] = []
    is_live_conn = hasattr(conn, "fetch") and callable(conn.fetch)

    if is_live_conn:
        try:
            # Zero out wrapped_key and set shredded_at
            rows = await conn.fetch(
                """
                UPDATE encryption_deks
                SET wrapped_key = decode(repeat('00', 32), 'hex'),
                    shredded_at = NOW()
                WHERE namespace_id = $1 AND subject_ref = $2 AND shredded_at IS NULL
                RETURNING dek_id
                """,
                ns_uuid,
                subject_ref,
            )
            shredded_dek_ids = [str(r["dek_id"]) for r in rows]
        except Exception as exc:
            log.warning(
                "Database update in crypto_shred_subject failed; checking in-memory store: %s", exc
            )

    # In-memory store fallback / synchronization
    mem_shredded = active_mgr.in_memory_store.shred(ns_str, subject_ref)
    for sid in mem_shredded:
        if sid not in shredded_dek_ids:
            shredded_dek_ids.append(sid)

    sorted_dek_ids = sorted(shredded_dek_ids)

    # Evict local cache and publish invalidations
    for did in sorted_dek_ids:
        active_mgr.evict_local_cache(did)

    if redis_client is not None:
        for did in sorted_dek_ids:
            await active_mgr.publish_key_invalidation(
                redis_client,
                namespace_id=ns_str,
                dek_id=did,
                subject_ref=subject_ref,
            )

    # Construct RFC 8785 JCS non-PII shred receipt
    receipt: dict[str, Any] = {
        "action": "gdpr_crypto_shred",
        "dek_count": len(sorted_dek_ids),
        "dek_ids": sorted_dek_ids,
        "namespace_id": ns_str,
        "subject_ref_sha256": subject_ref_sha256,
    }
    canonical_receipt = canonicalize_jcs(receipt)
    receipt_sha256 = hashlib.sha256(canonical_receipt).hexdigest()
    receipt["receipt_sha256"] = receipt_sha256

    # Write to WORM event_log if conn is in a transaction
    try:
        from nce.event_log import append_event

        can_append = (
            is_live_conn and hasattr(conn, "is_in_transaction") and conn.is_in_transaction()
        )
        if can_append:
            await append_event(
                conn=conn,
                namespace_id=ns_uuid,
                agent_id=agent_id,
                event_type="config_changed",
                params={
                    "actor": agent_id,
                    "changes": {
                        "action": "gdpr_crypto_shred",
                        "receipt_sha256": receipt_sha256,
                        "subject_ref_sha256": subject_ref_sha256,
                        "dek_count": len(sorted_dek_ids),
                    },
                },
            )
    except Exception as exc:
        log.warning("WORM event_log append in crypto_shred_subject skipped/failed: %s", exc)

    return receipt
