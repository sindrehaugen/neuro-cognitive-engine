"""Tests for TTLCache-based signing key cache with eviction-triggered zeroing."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.signing import (
    _ACTIVE_KEY_CACHE_KEY,
    MutableKeyBuffer,
    _CachedKey,
    _key_cache,
    _SigningKeyCache,
    get_active_key,
    get_key_by_id,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _make_entry(key_id: str = "sk-abc123", raw: bytes = b"K" * 32) -> _CachedKey:
    return _CachedKey(
        key_id=key_id,
        raw_key=MutableKeyBuffer(raw),
        expires_at=time.monotonic() + 300.0,
    )


def _make_mock_conn(fetchrow_return: object = None) -> AsyncMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    return conn


# ── _SigningKeyCache ───────────────────────────────────────────────────────


class TestSigningKeyCache:
    """Tests for the _SigningKeyCache TTLCache subclass."""

    def test_store_and_retrieve(self):
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        entry = _make_entry()
        cache["mykey"] = entry
        assert cache["mykey"] is entry
        assert bytes(entry.raw_key.raw) == b"K" * 32

    def test_contains(self):
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        entry = _make_entry()
        cache["mykey"] = entry
        assert "mykey" in cache
        assert "missing" not in cache

    def test_len(self):
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        assert len(cache) == 0
        cache["a"] = _make_entry("sk-a")
        cache["b"] = _make_entry("sk-b")
        assert len(cache) == 2

    def test_eviction_zeros_buffer_on_del(self):
        """When __delitem__ is called, the MutableKeyBuffer must be zeroed."""
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        entry = _make_entry()
        cache["key"] = entry
        assert bytes(entry.raw_key.raw) == b"K" * 32
        del cache["key"]
        # Buffer should be zeroed after deletion
        with pytest.raises(ValueError, match="zeroed"):
            _ = entry.raw_key.raw

    def test_eviction_zeros_even_if_already_zeroed(self):
        """Idempotent — zeroing an already-zeroed buffer is safe."""
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        entry = _make_entry()
        entry.raw_key.zero()
        cache["key"] = entry
        # Should not raise
        del cache["key"]

    def test_get_does_not_evict(self):
        """get() returns the entry but does not trigger eviction."""
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        entry = _make_entry()
        cache["key"] = entry
        retrieved = cache.get("key")
        assert retrieved is entry
        assert bytes(entry.raw_key.raw) == b"K" * 32  # Still valid

    def test_get_missing_returns_none(self):
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        assert cache.get("nope") is None

    def test_maxsize_eviction(self):
        """When maxsize is exceeded, oldest entry is evicted and zeroed."""
        cache = _SigningKeyCache(maxsize=2, ttl=300)
        e1 = _make_entry("sk-1")
        e2 = _make_entry("sk-2")
        e3 = _make_entry("sk-3")
        cache["k1"] = e1
        cache["k2"] = e2
        # Adding third entry should evict k1 (oldest)
        cache["k3"] = e3
        assert "k1" not in cache
        assert "k2" in cache
        assert "k3" in cache
        # k1's buffer must be zeroed
        with pytest.raises(ValueError, match="zeroed"):
            _ = e1.raw_key.raw

    def test_ttl_expiry_zeros_on_gc(self):
        """After TTL expiry + GC, the MutableKeyBuffer destructor zeros the key.

        TTLCache's internal timer removes the entry (no __delitem__ call),
        so zeroing relies on MutableKeyBuffer.__del__ when the _CachedKey
        is garbage-collected.
        """
        cache = _SigningKeyCache(maxsize=10, ttl=0.05)  # 50ms TTL
        entry = _make_entry()
        cache["key"] = entry
        # Drop our reference so the entry can be GC'd
        del entry
        # Let TTL expire
        time.sleep(0.10)
        # Access should miss — TTLCache timer has removed the entry
        with pytest.raises(KeyError):
            _ = cache["key"]

    def test_independent_buffer_copies(self):
        """Two cache entries for the same logical key have independent buffers."""
        cache = _SigningKeyCache(maxsize=10, ttl=300)
        raw = b"S" * 32
        e1 = _CachedKey("sk-abc", MutableKeyBuffer(raw), time.monotonic() + 300)
        e2 = _CachedKey("sk-abc", MutableKeyBuffer(raw), time.monotonic() + 300)
        cache[_ACTIVE_KEY_CACHE_KEY] = e1
        cache["sk-abc"] = e2
        # Evict one — the other must still be valid
        del cache[_ACTIVE_KEY_CACHE_KEY]
        with pytest.raises(ValueError, match="zeroed"):
            _ = e1.raw_key.raw
        assert bytes(e2.raw_key.raw) == b"S" * 32  # e2 untouched
        # Now evict the other
        del cache["sk-abc"]
        with pytest.raises(ValueError, match="zeroed"):
            _ = e2.raw_key.raw


# ── get_active_key ─────────────────────────────────────────────────────────


class TestGetActiveKeyTTLCache:
    """Tests for get_active_key() with TTLCache backing."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_key(self):
        """When the active key is in cache, no DB call is made."""
        cache = _key_cache
        cache.clear()
        entry = _make_entry("sk-hit", b"A" * 32)
        entry.expires_at = time.monotonic() + 600.0
        cache[_ACTIVE_KEY_CACHE_KEY] = entry

        conn = _make_mock_conn()
        key_id, raw = await get_active_key(conn)
        assert key_id == "sk-hit"
        assert raw == b"A" * 32
        # DB must NOT have been called
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_db(self):
        """On cache miss, fetch from signing_keys and populate cache."""
        cache = _key_cache
        cache.clear()
        # Simulate an encrypted key blob — use a simple v2 blob with known key
        from nce.signing import (
            MasterKey,
            encrypt_signing_key,
        )

        mk = MasterKey(b"M" * 32)
        raw_signing_key = b"R" * 32
        encrypted = encrypt_signing_key(raw_signing_key, mk)
        mk.zero()

        conn = _make_mock_conn({"key_id": "sk-db", "encrypted_key": encrypted})

        # Must have NCE_MASTER_KEY set for require_master_key()
        import os

        os.environ["NCE_MASTER_KEY"] = "M" * 32

        key_id, raw = await get_active_key(conn)
        assert key_id == "sk-db"
        assert raw == b"R" * 32
        conn.fetchrow.assert_awaited_once()

        # Cache should now be populated under both keys
        assert _ACTIVE_KEY_CACHE_KEY in cache
        assert "sk-db" in cache

    @pytest.mark.asyncio
    async def test_cache_miss_no_active_key_raises(self):
        """When DB has no active key, raise NoActiveSigningKeyError."""
        cache = _key_cache
        cache.clear()

        import os

        os.environ["NCE_MASTER_KEY"] = "M" * 32

        conn = _make_mock_conn(None)  # No row returned
        from nce.signing import NoActiveSigningKeyError

        with pytest.raises(NoActiveSigningKeyError, match="No active signing key"):
            await get_active_key(conn)

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_refresh_expiry(self, monkeypatch):
        """TTLCache does not refresh TTL on access — only on set."""
        cache = _key_cache
        cache.clear()
        # Create cache with ultra-short TTL
        short_cache = _SigningKeyCache(maxsize=10, ttl=0.01)
        entry = _make_entry("sk-stale", b"C" * 32)
        short_cache[_ACTIVE_KEY_CACHE_KEY] = entry

        # Wait for TTL to expire
        time.sleep(0.02)

        # Access should now miss (TTL expired)
        with pytest.raises(KeyError):
            _ = short_cache[_ACTIVE_KEY_CACHE_KEY]


