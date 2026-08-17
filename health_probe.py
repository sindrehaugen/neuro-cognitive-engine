"""
NCE Internal Health Probe
Verifies connectivity to all required backends (Redis, PG, Mongo)
and checks embedding model readiness.
"""

import asyncio
import logging
import sys

import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient

from nce.config import cfg

logging.basicConfig(level=logging.ERROR)


async def probe():
    # 0. P0 Security Check: Master Key
    if not cfg.NCE_MASTER_KEY or len(cfg.NCE_MASTER_KEY) < 32:
        print("CRITICAL: TRIMCP_MASTER_KEY is missing or too short")
        return False

    # 1. Config Validation
    try:
        cfg.validate()
    except Exception as e:
        print(f"Config Error: {e}")
        return False

    # 2. Redis Probe
    try:
        from redis.asyncio import from_url as async_redis_from_url

        r = async_redis_from_url(cfg.REDIS_URL, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
    except Exception as e:
        print(f"Redis Connection Failed: {e}")
        return False

    # 3. Postgres Probe
    try:
        conn = await asyncpg.connect(cfg.PG_DSN, timeout=5)
        # Verify pgvector extension is functional by running a distance query
        await conn.execute("SELECT '[1.0, 2.0]'::vector <=> '[1.0, 2.0]'::vector")
        await conn.close()
    except Exception as e:
        print(f"Postgres Connection Failed (pgvector validation failed): {e}")
        return False

    # 4. MongoDB Probe
    try:
        client = AsyncIOMotorClient(cfg.MONGO_URI, serverSelectionTimeoutMS=2000)
        await client.admin.command("ping")
    except Exception as e:
        print(f"MongoDB Connection Failed: {e}")
        return False

    # 5. Embedding Engine Check (Soft Check)
    # We check if the model is reachable or loaded if we are the worker/server
    # For now, reaching the cognitive sidecar is a good proxy.
    if cfg.NCE_COGNITIVE_BASE_URL:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{cfg.NCE_COGNITIVE_BASE_URL}/health")
                if resp.status_code != 200:
                    print(f"Cognitive Engine Unhealthy: {resp.status_code}")
                    return False
        except Exception as e:
            # Don't fail the whole probe if LLM is optional/down during boot,
            # but log it for the container health status.
            print(f"Cognitive Engine Probe Warning: {e}")
    else:
        print("Cognitive Engine Probe: Skipped (no TRIMCP_COGNITIVE_BASE_URL set)")

    return True


if __name__ == "__main__":
    success = asyncio.run(probe())
    sys.exit(0 if success else 1)
