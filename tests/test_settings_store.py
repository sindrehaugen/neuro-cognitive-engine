"""
tests/test_settings_store.py
============================
Unit tests for SettingsStore accessor (Batch 32).
Verified with mock pg_pool and redis_client for parallel-safe, containerless runs.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.config import cfg
from nce.settings_store import _local_cache, get, reset, set


@pytest.fixture(autouse=True)
def clear_local_cache():
    """Clear local settings cache before and after each test."""
    _local_cache.clear()
    yield
    _local_cache.clear()


@pytest.fixture
def mock_conn():
    c = AsyncMock()
    return c


@pytest.fixture
def mock_pool(mock_conn):
    pool = MagicMock()
    # Mock context manager: async with pool.acquire() as conn:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.fixture
def mock_redis():
    client = AsyncMock()
    # Mock pubsub object
    ps = AsyncMock()
    ps.subscribe = AsyncMock()
    client.pubsub = MagicMock(return_value=ps)
    return client


@pytest.mark.asyncio
async def test_settings_store_get_default(mock_pool, mock_redis):
    """Verify env default is returned when override is unset in DB and Redis."""
    mock_conn = mock_pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetchrow.return_value = None  # Not in DB
    mock_redis.hget.return_value = None  # Not in Redis

    # Ensure we use a key that has an env default in cfg
    val = await get("NCE_API_KEY", pool=mock_pool, redis_client=mock_redis)
    assert val == cfg.NCE_API_KEY


@pytest.mark.asyncio
async def test_settings_store_store_override_wins(mock_pool, mock_redis):
    """Verify store override takes precedence over env default."""
    mock_conn = mock_pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetchrow.return_value = {
        "value": json.dumps("overridden_api_key"),
        "secret_enc": None,
        "is_secret": False,
    }
    mock_redis.hget.return_value = None

    val = await get("NCE_API_KEY", pool=mock_pool, redis_client=mock_redis)
    assert val == "overridden_api_key"


@pytest.mark.asyncio
async def test_settings_store_secret_roundtrip(mock_pool, mock_redis):
    """Verify secrets are encrypted in the database and decrypt successfully on get."""
    mock_conn = mock_pool.acquire.return_value.__aenter__.return_value
    mock_redis.hget.return_value = None

    # Capture the parameters written to the DB
    db_args = {}

    async def mock_execute(sql, *args):
        # Basic parsing to extract args
        if "INSERT INTO settings" in sql:
            db_args["key"] = args[0]
            db_args["value"] = args[1]
            db_args["secret_enc"] = args[2]
            db_args["is_secret"] = args[3]
        return "INSERT 0 1"

    mock_conn.execute.side_effect = mock_execute

    secret_val = "my_super_secret_value"
    await set(
        "NCE_GEMINI_API_KEY",
        secret_val,
        is_secret=True,
        pool=mock_pool,
        redis_client=mock_redis,
    )

    # 1. Assert DB row values: value must be null, secret_enc must be encrypted bytes (not plaintext)
    assert db_args["key"] == "NCE_GEMINI_API_KEY"
    assert db_args["value"] is None
    assert db_args["is_secret"] is True
    assert db_args["secret_enc"] is not None
    assert db_args["secret_enc"] != secret_val.encode()

    # 2. Mock DB select returning the encrypted bytes
    mock_conn.fetchrow.return_value = {
        "value": None,
        "secret_enc": db_args["secret_enc"],
        "is_secret": True,
    }

    # 3. Verify get() returns the decrypted plaintext secret
    retrieved = await get("NCE_GEMINI_API_KEY", pool=mock_pool, redis_client=mock_redis)
    assert retrieved == secret_val


@pytest.mark.asyncio
async def test_settings_store_secret_masked(mock_pool, mock_redis):
    """Verify secrets return masked value '••••set' when decrypt_secrets=False is passed."""
    mock_conn = mock_pool.acquire.return_value.__aenter__.return_value
    mock_redis.hget.return_value = None

    # Set secret
    secret_val = "another_secret"
    await set(
        "NCE_OPENAI_API_KEY",
        secret_val,
        is_secret=True,
        pool=mock_pool,
        redis_client=mock_redis,
    )

    # Get raw encrypted bytes from mock conn execute captures
    # (Since we mocked the set execute call above, let's reuse a static encrypted bytes)
    from nce.signing import encrypt_signing_key, require_master_key

    with require_master_key() as mk:
        secret_enc = encrypt_signing_key(json.dumps(secret_val).encode(), mk)

    mock_conn.fetchrow.return_value = {
        "value": None,
        "secret_enc": secret_enc,
        "is_secret": True,
    }

    # Verify decrypt_secrets=False returns masked output
    masked = await get(
        "NCE_OPENAI_API_KEY",
        pool=mock_pool,
        redis_client=mock_redis,
        decrypt_secrets=False,
    )
    assert masked == "••••set"


@pytest.mark.asyncio
async def test_settings_store_master_key_rejection(mock_pool, mock_redis):
    """Verify NCE_MASTER_KEY cannot be written to settings store."""
    with pytest.raises(ValueError, match="NCE_MASTER_KEY cannot be stored"):
        await set("NCE_MASTER_KEY", "hacked_key", pool=mock_pool, redis_client=mock_redis)


@pytest.mark.asyncio
async def test_settings_store_cache_and_invalidation(mock_pool, mock_redis):
    """Verify local cache lookup, cache expiry, and invalidation."""
    mock_conn = mock_pool.acquire.return_value.__aenter__.return_value
    mock_redis.hget.return_value = None

    # 1. First lookup queries the database
    mock_conn.fetchrow.return_value = {
        "value": json.dumps("initial_value"),
        "secret_enc": None,
        "is_secret": False,
    }
    val1 = await get("MY_CONFIG_KEY", pool=mock_pool, redis_client=mock_redis)
    assert val1 == "initial_value"
    assert mock_conn.fetchrow.call_count == 1

    # 2. Second lookup hits the local cache (call count remains 1)
    val2 = await get("MY_CONFIG_KEY", pool=mock_pool, redis_client=mock_redis)
    assert val2 == "initial_value"
    assert mock_conn.fetchrow.call_count == 1

    # 3. Simulate cache expiry (setting expiry to the past)
    _local_cache["MY_CONFIG_KEY"] = ("initial_value", False, time.time() - 10)

    # 4. Lookup after expiry queries database again
    mock_conn.fetchrow.return_value = {
        "value": json.dumps("new_database_value"),
        "secret_enc": None,
        "is_secret": False,
    }
    val3 = await get("MY_CONFIG_KEY", pool=mock_pool, redis_client=mock_redis)
    assert val3 == "new_database_value"
    assert mock_conn.fetchrow.call_count == 2

    # 5. Calling set() invalidates local cache and publishes invalidate event
    await set(
        "MY_CONFIG_KEY",
        "manual_override",
        pool=mock_pool,
        redis_client=mock_redis,
    )
    assert "MY_CONFIG_KEY" not in _local_cache
    mock_redis.publish.assert_called_with("nce:settings:invalidate", "MY_CONFIG_KEY")

    # 6. Calling reset() invalidates local cache and deletes from DB/Redis
    await reset("MY_CONFIG_KEY", pool=mock_pool, redis_client=mock_redis)
    assert "MY_CONFIG_KEY" not in _local_cache
    mock_redis.publish.assert_called_with("nce:settings:invalidate", "MY_CONFIG_KEY")
    mock_conn.execute.assert_called_with("DELETE FROM settings WHERE key = $1", "MY_CONFIG_KEY")
