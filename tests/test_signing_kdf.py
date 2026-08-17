"""Hardening tests for PBKDF2-HMAC-SHA256 wrapping keys (signing.py)."""

from __future__ import annotations

import hashlib
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nce.signing import (
    _ENCRYPTED_KEY_BLOB_V2,
    _ENCRYPTED_KEY_BLOB_V3,
    _ENCRYPTED_KEY_BLOB_V4,
    _HAS_ARGON2,
    _NONCE_SIZE,
    _PBKDF2_ITERATIONS,
    _PBKDF2_ITERATIONS_V4,
    MasterKey,
    SigningError,
    SigningKeyDecryptionError,
    _argon2id_derive_aes_key,
    _pbkdf2_derive_aes_key,
    _pbkdf2_derive_aes_key_v4,
    decrypt_signing_key,
    encrypt_signing_key,
)


def test_pbkdf2_iteration_count_is_at_least_100k():
    assert _PBKDF2_ITERATIONS >= 100_000


def test_pbkdf2_v4_iteration_count_is_at_least_600k():
    assert _PBKDF2_ITERATIONS_V4 >= 600_000


def test_derive_aes_key_is_deterministic_for_same_master():
    mk = MasterKey(b"m" * 32)
    a = mk.derive_aes_key()
    b = mk.derive_aes_key()
    assert len(a) == len(b) == 32
    assert a == b
    mk.zero()


def test_pbkdf2_different_salts_yield_different_keys():
    mk = MasterKey(b"n" * 32)
    k1 = _pbkdf2_derive_aes_key(mk, b"salt-one-aaaaaaaa")
    k2 = _pbkdf2_derive_aes_key(mk, b"salt-two-bbbbbbbb")
    assert k1 != k2
    mk.zero()


def test_pbkdf2_rejects_short_salt():
    mk = MasterKey(b"s" * 32)
    with pytest.raises(SigningError, match="PBKDF2 salt"):
        _pbkdf2_derive_aes_key(mk, b"short")
    mk.zero()


def test_encrypt_emits_magic_and_unique_salts():
    """New encrypts emit v3 (Argon2id) prefix when argon2-cffi is installed,
    or v4 (PBKDF2 @ 600K) as OWASP 2026 fallback."""
    mk = MasterKey(b"e" * 32)
    raw = b"payload-bytes"
    c1 = encrypt_signing_key(raw, mk)
    c2 = encrypt_signing_key(raw, mk)
    # With argon2-cffi installed, prefix should be v3; otherwise v4 (OWASP 2026)
    expected_prefix = _ENCRYPTED_KEY_BLOB_V3 if _HAS_ARGON2 else _ENCRYPTED_KEY_BLOB_V4
    assert c1.startswith(expected_prefix)
    assert c2.startswith(expected_prefix)
    # random salt + nonce => ciphertext should differ
    assert c1 != c2
    assert decrypt_signing_key(c1, mk) == raw
    assert decrypt_signing_key(c2, mk) == raw
    mk.zero()


def test_v2_blob_still_decrypts():
    """v2 (PBKDF2) blobs generated with the old format must still decrypt."""
    mk = MasterKey(b"v" * 32)
    raw = b"legacy-key-material-32b!"
    # Manually construct a v2 blob using PBKDF2
    salt = os.urandom(16)
    aes_key = _pbkdf2_derive_aes_key(mk, salt)
    nonce = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(nonce, raw, None)
    v2_blob = _ENCRYPTED_KEY_BLOB_V2 + salt + nonce + ct
    assert decrypt_signing_key(v2_blob, mk) == raw
    mk.zero()


def test_v3_blob_roundtrip():
    """v3 (Argon2id) encrypt → decrypt round-trip must work."""
    if not _HAS_ARGON2:
        pytest.skip("argon2-cffi not installed")
    mk = MasterKey(b"a" * 32)
    raw = os.urandom(32)
    blob = encrypt_signing_key(raw, mk)
    assert blob.startswith(_ENCRYPTED_KEY_BLOB_V3)
    assert decrypt_signing_key(blob, mk) == raw
    mk.zero()


def test_v3_blob_wrong_master_fails():
    """Decrypting a v3 blob with a different MasterKey must raise."""
    if not _HAS_ARGON2:
        pytest.skip("argon2-cffi not installed")
    mk1 = MasterKey(b"A" * 32)
    mk2 = MasterKey(b"B" * 32)
    raw = os.urandom(32)
    blob = encrypt_signing_key(raw, mk1)
    mk1.zero()
    with pytest.raises(Exception):
        decrypt_signing_key(blob, mk2)
    mk2.zero()


