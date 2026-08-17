"""
NCE cryptographic signing infrastructure (Phase 0.2).

Every stored memory and every event_log row is signed with HMAC-SHA256 over
the JCS-canonicalised (RFC 8785) key fields of that record.  The signature
allows any retrieval to verify integrity and detect post-write tampering.

Key storage
-----------
Signing keys live in the ``signing_keys`` Postgres table as AES-256-GCM
encrypted blobs.  The AES wrapping key is derived from ``NCE_MASTER_KEY``
via **PBKDF2-HMAC-SHA256** (100,000–600,000 iterations, versioned) with a per-blob
random salt, or **Argon2id** when available.

**Wire format (v4 — PBKDF2 @ 600K, OWASP 2026)**: ``b'TC4\\x01' || salt (16) || nonce (12) || ciphertext+tag``.

**Wire format (v3 — Argon2id, preferred)**: ``b'TC3\\x01' || salt (16) || nonce (12) || ciphertext+tag``.

**Wire format (v2 — PBKDF2 @ 100K, NIST minimum)**: ``b'TC2\\x01' || salt (16) || nonce (12) || ciphertext+tag``.

**Legacy format** (still decrypted for migration): ``nonce (12) || ciphertext+tag``
using a single SHA-256 digest of the master key as the AES key.  New writes
always use v3 (Argon2id) or v4 (PBKDF2 @ 600K).

Server bootstrap
----------------
Call ``require_master_key()`` at startup (before the asyncpg pool is used).
It raises ``MasterKeyMissingError`` if ``NCE_MASTER_KEY`` is absent or
empty — the server must not start without it.

Active-key caching
------------------
``get_active_key()`` maintains a module-level ``cachetools.TTLCache``
(5-minute TTL, max 1000 entries) so the ``signing_keys`` table is not hit
on every write.  The TTLCache automatically evicts expired entries on access.
An ``__delitem__`` override ensures that the ``MutableKeyBuffer`` backing
each evicted entry is zeroed (nulled) before removal, reducing the window
during which decrypted PBKDF2 / Argon2id key material is readable in the
process heap.

The active key is stored under two cache keys — ``"__active__"`` (for
``get_active_key`` lookups) and its real ``key_id`` (for ``get_key_by_id``
cross-references) — each with an independent ``MutableKeyBuffer`` so that
evicting one slot does not prematurely zero the other.  ``rotate_key()``
explicitly zeros all cached buffers and clears the cache.

Thread/async safety
-------------------
All public ``async`` functions accept an ``asyncpg.Connection`` and must be
called inside an active transaction when the caller requires atomicity.
The in-memory key cache is not locked; for the single-threaded asyncio event
loop that is the normal deployment model this is safe.  For multi-threaded
deployments (e.g. thread-pool workers) the caller must hold its own lock
around ``get_active_key`` / ``rotate_key`` pairs.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import timezone
from typing import Any

import asyncpg

from nce.config import cfg, secret_env

try:
    import jcs as _jcs_lib  # RFC 8785 — preferred

    def canonical_json(data: dict[str, Any]) -> bytes:  # noqa: D401
        """Return the RFC 8785-canonical JSON encoding of *data*."""
        return _jcs_lib.canonicalize(data)

except ImportError:  # pragma: no cover – jcs is in requirements.txt; handle gracefully
    import json as _json

    def canonical_json(data: dict[str, Any]) -> bytes:  # type: ignore[misc]
        """
        Fallback RFC 8785-compatible canonical JSON.

        Covers the NCE signing payload (UUIDs, ISO-8601 strings, integers).
        Full ECMAScript number serialisation divergence is not triggered
        because signing inputs never contain bare floats.
        """
        return _json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )


from cachetools import TTLCache
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MASTER_KEY_ENV: str = "NCE_MASTER_KEY"
_NONCE_SIZE: int = 12  # 96-bit nonce for AES-256-GCM
_KEY_CACHE_TTL_S: float = 300.0  # 5 minutes
_MASTER_KEY_LEN: int = 32  # minimum master key length in bytes

# PBKDF2-HMAC-SHA256 for deriving AES-256 keys from the master secret.
# Floor: NIST minimum 100,000 for v2 backward compat.
# OWASP 2026: 600,000 for v4 blobs (new writes).  v2 decryption (100K) retained.
# Operator may raise via NCE_PBKDF2_ITERATIONS (affects v2 only).
_PBKDF2_ITERATIONS: int = cfg.NCE_PBKDF2_ITERATIONS
# OWASP 2026 recommended minimum: 600,000 iterations for PBKDF2-HMAC-SHA256.
_PBKDF2_ITERATIONS_V4: int = cfg.NCE_PBKDF2_ITERATIONS_V4
_PBKDF2_SALT_LEN: int = 16
# Magic prefix for v2 blobs (PBKDF2 @ 100K + random salt). Legacy blobs have no prefix.
_ENCRYPTED_KEY_BLOB_V2: bytes = b"TC2\x01"
# Magic prefix for v3 blobs (Argon2id).  v2 and legacy blobs still decrypt.
_ENCRYPTED_KEY_BLOB_V3: bytes = b"TC3\x01"
# Magic prefix for v4 blobs (PBKDF2 @ 600K, OWASP 2026).  v2/v3/legacy still decrypt.
_ENCRYPTED_KEY_BLOB_V4: bytes = b"TC4\x01"
# Fixed salt for ``MasterKey.derive_aes_key()`` only (self-tests / diagnostics; not stored).
_DERIVE_AES_SELFTEST_SALT: bytes = hashlib.sha256(
    b"NCE MasterKey.derive_aes_key selftest v2"
).digest()[:_PBKDF2_SALT_LEN]

# ---------------------------------------------------------------------------
# ML-DSA-44 (FIPS 204 / CRYSTALS-Dilithium) — post-quantum asymmetric signing
# ---------------------------------------------------------------------------
# Algorithm identifier used by new event_log rows.  Existing HMAC-SHA256 rows
# remain verifiable; ML-DSA is the new write path for Lattice Signing.
# "ML-DSA-512" is the colloquial roadmap name; canonical FIPS 204 name is "ML-DSA-44".
_KEY_ALGORITHM: str = "ML-DSA-44"  # FIPS 204 parameter set (128-bit classical security)

try:
    from cryptography.hazmat.primitives.asymmetric.mldsa import (
        MLDSA44PrivateKey as _MLDSA44PrivateKey,
    )
    from cryptography.hazmat.primitives.asymmetric.mldsa import (
        MLDSA44PublicKey as _MLDSA44PublicKey,
    )

    _HAS_MLDSA = True
except ImportError:
    _HAS_MLDSA = False


def generate_mldsa_keypair() -> tuple[bytes, bytes]:
    """Generate an ML-DSA-44 (FIPS 204) key pair.

    Returns ``(seed_bytes[32], public_key_bytes[1312])`` in raw bytes form.
    The seed (private key material) should be stored encrypted at rest via
    ``encrypt_signing_key``.

    Raises ``RuntimeError`` if the installed ``cryptography`` library is older
    than version 44 and does not support ML-DSA.
    """
    if not _HAS_MLDSA:
        raise RuntimeError(
            "ML-DSA-44 requires cryptography>=44.0.0. Run: pip install --upgrade cryptography"
        )
    private_key = _MLDSA44PrivateKey.generate()
    seed_bytes = private_key.private_bytes_raw()
    public_key_bytes = private_key.public_key().public_bytes_raw()
    return seed_bytes, public_key_bytes


def mldsa_sign(message: bytes, seed_bytes: bytes) -> bytes:
    """Sign *message* with an ML-DSA-44 private key (seed bytes).

    The seed_bytes parameter is the 32-byte value from private_bytes_raw().
    Returns the raw deterministic signature bytes.
    """
    if not _HAS_MLDSA:
        raise RuntimeError("ML-DSA-44 requires cryptography>=44.0.0")
    private_key = _MLDSA44PrivateKey.from_seed_bytes(seed_bytes)
    return private_key.sign(message)


def mldsa_verify(message: bytes, signature: bytes, public_key_bytes: bytes) -> bool:
    """Verify an ML-DSA-44 signature.

    Returns ``True`` on success, ``False`` on verification failure.
    """
    if not _HAS_MLDSA:
        raise RuntimeError("ML-DSA-44 requires cryptography>=44.0.0")
    try:
        public_key = _MLDSA44PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, message)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Attempt to import argon2-cffi for Argon2id wrapping-key derivation.
# If unavailable, encrypt_signing_key falls back to PBKDF2-HMAC-SHA256.
try:
    from argon2.low_level import Type, hash_secret_raw

    _HAS_ARGON2 = True
except ImportError:  # pragma: no cover — install argon2-cffi (see requirements.txt)
    _HAS_ARGON2 = False

# Argon2id parameters (OWASP 2025 recommended minimums).
_ARGON2_TIME_COST: int = 3
_ARGON2_MEMORY_COST: int = 65536  # 64 MiB
_ARGON2_PARALLELISM: int = 4
_ARGON2_HASH_LEN: int = 32  # AES-256 key length


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SigningError(Exception):
    """Base class for all signing-related errors."""


class MasterKeyMissingError(SigningError):
    """Raised at startup when ``NCE_MASTER_KEY`` is absent or empty."""


class NoActiveSigningKeyError(SigningError):
    """Raised when no row with ``status='active'`` exists in ``signing_keys``."""


class SigningKeyDecryptionError(SigningError):
    """Raised when AES-GCM decryption fails (wrong master key or corrupted blob)."""


# ---------------------------------------------------------------------------
# MasterKey — mutable buffer with secure zeroing
# ---------------------------------------------------------------------------


class MasterKey:
    """
    Mutable-buffer wrapper for the master signing key.

    Holds key material in a ``bytearray`` that can be securely zeroed after
    use.  This prevents the key from persisting in process memory as an
    immutable Python string that cannot be overwritten.

    **Usage**::

        with MasterKey.from_env() as mk:
            raw_key = decrypt_signing_key(blob, mk)

    When the context manager exits the buffer is overwritten with null bytes.
    ``__del__`` provides a second line of defence for GC-collected instances.

    After ``zero()`` has been called any attempt to access ``key_bytes`` or
    ``derive_aes_key()`` raises ``ValueError``.
    """

    __slots__ = ("_buf", "_zeroed")

    def __init__(self, key_bytes: bytes) -> None:
        if len(key_bytes) < _MASTER_KEY_LEN:
            raise MasterKeyMissingError(
                f"Master key must be at least {_MASTER_KEY_LEN} bytes; got {len(key_bytes)}."
            )
        self._buf = bytearray(key_bytes)
        self._zeroed = False

    # -- factory ----------------------------------------------------------

    @classmethod
    def from_env(cls) -> MasterKey:
        """
        Load the master key from ``NCE_MASTER_KEY`` (or ``NCE_MASTER_KEY_FILE``).

        Resolution goes through :func:`nce.config.secret_env`, so setting
        ``NCE_MASTER_KEY_FILE=/run/secrets/nce_master_key`` (Docker/K8s secret)
        loads the key from that file instead of the environment — keeping it out
        of ``/proc/<pid>/environ``.  ``*_FILE`` takes precedence over the plain
        env var; a missing/unreadable ``*_FILE`` raises a clear, non-secret
        error (see ``secret_env``).

        Raises ``MasterKeyMissingError`` if the key is absent or empty.

        Uses a ``ctypes`` C-allocated buffer for the UTF-8 encoding so the
        intermediate buffer can be explicitly zeroed after the key material
        is copied into the ``bytearray``-backed ``MasterKey``.  The Python
        ``str`` returned by the resolver is unavoidable, but the C encoding
        buffer is memset to zero before this function returns.
        """
        mk_str = secret_env(_MASTER_KEY_ENV, "").strip()
        if not mk_str:
            raise MasterKeyMissingError(
                f"Master key {_MASTER_KEY_ENV!r} is missing or empty.  "
                "The NCE server cannot start without a signing master key.  "
                f"Set {_MASTER_KEY_ENV} (or {_MASTER_KEY_ENV}_FILE pointing at a "
                "secret file) to a secret string of ≥32 random characters."
            )

        # Encode to UTF-8 bytes (one unavoidable intermediate bytes object).
        encoded = mk_str.encode("utf-8")
        buf_len = len(encoded)

        if buf_len < _MASTER_KEY_LEN:
            raise MasterKeyMissingError(
                f"Master key must be at least {_MASTER_KEY_LEN} bytes; got {buf_len}."
            )

        # Allocate a C-level char buffer and copy the encoded bytes into it.
        c_buf = ctypes.create_string_buffer(encoded, buf_len + 1)

        # Build the MasterKey directly from the C buffer via memoryview,
        # bypassing the creation of a second intermediate Python bytes object.
        instance = cls.__new__(cls)
        instance._buf = bytearray(memoryview(c_buf).cast("B")[:buf_len])
        instance._zeroed = False

        # Explicitly zero the C buffer now that we have our own bytearray copy.
        ctypes.memset(c_buf, 0, buf_len + 1)

        return instance

    # -- read-only accessors -----------------------------------------------

    @property
    def key_bytes(self) -> memoryview:
        """
        Return a read-only view of the raw key bytes.

        The returned ``memoryview`` references the internal buffer; it is
        NOT a copy and will be invalidated by ``zero()``.
        """
        if self._zeroed:
            raise ValueError("MasterKey has been zeroed and is no longer usable.")
        return memoryview(self._buf)

    def derive_aes_key(self) -> bytes:
        """
        Derive a 32-byte AES key via PBKDF2-HMAC-SHA256 using a fixed self-test salt.

        Used by unit tests and diagnostics only.  Stored signing-key blobs use
        ``encrypt_signing_key`` / ``decrypt_signing_key``, which pick a random
        salt per ciphertext.
        """
        if self._zeroed:
            raise ValueError("MasterKey has been zeroed.")
        return _pbkdf2_derive_aes_key(self, _DERIVE_AES_SELFTEST_SALT)

    # -- secure zeroing ----------------------------------------------------

    def zero(self) -> None:
        """
        Overwrite the internal buffer with null bytes.

        Idempotent — safe to call multiple times.
        """
        if not self._zeroed:
            for i in range(len(self._buf)):
                self._buf[i] = 0
            self._zeroed = True

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> MasterKey:
        return self

    def __exit__(self, *args: object) -> None:
        self.zero()

    # -- destructor --------------------------------------------------------

    def __del__(self) -> None:
        """Best-effort zeroing at GC time (not guaranteed on process exit)."""
        try:
            self.zero()
        except Exception:
            pass  # __del__ must not raise


# ---------------------------------------------------------------------------
# MutableKeyBuffer — zeroable buffer for cached signing keys
# ---------------------------------------------------------------------------


class MutableKeyBuffer:
    """
    Mutable buffer for signing key material that can be securely zeroed.

    Unlike raw ``bytes`` (immutable, unzeroable), this wraps a ``bytearray``
    and provides explicit ``zero()`` to overwrite the buffer with null bytes.
    Used by ``_CachedKey`` so that signing keys are wiped from process memory
    when the cache TTL expires or the key is rotated.

    The ``raw`` property returns a read-only ``memoryview`` suitable for
    passing to ``hmac.new()`` and other bytes-like APIs.
    """

    __slots__ = ("_buf", "_zeroed")

    def __init__(self, key_bytes: bytes) -> None:
        self._buf = bytearray(key_bytes)
        self._zeroed = False

    @property
    def raw(self) -> memoryview:
        """Read-only view of the key material.  Raises ``ValueError`` if zeroed."""
        if self._zeroed:
            raise ValueError("MutableKeyBuffer has been zeroed and is no longer usable.")
        return memoryview(self._buf)

    def zero(self) -> None:
        """
        Overwrite the internal buffer with null bytes.

        Idempotent — safe to call multiple times.  After this call ``raw``
        and ``bytes()`` will raise ``ValueError``.
        """
        if not self._zeroed:
            for i in range(len(self._buf)):
                self._buf[i] = 0
            self._zeroed = True

    def __bytes__(self) -> bytes:
        """Return a copy of the raw key bytes.  Raises ``ValueError`` if zeroed."""
        if self._zeroed:
            raise ValueError("MutableKeyBuffer has been zeroed and is no longer usable.")
        return bytes(self._buf)

    def __del__(self) -> None:
        """Best-effort zeroing at GC time."""
        try:
            self.zero()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SecureKeyBuffer — ephemeral zeroable buffer for transient crypto operations
# ---------------------------------------------------------------------------


class SecureKeyBuffer:
    """
    Short-lived, zero-on-exit buffer for **ephemeral** key material.

    Use this as a context manager any time a derived or decrypted key byte
    string must exist for only the duration of a single cryptographic
    operation (AES-GCM encryption/decryption, HMAC computation, KDF output).
    On ``__exit__`` — and as a fallback on ``__del__`` — the internal
    ``bytearray`` is overwritten with null bytes, preventing the material
    from lingering in process memory until the GC reclaims the object.

    Unlike ``MutableKeyBuffer`` (which is designed for *cached* keys that
    persist across multiple operations), ``SecureKeyBuffer`` is explicitly
    scoped to a single ``with`` block::

        with SecureKeyBuffer(derived_aes_key_bytes) as skb:
            ciphertext = AESGCM(bytes(skb)).encrypt(nonce, plaintext, None)
        # derived_aes_key_bytes is now zeroed in the underlying bytearray

    **Note on Python immutability:** The *input* ``bytes`` object passed to
    ``__init__`` is immutable and cannot be zeroed — Python may intern or
    share it.  ``SecureKeyBuffer`` copies the material into a ``bytearray``
    at construction time so that the mutable copy can be zeroed.  Callers
    that obtain key bytes from KDF functions (which return fresh ``bytes``
    objects) benefit most: the short-lived copy is zeroed as soon as the
    context manager exits, reducing the window during which raw key material
    is readable in the process heap.
    """

    __slots__ = ("_buf", "_zeroed")

    def __init__(self, key_bytes: bytes) -> None:
        self._buf = bytearray(key_bytes)
        self._zeroed = False

    # -- context manager interface -----------------------------------------

    def __enter__(self) -> SecureKeyBuffer:
        return self

    def __exit__(self, *args: object) -> None:
        self.zero()

    # -- bytes-like interface ----------------------------------------------

    def __bytes__(self) -> bytes:
        """Return a copy of the key bytes.  Raises ``ValueError`` if zeroed."""
        if self._zeroed:
            raise ValueError("SecureKeyBuffer has been zeroed and is no longer usable.")
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def raw(self) -> memoryview:
        """Read-only ``memoryview`` of the key material.  Raises ``ValueError`` if zeroed."""
        if self._zeroed:
            raise ValueError("SecureKeyBuffer has been zeroed and is no longer usable.")
        return memoryview(self._buf)

    # -- secure zeroing ----------------------------------------------------

    def zero(self) -> None:
        """
        Overwrite the internal buffer with null bytes.

        Idempotent — safe to call multiple times.  After ``zero()`` any
        access via ``raw`` or ``__bytes__`` raises ``ValueError``.
        """
        if not self._zeroed:
            for i in range(len(self._buf)):
                self._buf[i] = 0
            self._zeroed = True

    # -- destructor --------------------------------------------------------

    def __del__(self) -> None:
        """Best-effort zeroing at GC time — guards against forgotten ``with`` blocks."""
        try:
            self.zero()
        except Exception:
            pass  # __del__ must not raise


# ---------------------------------------------------------------------------
# In-memory key cache (TTL-aware with automatic eviction + secure zeroing)
# ---------------------------------------------------------------------------

# Sentinel key under which the currently-active signing key is stored.
_ACTIVE_KEY_CACHE_KEY: str = "__active__"


@dataclass
class _CachedKey:
    key_id: str
    raw_key: MutableKeyBuffer
    expires_at: float  # time.monotonic() deadline (retained for diagnostics)


class _SigningKeyCache(TTLCache):
    """
    TTLCache subclass that securely zeros ``MutableKeyBuffer`` entries on eviction.

    Every entry stored in this cache wraps key material in a ``MutableKeyBuffer``.
    When an entry is evicted — whether by exceeding *maxsize* (which goes through
    ``popitem`` → ``__delitem__``) or by explicit ``del`` / ``clear()`` — the
    buffer is overwritten with null bytes before the entry is removed.

    **TTL expiry** is handled by ``cachetools``'s internal ``_Timer`` thread
    which removes expired entries directly without invoking ``__delitem__`` at
    the dict level.  In that path the ``MutableKeyBuffer.__del__`` destructor
    provides GC-time zeroing.  While this is not instantaneous (GC is
    non-deterministic in CPython), the key material is removed from the active
    cache immediately and will be zeroed when the ``_CachedKey`` dataclass is
    collected — typically within one GC generation on Python 3.12+.

    Every ``store_memory`` / ``verify_event_signature`` calls
    ``get_active_key()``, guaranteeing frequent cache access and rapid
    re-population after TTL expiry.  An explicit background eviction thread
    beyond ``cachetools``'s own timer is not required at current throughput —
    see the Phase 3 Kaizen note for evaluation criteria.
    """

    def __init__(self, maxsize: int, ttl: float) -> None:
        super().__init__(maxsize=maxsize, ttl=ttl)

    def __delitem__(self, key: object) -> None:  # type: ignore[override]
        """Zero the key buffer before removing the entry from the cache."""
        entry: _CachedKey | None = self.get(key)  # type: ignore[arg-type]
        if entry is not None:
            try:
                entry.raw_key.zero()
                log.debug("Signing key buffer zeroed on eviction (key_id=%s).", entry.key_id)
            except Exception:
                pass  # Best-effort zeroing — must not prevent eviction
        super().__delitem__(key)


_key_cache: _SigningKeyCache = _SigningKeyCache(maxsize=1000, ttl=_KEY_CACHE_TTL_S)

# Lazy-initialized asyncio.Lock to prevent thundering-herd cache refreshes
# (Item 31 — elevated priority after PBKDF2 @ 600K upgrade).
_key_cache_lock: asyncio.Lock | None = None


def _get_key_cache_lock() -> asyncio.Lock:
    global _key_cache_lock
    if _key_cache_lock is None:
        _key_cache_lock = asyncio.Lock()
    return _key_cache_lock


# ---------------------------------------------------------------------------
# AES-256-GCM helpers
# ---------------------------------------------------------------------------


def _pbkdf2_derive_aes_key(master_key: MasterKey, salt: bytes) -> bytes:
    """Derive a 32-byte AES-256 key from the master secret using PBKDF2-HMAC-SHA256
    at the v2 iteration count (100K floor, NIST minimum)."""
    if len(salt) < _PBKDF2_SALT_LEN:
        raise SigningError(
            f"PBKDF2 salt must be at least {_PBKDF2_SALT_LEN} bytes; got {len(salt)}."
        )
    if master_key._zeroed:
        raise ValueError("MasterKey has been zeroed and is no longer usable.")
    return hashlib.pbkdf2_hmac(
        "sha256",
        bytes(master_key.key_bytes),
        salt[:_PBKDF2_SALT_LEN],
        _PBKDF2_ITERATIONS,
        dklen=32,
    )


def _pbkdf2_derive_aes_key_v4(master_key: MasterKey, salt: bytes) -> bytes:
    """Derive a 32-byte AES-256 key using PBKDF2-HMAC-SHA256 at 600,000 iterations.

    OWASP 2026 compliance.  Used for v4 (``TC4\\x01``) blob encryption/decryption.
    v2 blobs (100K) are still decrypted by ``_pbkdf2_derive_aes_key``.
    """
    if len(salt) < _PBKDF2_SALT_LEN:
        raise SigningError(
            f"PBKDF2 salt must be at least {_PBKDF2_SALT_LEN} bytes; got {len(salt)}."
        )
    if master_key._zeroed:
        raise ValueError("MasterKey has been zeroed and is no longer usable.")
    return hashlib.pbkdf2_hmac(
        "sha256",
        bytes(master_key.key_bytes),
        salt[:_PBKDF2_SALT_LEN],
        _PBKDF2_ITERATIONS_V4,
        dklen=32,
    )


def _legacy_sha256_derive_aes_key(master_key: MasterKey) -> bytes:
    """Pre-v2 behaviour: single SHA-256 digest of the master secret as the AES key."""
    if master_key._zeroed:
        raise ValueError("MasterKey has been zeroed and is no longer usable.")
    return hashlib.sha256(master_key.key_bytes).digest()


def _argon2id_derive_aes_key(master_key: MasterKey, salt: bytes) -> bytes:
    """
    Derive a 32-byte AES-256 key via Argon2id (memory-hard KDF).

    Uses OWASP-recommended parameters (time_cost=3, memory_cost=64 MiB,
    parallelism=4).  Falls back to PBKDF2 @ 600K (v4, OWASP 2026) if
    ``argon2-cffi`` is not installed.
    """
    if not _HAS_ARGON2:
        return _pbkdf2_derive_aes_key_v4(master_key, salt)
    if master_key._zeroed:
        raise ValueError("MasterKey has been zeroed and is no longer usable.")
    return hash_secret_raw(
        secret=bytes(master_key.key_bytes),
        salt=salt[:_PBKDF2_SALT_LEN],
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_ARGON2_HASH_LEN,
        type=Type.ID,
    )


def encrypt_signing_key(raw_key: bytes, master_key: MasterKey) -> bytes:
    """
    AES-256-GCM encrypt *raw_key* with the master key.

    Wire format (v3, Argon2id — preferred)::
        ``TC3\\x01 || salt (16) || nonce (12) || ciphertext+tag``

    Wire format (v4, PBKDF2 @ 600K — OWASP 2026 fallback when argon2-cffi unavailable)::
        ``TC4\\x01 || salt (16) || nonce (12) || ciphertext+tag``

    Legacy formats still decryptable but no longer produced:
        v2: ``TC2\\x01 || salt (16) || nonce (12) || ciphertext+tag``  (PBKDF2 @ 100K)
        v1: ``nonce (12) || ciphertext+tag``                           (SHA-256, no prefix)

    This function is exposed for the key-rotation CLI; callers should not
    store the returned bytes anywhere other than ``signing_keys.encrypted_key``.
    """
    salt = os.urandom(_PBKDF2_SALT_LEN)
    if _HAS_ARGON2:
        derived = _argon2id_derive_aes_key(master_key, salt)
        prefix = _ENCRYPTED_KEY_BLOB_V3
    else:
        derived = _pbkdf2_derive_aes_key_v4(master_key, salt)
        prefix = _ENCRYPTED_KEY_BLOB_V4
    nonce = os.urandom(_NONCE_SIZE)
    # Wrap the ephemeral AES key in SecureKeyBuffer so it is zeroed when the
    # encryption completes — the derived key must not outlive this scope.
    with SecureKeyBuffer(derived) as aes_buf:
        ciphertext_and_tag = AESGCM(bytes(aes_buf)).encrypt(nonce, raw_key, None)
    return prefix + salt + nonce + ciphertext_and_tag


def decrypt_signing_key(encrypted_key: bytes, master_key: MasterKey) -> bytes:
    """
    Decrypt a blob produced by ``encrypt_signing_key``.

    Accepts **v4** blobs (PBKDF2 @ 600K, OWASP 2026), **v3** blobs (Argon2id),
    **v2** blobs (PBKDF2 @ 100K), and **legacy** blobs (SHA-256, no prefix).
    Format is auto-detected from the prefix.

    Raises ``SigningKeyDecryptionError`` on authentication failure (wrong
    master key, truncated blob, or data corruption).
    """
    if master_key._zeroed:
        raise ValueError("MasterKey has been zeroed and is no longer usable.")
    if encrypted_key.startswith(_ENCRYPTED_KEY_BLOB_V4):
        tail = encrypted_key[len(_ENCRYPTED_KEY_BLOB_V4) :]
        need = _PBKDF2_SALT_LEN + _NONCE_SIZE + 16
        if len(tail) < need:
            raise SigningKeyDecryptionError(
                "encrypted_key v4 blob is too short to be valid "
                f"(got {len(encrypted_key)} bytes, need ≥{len(_ENCRYPTED_KEY_BLOB_V4) + need})."
            )
        salt = tail[:_PBKDF2_SALT_LEN]
        nonce = tail[_PBKDF2_SALT_LEN : _PBKDF2_SALT_LEN + _NONCE_SIZE]
        ct_and_tag = tail[_PBKDF2_SALT_LEN + _NONCE_SIZE :]
        derived = _pbkdf2_derive_aes_key_v4(master_key, salt)
    elif encrypted_key.startswith(_ENCRYPTED_KEY_BLOB_V3):
        tail = encrypted_key[len(_ENCRYPTED_KEY_BLOB_V3) :]
        need = _PBKDF2_SALT_LEN + _NONCE_SIZE + 16
        if len(tail) < need:
            raise SigningKeyDecryptionError(
                "encrypted_key v3 blob is too short to be valid "
                f"(got {len(encrypted_key)} bytes, need ≥{len(_ENCRYPTED_KEY_BLOB_V3) + need})."
            )
        salt = tail[:_PBKDF2_SALT_LEN]
        nonce = tail[_PBKDF2_SALT_LEN : _PBKDF2_SALT_LEN + _NONCE_SIZE]
        ct_and_tag = tail[_PBKDF2_SALT_LEN + _NONCE_SIZE :]
        derived = _argon2id_derive_aes_key(master_key, salt)
    elif encrypted_key.startswith(_ENCRYPTED_KEY_BLOB_V2):
        tail = encrypted_key[len(_ENCRYPTED_KEY_BLOB_V2) :]
        need = _PBKDF2_SALT_LEN + _NONCE_SIZE + 16
        if len(tail) < need:
            raise SigningKeyDecryptionError(
                "encrypted_key v2 blob is too short to be valid "
                f"(got {len(encrypted_key)} bytes, need ≥{len(_ENCRYPTED_KEY_BLOB_V2) + need})."
            )
        salt = tail[:_PBKDF2_SALT_LEN]
        nonce = tail[_PBKDF2_SALT_LEN : _PBKDF2_SALT_LEN + _NONCE_SIZE]
        ct_and_tag = tail[_PBKDF2_SALT_LEN + _NONCE_SIZE :]
        derived = _pbkdf2_derive_aes_key(master_key, salt)
    else:
        if len(encrypted_key) < _NONCE_SIZE + 16:
            raise SigningKeyDecryptionError(
                "encrypted_key blob is too short to be valid "
                f"(got {len(encrypted_key)} bytes, expected ≥{_NONCE_SIZE + 16})."
            )
        derived = _legacy_sha256_derive_aes_key(master_key)
        nonce = encrypted_key[:_NONCE_SIZE]
        ct_and_tag = encrypted_key[_NONCE_SIZE:]
    # Wrap the ephemeral AES key in SecureKeyBuffer.  The buffer is zeroed
    # the moment decryption completes — success or failure.
    try:
        with SecureKeyBuffer(derived) as aes_buf:
            return AESGCM(bytes(aes_buf)).decrypt(nonce, ct_and_tag, None)
    except SigningKeyDecryptionError:
        raise
    except Exception as exc:
        raise SigningKeyDecryptionError(
            "AES-GCM authentication failed.  This means either the master key "
            "is wrong or the signing key blob has been corrupted."
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def require_master_key() -> MasterKey:
    """
    Return a ``MasterKey`` wrapping ``NCE_MASTER_KEY``.

    Call once at server startup before any signing operation is attempted.
    The returned ``MasterKey`` should be used as a context manager to ensure
    the underlying buffer is zeroed after use::

        with require_master_key() as mk:
            raw_key = decrypt_signing_key(blob, mk)

    Raises ``MasterKeyMissingError`` if the env var is absent or empty.
    """
    return MasterKey.from_env()


async def get_active_key(
    conn: asyncpg.Connection,
) -> tuple[str, bytes]:
    """
    Return ``(key_id, raw_signing_key)`` for the current active key.

    Checks the module-level TTLCache first.  On a cache miss the active key
    is fetched from ``signing_keys``, decrypted, and cached under two keys:
    ``"__active__"`` (for ``get_active_key`` lookups) and the real ``key_id``
    (for ``get_key_by_id`` cross-references).  Each entry holds an independent
    ``MutableKeyBuffer`` copy so evicting one does not zero the other.

    Parameters
    ----------
    conn:
        An ``asyncpg.Connection``.  Need not be inside a transaction.

    Raises
    ------
    MasterKeyMissingError
        If ``NCE_MASTER_KEY`` is absent.
    NoActiveSigningKeyError
        If no ``status='active'`` row exists in ``signing_keys``.
    SigningKeyDecryptionError
        If the stored key blob cannot be decrypted with the master key.
    """
    from nce.observability import SIGNING_KEY_CACHE_HIT_TOTAL, SIGNING_KEY_CACHE_MISS_TOTAL

    now = time.monotonic()
    try:
        entry: _CachedKey = _key_cache[_ACTIVE_KEY_CACHE_KEY]  # type: ignore[index]
        SIGNING_KEY_CACHE_HIT_TOTAL.inc()
        return entry.key_id, bytes(entry.raw_key.raw)
    except KeyError:
        pass  # Cache miss — fetch from DB

    async with _get_key_cache_lock():
        # Double-checked locking: another coroutine may have populated the cache
        # while we were waiting for the lock.
        try:
            entry = _key_cache[_ACTIVE_KEY_CACHE_KEY]  # type: ignore[index]
            SIGNING_KEY_CACHE_HIT_TOTAL.inc()
            return entry.key_id, bytes(entry.raw_key.raw)
        except KeyError:
            SIGNING_KEY_CACHE_MISS_TOTAL.inc()

        with require_master_key() as master_key:
            row = await conn.fetchrow("""
                SELECT key_id, encrypted_key
                FROM   signing_keys
                WHERE  status = 'active'
                ORDER  BY created_at DESC
                LIMIT  1
                """)
            if row is None:
                raise NoActiveSigningKeyError(
                    "No active signing key exists in signing_keys.  "
                    "Run the key-rotation initialisation to create the first key."
                )

            raw_key = decrypt_signing_key(bytes(row["encrypted_key"]), master_key)

        key_id: str = row["key_id"]

        # Store under "__active__" with its own MutableKeyBuffer.
        active_entry = _CachedKey(
            key_id=key_id,
            raw_key=MutableKeyBuffer(raw_key),
            expires_at=now + _KEY_CACHE_TTL_S,
        )
        # Also store under the real key_id with an independent MutableKeyBuffer copy
        # so get_key_by_id() finds it and evicting one slot does not zero the other.
        by_id_entry = _CachedKey(
            key_id=key_id,
            raw_key=MutableKeyBuffer(raw_key),
            expires_at=now + _KEY_CACHE_TTL_S,
        )
        _key_cache[_ACTIVE_KEY_CACHE_KEY] = active_entry
        _key_cache[key_id] = by_id_entry
        log.debug("Signing key cache refreshed (key_id=%s).", key_id)
        return active_entry.key_id, bytes(active_entry.raw_key.raw)


async def get_key_by_id(
    conn: asyncpg.Connection,
    key_id: str,
) -> bytes:
    """
    Return the raw signing key for a specific ``key_id``.

    Checks the module-level TTLCache first.  On a cache miss the key is
    fetched from ``signing_keys`` and decrypted.

    Parameters
    ----------
    conn:
        An ``asyncpg.Connection``.
    key_id:
        The ``key_id`` string to retrieve.

    Raises
    ------
    MasterKeyMissingError
        If ``NCE_MASTER_KEY`` is absent.
    NoActiveSigningKeyError
        If the ``key_id`` does not exist in ``signing_keys``.
    SigningKeyDecryptionError
        If the stored key blob cannot be decrypted with the master key.
    """
    from nce.observability import SIGNING_KEY_CACHE_HIT_TOTAL, SIGNING_KEY_CACHE_MISS_TOTAL

    now = time.monotonic()
    try:
        entry: _CachedKey = _key_cache[key_id]  # type: ignore[index]
        SIGNING_KEY_CACHE_HIT_TOTAL.inc()
        return bytes(entry.raw_key.raw)
    except KeyError:
        pass  # Cache miss — fetch from DB

    async with _get_key_cache_lock():
        # Double-checked locking
        try:
            entry = _key_cache[key_id]  # type: ignore[index]
            SIGNING_KEY_CACHE_HIT_TOTAL.inc()
            return bytes(entry.raw_key.raw)
        except KeyError:
            SIGNING_KEY_CACHE_MISS_TOTAL.inc()

        with require_master_key() as master_key:
            row = await conn.fetchrow(
                """
                SELECT encrypted_key
                FROM   signing_keys
                WHERE  key_id = $1
                """,
                key_id,
            )
            if row is None:
                raise NoActiveSigningKeyError(
                    f"Signing key '{key_id}' not found in signing_keys table."
                )

            raw_key = decrypt_signing_key(bytes(row["encrypted_key"]), master_key)

        new_entry = _CachedKey(
            key_id=key_id,
            raw_key=MutableKeyBuffer(raw_key),
            expires_at=now + _KEY_CACHE_TTL_S,
        )
        _key_cache[key_id] = new_entry
        log.debug("Signing key cached by id (key_id=%s).", key_id)
        return bytes(new_entry.raw_key.raw)


def sign_fields(fields: dict[str, Any], raw_signing_key: bytes) -> bytes:
    """
    Return HMAC-SHA256 over the JCS-canonical encoding of *fields*.

    *fields* must be a dict whose values are JSON-serialisable (strings,
    integers, nested dicts, None).  The signature is computed over the UTF-8
    bytes of the canonical JSON representation.
    """
    canonical_bytes = canonical_json(fields)
    return hmac.new(raw_signing_key, canonical_bytes, hashlib.sha256).digest()


def verify_fields(
    fields: dict[str, Any],
    raw_signing_key: bytes,
    expected_signature: bytes,
) -> bool:
    """
    Return ``True`` iff the HMAC-SHA256 of *fields* matches *expected_signature*.

    Uses ``hmac.compare_digest`` for constant-time comparison.
    """
    computed = sign_fields(fields, raw_signing_key)
    return hmac.compare_digest(computed, expected_signature)


def sign_audit_log_entry(
    entry_id: uuid.UUID,
    event_type: str,
    namespace_id: uuid.UUID,
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Sign an audit log entry.

    Serializes using canonical JCS serialization and signs via HMAC-SHA256
    derived from the environment master key. Returns a hex-encoded string.
    """
    payload = {
        "entry_id": str(entry_id),
        "event_type": event_type,
        "namespace_id": str(namespace_id),
        "metadata": metadata or {},
    }
    canonical_bytes = canonical_json(payload)
    with require_master_key() as master_key:
        sig = hmac.new(bytes(master_key.key_bytes), canonical_bytes, hashlib.sha256).hexdigest()
    return sig