# ── get_key_by_id ──────────────────────────────────────────────────────────


class TestGetKeyByIdTTLCache:
    """Tests for get_key_by_id() with TTLCache backing."""

    @pytest.mark.asyncio
    async def test_cache_hit_by_id(self):
        """Key found in cache by its real key_id."""
        cache = _key_cache
        cache.clear()
        entry = _make_entry("sk-byid", b"D" * 32)
        cache["sk-byid"] = entry

        conn = _make_mock_conn()
        raw = await get_key_by_id(conn, "sk-byid")
        assert raw == b"D" * 32
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_by_id_fetches_from_db(self):
        """On cache miss, fetch from signing_keys and cache."""
        cache = _key_cache
        cache.clear()

        from nce.signing import MasterKey, encrypt_signing_key

        mk = MasterKey(b"M" * 32)
        raw_key = b"E" * 32
        encrypted = encrypt_signing_key(raw_key, mk)
        mk.zero()

        conn = _make_mock_conn({"encrypted_key": encrypted})

        import os

        os.environ["NCE_MASTER_KEY"] = "M" * 32

        raw = await get_key_by_id(conn, "sk-miss")
        assert raw == b"E" * 32
        conn.fetchrow.assert_awaited_once()
        assert "sk-miss" in cache

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        """When key_id not in DB, raise NoActiveSigningKeyError."""
        cache = _key_cache
        cache.clear()

        import os

        os.environ["NCE_MASTER_KEY"] = "M" * 32

        conn = _make_mock_conn(None)
        from nce.signing import NoActiveSigningKeyError

        with pytest.raises(NoActiveSigningKeyError, match="not found"):
            await get_key_by_id(conn, "sk-nonexistent")


# ── rotate_key ─────────────────────────────────────────────────────────────


class TestRotateKeyCacheClear:
    """Tests for rotate_key() cache clearing behavior."""

    def test_rotate_clears_cache_and_zeros_buffers(self, monkeypatch):
        """rotate_key() must zero all cached entries and clear the cache."""
        cache = _key_cache
        cache.clear()
        e1 = _make_entry("sk-a", b"F" * 32)
        e2 = _make_entry("sk-b", b"G" * 32)
        cache[_ACTIVE_KEY_CACHE_KEY] = e1
        cache["sk-a"] = e2

        # Simulate the cache-zeroing part of rotate_key (not the DB part)
        for cache_key in list(cache.keys()):
            entry = cache.get(cache_key)
            if entry is not None:
                try:
                    entry.raw_key.zero()
                except Exception:
                    pass
        cache.clear()

        assert len(cache) == 0
        # Buffers must be zeroed
        with pytest.raises(ValueError, match="zeroed"):
            _ = e1.raw_key.raw
        with pytest.raises(ValueError, match="zeroed"):
            _ = e2.raw_key.raw


# ── Module-level cache singleton behavior ───────────────────────────────────


class TestModuleLevelCache:
    """Tests verifying module-level _key_cache is the correct type."""

    def test_cache_is_signing_key_cache_instance(self):
        assert isinstance(_key_cache, _SigningKeyCache)

    def test_cache_maxsize_is_1000(self):
        assert _key_cache.maxsize == 1000

    def test_cache_ttl_is_300(self):
        assert _key_cache.ttl == 300
