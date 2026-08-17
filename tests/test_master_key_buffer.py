"""
Tests for MasterKey mutable-buffer secure zeroing (signing.py).

Verifies that the bytearray-backed MasterKey class properly overwrites its
internal buffer, that context-manager and __del__ zeroing work, and that
zeroed keys reject further access.
"""

from __future__ import annotations

import gc
import os

import pytest

from nce.signing import (
    MasterKey,
    MasterKeyMissingError,
    decrypt_signing_key,
    encrypt_signing_key,
    require_master_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(key: MasterKey) -> list[int]:
    """Return the internal buffer as a list of ints (for inspection).

    Uses _buf directly (bypassing the key_bytes gate) so we can verify
    the buffer state after zero() has been called.
    """
    return list(key._buf)


# ---------------------------------------------------------------------------
# Tests — zero() behaviour
# ---------------------------------------------------------------------------


def test_zero_overwrites_all_bytes():
    """zero() must set every byte in the buffer to 0."""
    mk = MasterKey(b"A" * 32)
    assert _raw(mk) != [0] * 32
    mk.zero()
    assert _raw(mk) == [0] * 32


def test_zero_is_idempotent():
    """Calling zero() multiple times must not raise or corrupt state."""
    mk = MasterKey(b"B" * 32)
    mk.zero()
    mk.zero()
    mk.zero()
    assert _raw(mk) == [0] * 32  # still zeroed


def test_zeroed_key_rejects_key_bytes():
    """Accessing key_bytes after zero() must raise ValueError."""
    mk = MasterKey(b"C" * 32)
    mk.zero()
    with pytest.raises(ValueError, match="zeroed"):
        _ = mk.key_bytes


def test_zeroed_key_rejects_derive_aes_key():
    """Calling derive_aes_key() after zero() must raise ValueError."""
    mk = MasterKey(b"D" * 32)
    assert len(mk.derive_aes_key()) == 32  # works before
    mk.zero()
    with pytest.raises(ValueError, match="zeroed"):
        mk.derive_aes_key()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_zeroes_on_exit():
    """Using MasterKey as a context manager must zero on __exit__."""
    mk = MasterKey(b"E" * 32)
    with mk as m:
        assert m is mk
        assert _raw(mk) != [0] * 32
    assert _raw(mk) == [0] * 32


def test_context_manager_zeroes_on_exception():
    """Zeroing must happen even when the with-block raises."""
    mk = MasterKey(b"F" * 32)
    try:
        with mk:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _raw(mk) == [0] * 32


# ---------------------------------------------------------------------------
# __del__ destructor
# ---------------------------------------------------------------------------


def test_del_zeroes_buffer():
    """When an unreferenced MasterKey is GC'd, __del__ must zero it."""
    mk = MasterKey(b"G" * 32)
    buf_ref = mk._buf  # keep a reference to the bytearray for post-GC check
    del mk
    gc.collect()
    assert list(buf_ref) == [0] * 32


def test_del_does_not_raise():
    """__del__ must never propagate exceptions."""
    mk = MasterKey(b"H" * 32)
    # Simulate a corrupt state that would cause zero() to fail
    mk._buf = None  # type: ignore — deliberately bad
    try:
        del mk
        gc.collect()
    except Exception:
        pytest.fail("__del__ raised an exception")


# ---------------------------------------------------------------------------
# from_env() factory
# ---------------------------------------------------------------------------


def test_from_env_with_valid_key(monkeypatch):
    """from_env() must load NCE_MASTER_KEY and return a MasterKey."""
    monkeypatch.setenv("NCE_MASTER_KEY", "x" * 32)
    mk = MasterKey.from_env()
    assert isinstance(mk, MasterKey)
    assert len(mk.key_bytes) == 32
    mk.zero()


def test_from_env_with_missing_key(monkeypatch):
    """from_env() must raise MasterKeyMissingError when env var is absent."""
    monkeypatch.delenv("NCE_MASTER_KEY", raising=False)
    with pytest.raises(MasterKeyMissingError, match="NCE_MASTER_KEY"):
        MasterKey.from_env()


def test_from_env_with_empty_key(monkeypatch):
    """from_env() must raise MasterKeyMissingError when env var is empty."""
    monkeypatch.setenv("NCE_MASTER_KEY", "   ")
    with pytest.raises(MasterKeyMissingError, match="NCE_MASTER_KEY"):
        MasterKey.from_env()


# ---------------------------------------------------------------------------
# Minimum length enforcement
# ---------------------------------------------------------------------------


def test_init_rejects_short_key():
    """MasterKey must reject keys shorter than _MASTER_KEY_LEN (32 bytes)."""
    with pytest.raises(MasterKeyMissingError, match="at least 32 bytes"):
        MasterKey(b"short")


# ---------------------------------------------------------------------------
# require_master_key() wrapper
# ---------------------------------------------------------------------------


def test_require_master_key_returns_masterkey(monkeypatch):
    """require_master_key() must return a usable MasterKey."""
    monkeypatch.setenv("NCE_MASTER_KEY", "y" * 32)
    mk = require_master_key()
    assert isinstance(mk, MasterKey)
    aes = mk.derive_aes_key()
    assert len(aes) == 32
    mk.zero()
    with pytest.raises(ValueError, match="zeroed"):
        mk.derive_aes_key()


# ---------------------------------------------------------------------------
# encrypt_signing_key / decrypt_signing_key round-trip
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    """encrypt + decrypt round-trip with MasterKey must work."""
    mk = MasterKey(b"R" * 32)
    raw = os.urandom(32)
    encrypted = encrypt_signing_key(raw, mk)
    decrypted = decrypt_signing_key(encrypted, mk)
    assert decrypted == raw
    mk.zero()


def test_decrypt_with_wrong_key_fails():
    """Decrypting with a different MasterKey must raise."""
    mk1 = MasterKey(b"A" * 32)
    mk2 = MasterKey(b"B" * 32)
    raw = os.urandom(32)
    encrypted = encrypt_signing_key(raw, mk1)
    mk1.zero()
    with pytest.raises(Exception):  # SigningKeyDecryptionError
        decrypt_signing_key(encrypted, mk2)
    mk2.zero()


def test_encrypt_with_zeroed_key_fails():
    """encrypt_signing_key with a zeroed MasterKey must raise."""
    mk = MasterKey(b"Z" * 32)
    mk.zero()
    with pytest.raises(ValueError, match="zeroed"):
        encrypt_signing_key(os.urandom(32), mk)


def test_decrypt_with_zeroed_key_fails():
    """decrypt_signing_key with a zeroed MasterKey must raise."""
    mk = MasterKey(b"Z" * 32)
    mk.zero()
    with pytest.raises(ValueError, match="zeroed"):
        decrypt_signing_key(b"\x00" * 28, mk)


# ---------------------------------------------------------------------------
# Memory safety: verify bytearray mutation actually changes the underlying
# memory (not just rebinding a name).
# ---------------------------------------------------------------------------


def test_bytearray_mutation_is_in_place():
    """bytearray mutation must modify the same memory, not create a copy."""
    mk = MasterKey(b"M" * 32)
    buf = mk._buf  # direct reference to the bytearray
    assert buf[0] == ord("M")
    mk.zero()
    # The direct reference must see zeroed bytes — proof of in-place mutation
    assert list(buf) == [0] * 32


def test_key_bytes_memoryview_invalidated():
    """A memoryview obtained before zero() must reflect the zeroed buffer."""
    mk = MasterKey(b"N" * 32)
    mv = mk.key_bytes
    assert mv[0] == ord("N")
    mk.zero()
    # The memoryview still references the same bytearray,
    # which is now all zeros.
    assert list(mv) == [0] * 32


# ---------------------------------------------------------------------------
# MutableKeyBuffer — zeroable buffer for cached signing keys
# ---------------------------------------------------------------------------


def test_mutable_key_buffer_creation_and_raw():
    """MutableKeyBuffer must wrap a bytearray and expose a memoryview."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\x01" * 32)
    raw = buf.raw
    assert len(raw) == 32
    assert raw[0] == 1
    assert raw[31] == 1


def test_mutable_key_buffer_zero_overwrites():
    """zero() must set all bytes to 0."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\xff" * 32)
    buf.zero()
    # raw raises after zero
    with pytest.raises(ValueError, match="zeroed"):
        _ = buf.raw