async def admin_signing_keys_status(conn: asyncpg.Connection) -> dict[str, Any]:
    """
    Non-secret operational summary for the Admin API.

    Exposes metadata only — never raw key bytes, encrypted blobs, or master-key
    derivation parameters.
    """

    active = await conn.fetchrow(
        """
        SELECT key_id, created_at
        FROM   signing_keys
        WHERE  status = 'active'
        ORDER  BY created_at DESC
        LIMIT  1
        """
    )
    status_rows = await conn.fetch(
        """
        SELECT status, COUNT(*)::bigint AS cnt
        FROM   signing_keys
        GROUP BY status
        """
    )

    retired_at_row = await conn.fetchrow(
        """
        SELECT max(retired_at) AS latest_retired_at
        FROM   signing_keys
        WHERE  status = 'retired'
        """
    )

    by_status: dict[str, int] = {r["status"]: int(r["cnt"]) for r in status_rows}
    latest_ret = retired_at_row["latest_retired_at"] if retired_at_row else None

    return {
        "active_key_id": str(active["key_id"]) if active else None,
        "active_created_at": (
            active["created_at"].astimezone(timezone.utc).isoformat()
            if active and active.get("created_at")
            else None
        ),
        "keys_by_status": by_status,
        "latest_retired_at": (
            latest_ret.astimezone(timezone.utc).isoformat() if latest_ret is not None else None
        ),
        "signing_wire_format_notes": (
            "memories/events: HMAC-SHA256 over RFC 8785 (JCS) canonical JSON; "
            "see nce.signing.sign_fields."
        ),
    }


