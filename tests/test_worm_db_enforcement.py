"""
Postgres integration tests for WORM enforcement under the actual `nce_app` role.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from nce.config import cfg
from nce.event_log import _WORM_TABLES


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nce_app_worm_privilege_enforcement():
    """
    Assert that the actual nce_app role is restricted and cannot UPDATE or DELETE WORM tables.
    """
    # 1. Obtain primary integration PG DSN
    primary_dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()

    if not primary_dsn:
        pytest.skip("Integration database DSN not configured — skipping WORM privilege tests.")

    # 2. Reconstruct DSN for nce_app
    try:
        parsed = urlparse(primary_dsn)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
        netloc = f"nce_app:{app_pass}@{netloc}"
        app_dsn = urlunparse(parsed._replace(netloc=netloc))
    except Exception as exc:
        pytest.skip(f"Failed to parse integration DSN: {exc}")

    # 3. Connect as nce_app and verify it blocks UPDATE/DELETE
    try:
        conn = await asyncpg.connect(app_dsn, timeout=10.0)
    except Exception as exc:
        pytest.skip(f"Could not connect as nce_app (role might not be initialized): {exc}")

    try:
        # Set dummy namespace ID session setting so RLS policies don't fail during WORM checks
        try:
            await conn.execute("SET nce.namespace_id = '00000000-0000-0000-0000-000000000000'")
        except Exception:
            pass

        for table in _WORM_TABLES:
            # UPDATE should raise InsufficientPrivilegeError
            col = "event_id" if table == "event_parents" else "id"
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError) as exc_info:
                await conn.execute(f"UPDATE {table} SET {col} = {col} WHERE FALSE")
            assert (
                "permission denied" in str(exc_info.value).lower()
                or "privilege" in str(exc_info.value).lower()
            )

            # DELETE should raise InsufficientPrivilegeError
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError) as exc_info:
                await conn.execute(f"DELETE FROM {table} WHERE FALSE")
            assert (
                "permission denied" in str(exc_info.value).lower()
                or "privilege" in str(exc_info.value).lower()
            )
    finally:
        await conn.close()