def test_argon2id_produces_different_key_than_pbkdf2():
    """Argon2id and PBKDF2 must yield different AES keys from the same master."""
    if not _HAS_ARGON2:
        pytest.skip("argon2-cffi not installed")
    mk = MasterKey(b"x" * 32)
    salt = os.urandom(16)
    k_argon = _argon2id_derive_aes_key(mk, salt)
    k_pbkdf2 = _pbkdf2_derive_aes_key(mk, salt)
    assert k_argon != k_pbkdf2
    mk.zero()


def test_legacy_sha256_wrapped_blob_still_decrypts():
    """Pre-PBKDF2 blobs (nonce || ciphertext+tag) must remain readable."""
    mk = MasterKey(b"L" * 32)
    raw = os.urandom(32)
    nonce = os.urandom(_NONCE_SIZE)
    aes_key = hashlib.sha256(bytes(mk.key_bytes)).digest()
    legacy = nonce + AESGCM(aes_key).encrypt(nonce, raw, None)
    assert not legacy.startswith(_ENCRYPTED_KEY_BLOB_V2)
    assert decrypt_signing_key(legacy, mk) == raw
    mk.zero()


def test_v2_blob_too_short_raises():
    mk = MasterKey(b"t" * 32)
    blob = _ENCRYPTED_KEY_BLOB_V2 + b"\x00" * 10
    with pytest.raises(SigningKeyDecryptionError, match="v2 blob is too short"):
        decrypt_signing_key(blob, mk)
    mk.zero()


def test_wrong_master_fails_v2():
    mk1 = MasterKey(b"1" * 32)
    mk2 = MasterKey(b"2" * 32)
    enc = encrypt_signing_key(b"secret", mk1)
    mk1.zero()
    with pytest.raises(SigningKeyDecryptionError):
        decrypt_signing_key(enc, mk2)
    mk2.zero()


# ---------------------------------------------------------------------------
# v4 (PBKDF2 @ 600K, OWASP 2026)
# ---------------------------------------------------------------------------


def test_v4_blob_still_decrypts_v2():
    """v4 decryption reads v2 (PBKDF2 @ 100K) blobs for backward compatibility."""
    mk = MasterKey(b"V" * 32)
    raw = b"legacy-v2-material"
    salt = os.urandom(16)
    aes_key = _pbkdf2_derive_aes_key(mk, salt)
    nonce = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(nonce, raw, None)
    v2_blob = _ENCRYPTED_KEY_BLOB_V2 + salt + nonce + ct
    assert decrypt_signing_key(v2_blob, mk) == raw
    mk.zero()


def test_v4_blob_roundtrip_without_argon2():
    """v4 (PBKDF2 @ 600K) encrypt → decrypt round-trip must work."""
    if _HAS_ARGON2:
        pytest.skip("argon2-cffi masks PBKDF2 v4 path — test separately without argon2-cffi")
    mk = MasterKey(b"V" * 32)
    raw = os.urandom(32)
    blob = encrypt_signing_key(raw, mk)
    assert blob.startswith(_ENCRYPTED_KEY_BLOB_V4)
    assert decrypt_signing_key(blob, mk) == raw
    mk.zero()


def test_v4_blob_wrong_master_fails():
    """Decrypting a v4 blob with a different MasterKey must raise."""
    if _HAS_ARGON2:
        pytest.skip("argon2-cffi masks PBKDF2 v4 path — test separately without argon2-cffi")
    mk1 = MasterKey(b"A" * 32)
    mk2 = MasterKey(b"B" * 32)
    raw = os.urandom(32)
    blob = encrypt_signing_key(raw, mk1)
    mk1.zero()
    with pytest.raises(SigningKeyDecryptionError):
        decrypt_signing_key(blob, mk2)
    mk2.zero()


def test_v4_blob_too_short_raises():
    mk = MasterKey(b"t" * 32)
    blob = _ENCRYPTED_KEY_BLOB_V4 + b"\x00" * 10
    with pytest.raises(SigningKeyDecryptionError, match="v4 blob is too short"):
        decrypt_signing_key(blob, mk)
    mk.zero()


def test_v4_pbkdf2_produces_different_key_than_v2():
    """v4 (600K) and v2 (100K) must yield different keys from same master+salt."""
    mk = MasterKey(b"y" * 32)
    salt = os.urandom(16)
    k_v2 = _pbkdf2_derive_aes_key(mk, salt)
    k_v4 = _pbkdf2_derive_aes_key_v4(mk, salt)
    assert k_v2 != k_v4
    mk.zero()


def test_v4_derive_aes_key_rejects_short_salt():
    mk = MasterKey(b"s" * 32)
    with pytest.raises(SigningError, match="PBKDF2 salt"):
        _pbkdf2_derive_aes_key_v4(mk, b"short")
    mk.zero()