async def rotate_key(conn: asyncpg.Connection) -> str:
    """
    Generate and persist a new active signing key, retiring all current ones.

    Runs inside a DB transaction.  On success the in-memory cache is
    invalidated so the next ``get_active_key`` call loads the new key.

    Returns
    -------
    str
        The ``key_id`` of the newly created key.

    Raises
    ------
    MasterKeyMissingError
        If ``NCE_MASTER_KEY`` is absent.
    asyncpg.PostgresError
        Propagated unchanged on DB failure.
    """
    global _key_cache

    with require_master_key() as master_key:
        new_key_id = f"sk-{uuid.uuid4().hex[:16]}"
        # Wrap the freshly generated signing key material in SecureKeyBuffer
        # so the raw bytes are zeroed once the encrypted blob is produced.
        # os.urandom() returns a fresh bytes object — we immediately hand it
        # to SecureKeyBuffer which copies into a mutable bytearray.
        with SecureKeyBuffer(os.urandom(32)) as raw_buf:
            encrypted_blob = encrypt_signing_key(bytes(raw_buf), master_key)

        async with conn.transaction():
            await conn.execute("""
                UPDATE signing_keys
                SET    status = 'retired',
                       retired_at = now()
                WHERE  status = 'active'
                """)
            await conn.execute(
                """
                INSERT INTO signing_keys (key_id, encrypted_key, status)
                VALUES ($1, $2, 'active')
                """,
                new_key_id,
                encrypted_blob,
            )

    # Zero all cached key buffers, then clear the cache.
    # Iterate over a snapshot of keys to avoid mutation-during-iteration.
    for cache_key in list(_key_cache.keys()):
        entry: _CachedKey | None = _key_cache.get(cache_key)
        if entry is not None:
            try:
                entry.raw_key.zero()
            except Exception:
                pass
    _key_cache.clear()
    log.info("Signing key rotated.  New key_id=%s.", new_key_id)
    return new_key_id
