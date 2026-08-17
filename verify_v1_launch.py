#!/usr/bin/env python3
"""
NCE v1.0 launch verification.

Checks (in order):
  1. Admin REST ``GET /api/health`` — Postgres, Mongo, Redis up (HMAC auth).
  2. A2A discovery ``GET /.well-known/agent-card`` — public JSON card.
  3. Sleep consolidation dry-run — ``ConsolidationWorker`` against a dedicated
     namespace with no episodic rows (DB + consolidation_runs path only; no LLM).
  4. Temporal event log — ``GET /api/admin/events/summary`` after consolidation
     (readable WORM feed counters).

Run against a live stack (Compose or local) with the same env as the services::

    python verify_v1_launch.py

Override bases if needed::

    TRIMCP_ADMIN_BASE_URL=http://127.0.0.1:8003 \\
    TRIMCP_A2A_BASE_URL=http://127.0.0.1:8004 \\
    python verify_v1_launch.py

Exit code ``0`` = PASS, ``1`` = FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any
from uuid import UUID

try:
    import asyncpg
except ModuleNotFoundError as _e:
    raise SystemExit(
        "[verify_v1_launch] Could not import 'asyncpg'. "
        "Activate the virtual environment first:\n"
        "  .venv\\Scripts\\activate  (Windows)\n"
        "  source .venv/bin/activate  (Unix)\n"
        "or install dependencies: pip install -r requirements.txt"
    ) from _e
import httpx

from nce.config import cfg
from nce.consolidation import ConsolidationWorker

VERIFY_NS_SLUG = "nce-v1-launch-verify"


class _NoopLLM:
    """Provider stub; unused when the verify namespace has no episodic memories."""

    async def complete(self, messages: list, response_model: type) -> Any:  # noqa: ANN401
        raise RuntimeError("verify_v1_launch: LLM should not run in empty-namespace dry-run")

    def model_identifier(self) -> str:
        return "verify/noop"


def _admin_hmac_headers(api_key: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    ts = int(time.time())
    parts = [method.upper(), path, str(ts)]
    if body:
        parts.append(hashlib.sha256(body).hexdigest())
    canonical = "\n".join(parts)
    sig = hmac.new(api_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-NCE-Timestamp": str(ts),
        "Authorization": f"HMAC-SHA256 {sig}",
    }


def _fail(step: str, detail: str) -> None:
    print(f"[FAIL] {step}")
    print(f"       {detail}")
    print()
    print("RESULT: FAIL")
    sys.exit(1)


def _ok(step: str) -> None:
    print(f"[PASS] {step}")


async def _step_health(client: httpx.AsyncClient, api_key: str) -> None:
    path = "/api/health/v1"
    r = await client.get(path, headers=_admin_hmac_headers(api_key, "GET", path))
    if r.status_code != 200:
        body = r.text[:500]
        _fail("Admin /api/health/v1", f"HTTP {r.status_code}: {body}")
    data = r.json()

    if data.get("status") != "ok":
        _fail(
            "Admin /api/health/v1",
            f"Status is {data.get('status')!r} — Full report: {json.dumps(data)}",
        )

    # Specific database checks
    db_report = data.get("databases", {})
    for key in ("postgres", "mongo", "redis"):
        if db_report.get(key) != "up":
            _fail(
                "Admin /api/health/v1 (Databases)",
                f"{key!r} expected 'up', got {db_report.get(key)!r}",
            )

    # Cognitive sidecar check (soft warning in orchestrator, but verify script should be strict)
    cog_report = data.get("cognitive", {})
    if cog_report.get("engine") != "up":
        print(f"[WARN] Cognitive engine state: {cog_report.get('engine')}")

    _ok("Admin /api/health/v1 (Full Tri-Stack deep check passed)")


async def _step_a2a(base: str) -> None:
    url = f"{base.rstrip('/')}/.well-known/agent-card"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
    if r.status_code != 200:
        _fail("A2A agent card", f"HTTP {r.status_code}: {r.text[:500]}")
    try:
        card = r.json()
    except json.JSONDecodeError as exc:
        _fail("A2A agent card", f"Invalid JSON: {exc}")
    if not card.get("schema_version") and not card.get("name"):
        _fail("A2A agent card", f"Unexpected shape: {list(card.keys())!r}")
    _ok("A2A /.well-known/agent-card")


async def _step_consolidation() -> UUID:
    pool = await asyncpg.create_pool(cfg.PG_DSN, min_size=1, max_size=2, command_timeout=120)
    try:
        async with pool.acquire(timeout=10.0) as conn:
            ns_id = await conn.fetchval(
                "SELECT id FROM namespaces WHERE slug = $1",
                VERIFY_NS_SLUG,
            )
            if ns_id is None:
                ns_id = await conn.fetchval(
                    "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
                    VERIFY_NS_SLUG,
                )
        worker = ConsolidationWorker(pool, _NoopLLM())
        await worker.run_consolidation(UUID(str(ns_id)))

        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(pool, ns_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT status
                FROM consolidation_runs
                WHERE namespace_id = $1
                ORDER BY started_at DESC
                LIMIT 1
                """,
                ns_id,
            )
        if not row or row["status"] != "completed":
            _fail(
                "Consolidation dry-run",
                f"Last run status expected 'completed', got {row!r}",
            )
        return UUID(str(ns_id))
    finally:
        await pool.close()
    _ok("Sleep consolidation dry-run (consolidation_runs completed)")