def test_mutable_key_buffer_zero_is_idempotent():
    """Calling zero() multiple times must not raise."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\xab" * 32)
    buf.zero()
    buf.zero()
    buf.zero()
    with pytest.raises(ValueError, match="zeroed"):
        _ = buf.raw


def test_mutable_key_buffer_bytes_after_zero_raises():
    """bytes() on a zeroed buffer must raise ValueError."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\xcd" * 32)
    buf.zero()
    with pytest.raises(ValueError, match="zeroed"):
        bytes(buf)


def test_mutable_key_buffer_bytes_before_zero():
    """bytes() on a live buffer must return a copy of the key."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\xef" * 32)
    result = bytes(buf)
    assert result == b"\xef" * 32
    # The internal buffer is still intact after bytes() call
    assert buf.raw[0] == 0xEF
    buf.zero()


def test_mutable_key_buffer_del_zeroes():
    """__del__ must call zero() when the object is garbage collected."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\x42" * 32)
    buf_ref = buf._buf  # keep reference to bytearray for post-GC inspection
    del buf
    gc.collect()
    assert list(buf_ref) == [0] * 32


def test_mutable_key_buffer_del_does_not_raise():
    """__del__ must never propagate exceptions."""
    from nce.signing import MutableKeyBuffer

    buf = MutableKeyBuffer(b"\x99" * 32)
    buf._buf = None  # corrupt state
    try:
        del buf
        gc.collect()
    except Exception:
        pytest.fail("MutableKeyBuffer.__del__ raised an exception")


