"""
NCE Internal Health Probe
Verifies connectivity to all required backends (Redis, PG, Mongo)
and checks embedding model readiness, or supports fast ``--liveness`` mode.
"""

import argparse
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
        print("CRITICAL: NCE_MASTER_KEY is missing or too short")
        return False

    # 1. Config Validation
    try:
        cfg.validate()
    except Exception as e:
        print(f"Config Error: {e}")
        return False

    # 2. Redis Probe
    try:
        from nce._client_factory import make_async_redis

        r = make_async_redis(cfg.REDIS_URL, socket_connect_timeout=2)
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
    client = None
    try:
        from nce._client_factory import motor_timeout_kwargs

        client = AsyncIOMotorClient(
            cfg.MONGO_URI, **motor_timeout_kwargs(serverSelectionTimeoutMS=2000)
        )
        await client.admin.command("ping")
    except Exception as e:
        print(f"MongoDB Connection Failed: {e}")
        return False
    finally:
        if client is not None:
            client.close()

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
                try:
                    data = resp.json() or {}
                    if str(data.get("engine", "")).strip().lower() == "stub":
                        if cfg.ENVIRONMENT in {"prod", "production"} and not getattr(
                            cfg, "NCE_ALLOW_COGNITIVE_STUB", False
                        ):
                            print("Cognitive Engine is running STUB in production")
                            return False
                except Exception:
                    pass
        except Exception as e:
            # Don't fail the whole probe if LLM is optional/down during boot,
            # but log it for the container health status.
            print(f"Cognitive Engine Probe Warning: {e}")
    else:
        print("Cognitive Engine Probe: Skipped (no NCE_COGNITIVE_BASE_URL set)")

    return True


async def liveness_probe() -> bool:
    """Fast in-memory liveness check (zero network/DB round-trips)."""
    if not cfg.NCE_MASTER_KEY or len(cfg.NCE_MASTER_KEY) < 32:
        print("CRITICAL: NCE_MASTER_KEY is missing or too short")
        return False
    try:
        cfg.validate()
    except Exception as e:
        print(f"Config Error: {e}")
        return False
    await asyncio.sleep(0)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NCE Internal Health Probe")
    parser.add_argument(
        "--liveness",
        action="store_true",
        help="Run fast in-memory config and event-loop check without DB/Redis/Mongo I/O.",
    )
    args = parser.parse_args()
    success = asyncio.run(liveness_probe() if args.liveness else probe())
    sys.exit(0 if success else 1)
