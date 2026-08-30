"""MCP stdio server lifecycle — connect engine, background tasks, stdio transport."""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server

from nce import NCEEngine, run_gc_loop
from nce.config import assert_admin_override_not_in_production, cfg, redact_secrets_in_text
from nce.mcp_stdio_dispatch import execute_call_tool
from nce.mcp_stdio_tools import TOOLS

log = logging.getLogger("nce-mcp")


def create_stdio_app(*, engine: NCEEngine) -> Server:
    """Build the MCP Server with tool handlers bound to ``engine``."""
    app = Server("nce-memory")

    @app.list_tools()
    async def list_tools():
        return TOOLS

    @app.call_tool()
    async def call_tool(name: str, arguments: dict):
        return await execute_call_tool(engine, name, arguments)

    return app


async def run_stdio_server(*, app: Server | None = None, engine: NCEEngine | None = None) -> None:
    """Connect NCEEngine, start background loops, and serve MCP over stdio."""
    assert_admin_override_not_in_production()

    from io import TextIOWrapper

    import anyio

    from nce.mcp_middleware import StderrSanitizer

    original_stdout = sys.stdout
    original_stdout_wrapped = anyio.wrap_file(
        TextIOWrapper(original_stdout.buffer, encoding="utf-8")
    )

    with StderrSanitizer():
        if engine is None:
            engine = NCEEngine()

        from nce.observability import init_observability

        init_observability()
        log.info("Observability layer initialized.")

        try:
            await engine.connect()
        except Exception as exc:
            log.critical("FATAL: Startup failure: %s", redact_secrets_in_text(str(exc)))
            sys.exit(1)

        log.info("NCEEngine connected to all database layers.")

        # Warm the governance cache before serving so production reads the
        # admin-disabled snapshot up front. On failure we log and rely on the
        # never-initialized prod-block (next-call retry still attempts a fetch).
        from nce.tool_governance import GOVERNANCE

        if await GOVERNANCE.warm(engine.redis_client):
            log.info("Tool governance cache warmed.")
        else:
            log.warning("Tool governance warm failed; will retry on first dispatch.")

        from nce.background_task_manager import create_tracked_task

        gc_task = create_tracked_task(run_gc_loop(), name="gc_loop")
        log.info("GC background task started.")

        from nce import quotas as _quotas

        quota_flush_task = create_tracked_task(
            _quotas.quota_redis_flush_loop(engine.redis_client, engine.pg_pool),
            name="quota_redis_flush_loop",
        )
        log.info("Quota Redis flush background task started.")

        from nce.re_embedder import start_re_embedder

        re_embedder_task = start_re_embedder(engine.pg_pool, engine.mongo_client)
        log.info("Re-embedder background task started.")

        from nce.outbox_relay import run_outbox_relay_once
        from nce.vertical_modules.system_design.subscribers import (
            register_system_design_subscribers,
        )

        # Must run BEFORE the relay task below starts polling. Every
        # <TYPE>.upserted event the System Design authoring cores emit
        # dead-letters if its selector has no subscriber -- and the row is
        # never marked published either, so it also stays in
        # idx_outbox_unpublished, which every tenant's relay poll reads.
        register_system_design_subscribers()

        interval_s = max(1, int(cfg.OUTBOX_RELAY_INTERVAL_SECONDS))

        async def _outbox_relay_loop() -> None:
            while True:
                try:
                    delivered = await run_outbox_relay_once(engine.pg_pool)
                    if delivered:
                        log.debug("Outbox relay delivered %d event(s).", delivered)
                except Exception:
                    log.exception("Outbox relay iteration failed; will retry.")
                await asyncio.sleep(interval_s)

        outbox_relay_task = create_tracked_task(_outbox_relay_loop(), name="outbox_relay_loop")
        log.info("Outbox relay background task started (interval=%ds).", interval_s)

        import signal

        main_task = asyncio.current_task()

        def _handle_signal(sig, frame):
            log.info("Received signal %d; initiating graceful shutdown.", sig)
            if main_task and not main_task.done():
                main_task.get_loop().call_soon_threadsafe(main_task.cancel)

        old_sigterm, old_sigint = None, None
        try:
            old_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
            old_sigint = signal.signal(signal.SIGINT, _handle_signal)
        except ValueError:
            log.warning("Could not register signal handlers (not in main thread).")

        stdio_app = app or create_stdio_app(engine=engine)
        try:
            async with stdio_server(stdout=original_stdout_wrapped) as (read_stream, write_stream):
                log.info("MCP server listening on stdio.")
                await stdio_app.run(
                    read_stream, write_stream, stdio_app.create_initialization_options()
                )
        except asyncio.CancelledError:
            log.info("Server task cancelled (graceful shutdown triggered).")
        finally:
            try:
                if old_sigterm is not None:
                    signal.signal(signal.SIGTERM, old_sigterm)
                if old_sigint is not None:
                    signal.signal(signal.SIGINT, old_sigint)
            except ValueError:
                pass

            # Clean up child processes first to avoid resource leaks
            from nce.subprocess_registry import terminate_all

            terminate_all()

            for task in (gc_task, quota_flush_task, outbox_relay_task, re_embedder_task):
                task.cancel()
            for task in (gc_task, quota_flush_task, outbox_relay_task, re_embedder_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await engine.disconnect()
            log.info("Shutdown complete.")