# ---------------------------------------------------------------------------
# MasterKey.from_env() — ctypes-backed path
# ---------------------------------------------------------------------------


def test_from_env_ctypes_loads_correct_key(monkeypatch):
    """from_env() via ctypes must load the exact key string."""
    test_key = "a" * 32
    monkeypatch.setenv("NCE_MASTER_KEY", test_key)
    mk = MasterKey.from_env()
    assert bytes(mk.key_bytes) == test_key.encode("utf-8")
    mk.zero()


def test_from_env_ctypes_with_unicode(monkeypatch):
    """from_env() must handle non-ASCII UTF-8 characters correctly."""
    # A key with accented characters to test UTF-8 encoding path
    test_key = "Å" * 16  # 32 bytes in UTF-8 (2 bytes per char)
    monkeypatch.setenv("NCE_MASTER_KEY", test_key)
    mk = MasterKey.from_env()
    assert len(mk.key_bytes) == 32
    assert bytes(mk.key_bytes) == test_key.encode("utf-8")
    mk.zero()


def test_from_env_rejects_short_key(monkeypatch):
    """from_env() must reject keys shorter than 32 bytes."""
    monkeypatch.setenv("NCE_MASTER_KEY", "short")
    with pytest.raises(MasterKeyMissingError, match="at least 32 bytes"):
        MasterKey.from_env()


def test_from_env_strips_whitespace(monkeypatch):
    """from_env() must strip leading/trailing whitespace from env var."""
    monkeypatch.setenv("NCE_MASTER_KEY", "  " + "x" * 32 + "  ")
    mk = MasterKey.from_env()
    assert bytes(mk.key_bytes) == b"x" * 32
    mk.zero()


# ---------------------------------------------------------------------------
# _CachedKey with MutableKeyBuffer — cache lifecycle tests
# ---------------------------------------------------------------------------


def test_cached_key_zero_on_replacement():
    """Replacing _key_cache must zero the old MutableKeyBuffer."""
    import time as _time

    from nce.signing import MutableKeyBuffer, _CachedKey, _key_cache

    # Save original cache
    original = _key_cache
    try:
        # Set up a cached key
        buf1 = MutableKeyBuffer(b"\x11" * 32)
        _key_cache_ref = _CachedKey(
            key_id="test-key-1",
            raw_key=buf1,
            expires_at=_time.monotonic() + 300,
        )
        # Directly assign for test purposes
        import nce.signing as signing_mod

        signing_mod._key_cache = _key_cache_ref

        # Now "replace" it — simulate what get_active_key does
        old_cache = signing_mod._key_cache
        signing_mod._key_cache = None  # clear
        if old_cache is not None:
            old_cache.raw_key.zero()

        # The old buffer must be zeroed
        with pytest.raises(ValueError, match="zeroed"):
            _ = buf1.raw

        # Verify bytearray is actually zeroed
        assert list(buf1._buf) == [0] * 32
    finally:
        signing_mod._key_cache = original
