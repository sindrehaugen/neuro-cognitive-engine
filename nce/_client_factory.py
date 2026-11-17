"""Centralized client factory for Redis, MongoDB (Motor), and MinIO connections.

Ensures bounded connect/socket timeouts, health-check intervals, and urllib3
connection-pool retries are applied uniformly across all services and workers.
"""

from __future__ import annotations

from typing import Any

import redis
import redis.asyncio as aioredis
import urllib3

from nce.config import cfg


def make_async_redis(url: str, *, client_cls: Any = None, **kw: Any) -> Any:
    """Create an async Redis client with bounded connect/socket timeouts."""
    cls = client_cls or aioredis
    opts: dict[str, Any] = {
        "socket_connect_timeout": cfg.REDIS_CONNECT_TIMEOUT_S,
        "socket_timeout": cfg.REDIS_SOCKET_TIMEOUT_S,
        "health_check_interval": 30,
    }
    opts.update(kw)
    return cls.from_url(url, **opts)


def make_sync_redis(url: str, **kw: Any) -> redis.Redis:
    """Create a sync Redis client with bounded connect/socket timeouts."""
    opts: dict[str, Any] = {
        "socket_connect_timeout": cfg.REDIS_CONNECT_TIMEOUT_S,
        "socket_timeout": cfg.REDIS_SOCKET_TIMEOUT_S,
        "health_check_interval": 30,
    }
    opts.update(kw)
    return redis.from_url(url, **opts)


def motor_timeout_kwargs(**kw: Any) -> dict[str, Any]:
    """Return bounded timeout kwargs for Motor ``AsyncIOMotorClient``."""
    opts: dict[str, Any] = {
        "serverSelectionTimeoutMS": 5000,
        "connectTimeoutMS": 5000,
        "socketTimeoutMS": 5000,
    }
    opts.update(kw)
    return opts


def make_minio(
    endpoint: str,
    access_key: str,
    secret_key: str,
    secure: bool = False,
    *,
    maxsize: int = 16,
    minio_cls: Any = None,
) -> Any:
    """Create a MinIO client backed by a bounded urllib3 PoolManager."""
    if minio_cls is None:
        from minio import Minio

        minio_cls = Minio

    http_client = urllib3.PoolManager(
        maxsize=maxsize,
        timeout=urllib3.Timeout(connect=2.0, read=5.0),
        retries=urllib3.Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
        ),
    )
    return minio_cls(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        http_client=http_client,
    )