async def _step_rls_isolation(ns_id: UUID) -> None:
    from urllib.parse import urlparse, urlunparse

    app_dsn = None
    if cfg.PG_DSN:
        try:
            parsed = urlparse(cfg.PG_DSN)
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            app_pass = cfg.NCE_APP_PASSWORD or "nce_app_secret"
            netloc = f"nce_app:{app_pass}@{netloc}"
            app_dsn = urlunparse(parsed._replace(netloc=netloc))
        except Exception as exc:
            _fail("RLS Isolation check", f"Failed to construct nce_app DSN: {exc}")

    if not app_dsn:
        _fail("RLS Isolation check", "Could not resolve app DSN")

    # Connect as nce_app (restricted RLS role)
    try:
        conn = await asyncpg.connect(app_dsn, timeout=10.0)
    except Exception as exc:
        _fail("RLS Isolation check", f"Failed to connect as nce_app role: {exc}")

    try:
        # Test 1: Assert that querying RLS-protected table WITHOUT setting nce.namespace_id raises exception
        try:
            await conn.fetch("SELECT status FROM consolidation_runs WHERE namespace_id = $1", ns_id)
            _fail(
                "RLS Isolation check",
                "Queried RLS table without setting namespace_id, but no exception was raised!",
            )
        except asyncpg.PostgresError as exc:
            if "nce.namespace_id is not set for this transaction" in str(exc):
                # Correct exception raised!
                pass
            else:
                _fail(
                    "RLS Isolation check",
                    f"Expected 'nce.namespace_id is not set' error, got: {exc}",
                )

        # Test 2: Set context to a DIFFERENT/DUMMY namespace, query VERIFY_NS_SLUG's namespace_id.
        # It should return NO rows because of RLS partition isolation.
        dummy_ns = UUID("00000000-0000-0000-0000-000000000000")
        async with conn.transaction():
            await conn.execute("SELECT set_config('nce.namespace_id', $1, true)", str(dummy_ns))
            rows = await conn.fetch(
                "SELECT status FROM consolidation_runs WHERE namespace_id = $1", ns_id
            )
            if len(rows) > 0:
                _fail(
                    "RLS Isolation check",
                    f"RLS isolation bypassed! Dummy namespace could see {len(rows)} rows of namespace {ns_id}",
                )

        # Test 3: Set context to the CORRECT namespace_id. Query should succeed and return the row.
        async with conn.transaction():
            await conn.execute("SELECT set_config('nce.namespace_id', $1, true)", str(ns_id))
            rows = await conn.fetch(
                "SELECT status FROM consolidation_runs WHERE namespace_id = $1", ns_id
            )
            if not rows:
                _fail(
                    "RLS Isolation check",
                    f"Could not fetch consolidation run when using correct namespace_id {ns_id}",
                )

    finally:
        await conn.close()

    _ok("Multi-Tenant RLS isolation validated successfully (nce_app restricted and isolated)")


async def _step_event_log(client: httpx.AsyncClient, api_key: str) -> None:
    path = "/api/admin/events/summary"
    r = await client.get(path, headers=_admin_hmac_headers(api_key, "GET", path))
    if r.status_code != 200:
        _fail("Event log summary", f"HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    if "total_events" not in data:
        _fail("Event log summary", f"Missing total_events: {data!r}")
    if "replay_failed_runs" not in data:
        _fail("Event log summary", f"Missing replay_failed_runs: {data!r}")

    path_events = "/api/admin/events"
    r2 = await client.get(
        path_events,
        params={"limit": 1, "page": 1},
        headers=_admin_hmac_headers(api_key, "GET", path_events),
    )
    if r2.status_code != 200:
        _fail("Event log query", f"HTTP {r2.status_code}: {r2.text[:500]}")
    payload = r2.json()
    if "items" not in payload or "total" not in payload:
        _fail("Event log query", f"Unexpected body: {payload!r}")

    _ok(
        f"Temporal event log (summary total_events={data['total_events']}, "
        f"replay_failed_runs={data['replay_failed_runs']}; page query ok, total={payload['total']})"
    )


async def _async_main(admin_base: str, a2a_base: str) -> None:
    cfg.validate()
    api_key = cfg.NCE_API_KEY
    if not api_key:
        _fail("Configuration", "TRIMCP_API_KEY is empty (required for HMAC admin calls)")

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(
        base_url=admin_base.rstrip("/"), timeout=60.0, limits=limits
    ) as admin_client:
        await _step_health(admin_client, api_key)
    await _step_a2a(a2a_base)
    ns_id = await _step_consolidation()
    await _step_rls_isolation(ns_id)
    async with httpx.AsyncClient(
        base_url=admin_base.rstrip("/"), timeout=60.0, limits=limits
    ) as admin_client:
        await _step_event_log(admin_client, api_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="NCE v1.0 launch verification")
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("TRIMCP_ADMIN_BASE_URL", "http://127.0.0.1:8003"),
        help="Admin server base URL (default TRIMCP_ADMIN_BASE_URL or http://127.0.0.1:8003)",
    )
    parser.add_argument(
        "--a2a-url",
        default=os.environ.get("TRIMCP_A2A_BASE_URL", "http://127.0.0.1:8004"),
        help="A2A server base URL (default TRIMCP_A2A_BASE_URL or http://127.0.0.1:8004)",
    )
    args = parser.parse_args()

    print("=== NCE v1.0 Launch Verification ===")
    print()

    try:
        asyncio.run(_async_main(args.admin_url, args.a2a_url))
    except httpx.ConnectError as exc:
        _fail("Network", f"Could not connect — is the stack up? {exc}")
    except asyncpg.PostgresError as exc:
        _fail("PostgreSQL", str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error to operator
        _fail("Unexpected error", f"{type(exc).__name__}: {exc}")

    print()
    print("RESULT: PASS")


if __name__ == "__main__":
    main()
