"""Async load test for recursive CTE graph traversal (local/dev only).

Requires PG_DSN — never embed credentials in this file.

Example::

    PG_DSN=postgresql://user:pass@localhost:5432/memory_meta python scripts/load_test_cte.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import asyncpg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("load-test-cte")


def _require_pg_dsn() -> str:
    dsn = os.getenv("PG_DSN", "").strip()
    if not dsn:
        logger.error(
            "PG_DSN is required. Example: "
            "PG_DSN=postgresql://user:pass@localhost:5432/memory_meta "
            "python scripts/load_test_cte.py"
        )
        sys.exit(2)
    return dsn


async def run_load_test() -> None:
    dsn = _require_pg_dsn()
    pool = await asyncpg.create_pool(dsn, min_size=5, max_size=20)

    query = """
    WITH RECURSIVE traversal AS (
        SELECT id, label, 0 AS depth, ARRAY[id] AS path
        FROM kg_nodes
        WHERE id = (SELECT id FROM kg_nodes LIMIT 1)
        UNION
        SELECT n.id, n.label, t.depth + 1, t.path || n.id
        FROM traversal t
        JOIN kg_edges e ON t.label = e.subject_label
        JOIN kg_nodes n ON e.object_label = n.label
        WHERE t.depth < 3
          AND n.id != ALL(t.path)
    )
    SELECT * FROM traversal LIMIT 100;
    """

    async def worker(i: int) -> float:
        async with pool.acquire() as conn:
            start = time.time()
            try:
                await conn.fetch(query)
            except Exception as e:
                logger.error("Worker %s error: %s", i, e)
            return time.time() - start

    logger.info("Starting recursive CTE load test with 50 concurrent connections...")
    start_total = time.time()
    results = await asyncio.gather(*(worker(i) for i in range(50)))
    total_time = time.time() - start_total

    logger.info("Ran 50 concurrent CTE traversals in %.2fs", total_time)
    logger.info("Average time per query: %.3fs", sum(results) / len(results))
    logger.info("Max time: %.3fs", max(results))
    logger.info("Min time: %.3fs", min(results))

    await pool.close()


if __name__ == "__main__":
    asyncio.run(run_load_test())
