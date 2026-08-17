"""
nce/settings_store.py
=====================
BATCH-P5-V.1b — Dynamic settings store database accessor.

Provides precedence lookup (environment default < database override),
AES-256-GCM encryption for secret settings, a short-TTL local cache,
and multi-process invalidation via Redis pub/sub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from nce import admin_state
from nce.config import _Config, cfg
from nce.signing import decrypt_signing_key, encrypt_signing_key, require_master_key

logger = logging.getLogger("nce.settings_store")

# Cache configuration
CACHE_TTL = 5.0  # 5 seconds
_local_cache: dict[str, tuple[Any, bool, float]] = {}  # key -> (value, is_secret, expiry)

_listener_task: asyncio.Task[None] | None = None


def _get_pg_pool() -> Any:
    """Return the active PostgreSQL connection pool from admin state."""
    if admin_state.engine and admin_state.engine.pg_pool:
        return admin_state.engine.pg_pool
    return None


def _get_redis() -> Any:
    """Return the active Redis client from admin state."""
    if admin_state.engine and admin_state.engine.redis_client:
        return admin_state.engine.redis_client
    return None


async def _invalidation_listener(pubsub: Any) -> None:
    """Listens for cache invalidation messages on Redis pub/sub."""
    try:
        async for message in pubsub.listen():
            if message and message["type"] == "message":
                try:
                    data = message["data"]
                    key = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                    _local_cache.pop(key, None)
                    logger.debug("Invalidated local cache for setting: %s", key)
                except Exception as e:
                    logger.warning("Error processing invalidation message: %s", e)
    except asyncio.CancelledError:
        logger.debug("Settings invalidation listener cancelled")
    except Exception as e:
        logger.warning("Settings invalidation listener stopped with error: %s", e)


def _ensure_invalidation_listener(redis_client: Any) -> None:
    """Ensure the Redis pub/sub listener is running in the background."""
    global _listener_task
    if _listener_task is None or _listener_task.done():
        try:
            pubsub = redis_client.pubsub()

            async def start_subscriber() -> None:
                try:
                    await pubsub.subscribe("nce:settings:invalidate")
                    await _invalidation_listener(pubsub)
                except Exception as e:
                    logger.warning("Failed to subscribe to settings invalidation channel: %s", e)

            _listener_task = asyncio.create_task(start_subscriber())
            logger.debug("Started settings invalidation listener task")
        except Exception as e:
            logger.warning("Failed to setup pubsub listener: %s", e)


async def get(
    key: str,
    default: Any = None,
    *,
    pool: Any = None,
    redis_client: Any = None,
    decrypt_secrets: bool = True,
) -> Any:
    """
    Get a configuration setting.
    Precedence: database overrides > environment variables > default.
    Secrets are decrypted on demand unless decrypt_secrets=False.
    """
    # 1. Never query or store NCE_MASTER_KEY
    if key == "NCE_MASTER_KEY":
        return getattr(cfg, "NCE_MASTER_KEY", default)

    now = time.time()

    # 2. Check local in-process cache
    if key in _local_cache:
        val, is_sec, expiry = _local_cache[key]
        if now < expiry:
            if is_sec and not decrypt_secrets:
                return "••••set"
            return val

    # 3. Retrieve overrides from Redis/Postgres
    active_pool = pool or _get_pg_pool()
    active_redis = redis_client or _get_redis()

    if active_redis:
        _ensure_invalidation_listener(active_redis)

    redis_val: dict[str, Any] | None = None

    # Try Redis cache first
    if active_redis:
        try:
            redis_bytes = await active_redis.hget("nce:settings:overrides", key)
            if redis_bytes is not None:
                redis_val = json.loads(redis_bytes.decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to retrieve setting %s from Redis: %s", key, e)

    # Fallback to database
    if redis_val is None:
        db_row = None
        if active_pool:
            try:
                async with active_pool.acquire(timeout=10.0) as conn:
                    db_row = await conn.fetchrow(
                        "SELECT value, secret_enc, is_secret FROM settings WHERE key = $1",
                        key,
                    )
            except Exception as e:
                logger.error("Failed to query setting %s from Postgres: %s", key, e)

        if db_row is not None:
            is_secret = db_row["is_secret"]
            secret_enc = db_row["secret_enc"]
            db_val_raw = db_row["value"]

            redis_val = {
                "is_secret": is_secret,
                "secret_enc_hex": secret_enc.hex() if secret_enc else None,
                "value": db_val_raw,
            }

            # Cache in Redis overrides hash
            if active_redis:
                try:
                    await active_redis.hset("nce:settings:overrides", key, json.dumps(redis_val))
                except Exception as e:
                    logger.warning("Failed to cache setting %s in Redis: %s", key, e)
        else:
            # Not found in database, cache as unset (env default fallback)
            redis_val = {"is_unset": True}
            if active_redis:
                try:
                    await active_redis.hset("nce:settings:overrides", key, json.dumps(redis_val))
                except Exception:
                    pass

    # 4. Resolve the value
    if redis_val.get("is_unset"):
        env_val = getattr(cfg, key, None)
        final_val = env_val if env_val is not None else default
        _local_cache[key] = (final_val, False, now + CACHE_TTL)
        return final_val

    is_secret = redis_val.get("is_secret", False)
    if is_secret:
        secret_enc_hex = redis_val.get("secret_enc_hex")
        if secret_enc_hex:
            try:
                secret_enc = bytes.fromhex(secret_enc_hex)
                with require_master_key() as mk:
                    decrypted = decrypt_signing_key(secret_enc, mk)
                final_val = json.loads(decrypted.decode("utf-8"))
            except Exception as e:
                logger.error("Failed to decrypt secret setting %s: %s", key, e)
                final_val = default
        else:
            final_val = default

        _local_cache[key] = (final_val, True, now + CACHE_TTL)
        if not decrypt_secrets:
            return "••••set"
        return final_val
    else:
        val_raw = redis_val.get("value")
        if isinstance(val_raw, str):
            try:
                final_val = json.loads(val_raw)
            except Exception:
                final_val = val_raw
        else:
            final_val = val_raw

        _local_cache[key] = (final_val, False, now + CACHE_TTL)
        return final_val


async def set(
    key: str,
    value: Any,
    is_secret: bool = False,
    section: str | None = None,
    updated_by: str | None = None,
    *,
    pool: Any = None,
    redis_client: Any = None,
    conn: Any = None,
) -> None:
    """
    Set a configuration override in the database.
    Secrets are encrypted under the master key.
    """
    if key == "NCE_MASTER_KEY":
        raise ValueError("NCE_MASTER_KEY cannot be stored in the settings store.")

    active_pool = pool or _get_pg_pool()
    active_redis = redis_client or _get_redis()

    if is_secret:
        plaintext = json.dumps(value).encode("utf-8")
        with require_master_key() as mk:
            secret_enc = encrypt_signing_key(plaintext, mk)
        db_value = None
    else:
        secret_enc = None
        db_value = json.dumps(value)

    if conn:
        await conn.execute(
            """
            INSERT INTO settings (key, value, secret_enc, is_secret, section, updated_by, updated_at)
            VALUES ($1, $2::jsonb, $3, $4, $5, $6, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                secret_enc = EXCLUDED.secret_enc,
                is_secret = EXCLUDED.is_secret,
                section = COALESCE(EXCLUDED.section, settings.section),
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            """,
            key,
            db_value,
            secret_enc,
            is_secret,
            section,
            updated_by,
        )
    elif active_pool:
        async with active_pool.acquire(timeout=10.0) as conn_to_use:
            await conn_to_use.execute(
                """
                INSERT INTO settings (key, value, secret_enc, is_secret, section, updated_by, updated_at)
                VALUES ($1, $2::jsonb, $3, $4, $5, $6, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    secret_enc = EXCLUDED.secret_enc,
                    is_secret = EXCLUDED.is_secret,
                    section = COALESCE(EXCLUDED.section, settings.section),
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                key,
                db_value,
                secret_enc,
                is_secret,
                section,
                updated_by,
            )
    else:
        raise RuntimeError("No database pool available to save setting.")

    # Invalidate and populate Redis if not inside a connection transaction
    if not conn:
        if active_redis:
            try:
                cache_payload = {
                    "is_secret": is_secret,
                    "secret_enc_hex": secret_enc.hex() if secret_enc else None,
                    "value": value if not is_secret else None,
                }
                await active_redis.hset("nce:settings:overrides", key, json.dumps(cache_payload))
                await active_redis.publish("nce:settings:invalidate", key)
            except Exception as e:
                logger.warning("Failed to update Redis cache for setting %s: %s", key, e)

        # Invalidate local cache
        _local_cache.pop(key, None)


async def reset(key: str, *, pool: Any = None, redis_client: Any = None) -> None:
    """
    Delete a configuration override from the database, reverting to env defaults.
    """
    active_pool = pool or _get_pg_pool()
    active_redis = redis_client or _get_redis()

    if active_pool:
        async with active_pool.acquire(timeout=10.0) as conn:
            await conn.execute("DELETE FROM settings WHERE key = $1", key)

    if active_redis:
        try:
            await active_redis.hdel("nce:settings:overrides", key)
            await active_redis.publish("nce:settings:invalidate", key)
        except Exception as e:
            logger.warning("Failed to clear Redis cache for setting %s: %s", key, e)

    # Invalidate local cache
    _local_cache.pop(key, None)


# Monkeypatch _Config and cfg at module import time
async def _cfg_get(self: Any, key: str, default: Any = None) -> Any:
    return await get(key, default)


_Config.get = _cfg_get  # type: ignore[attr-defined]
