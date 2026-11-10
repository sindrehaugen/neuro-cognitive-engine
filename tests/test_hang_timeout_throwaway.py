import asyncio

import pytest

from nce.plugins import get_plugin_metrics_snapshot, record_plugin_tool_execution


@pytest.mark.asyncio
async def test_hang_timeout_cancellation_and_metrics_boundary():
    """Verify asyncio.wait_for cancellation semantics on hung coroutine and metrics recording."""

    async def hung_worker():
        await asyncio.sleep(10.0)
        return "unreachable"

    # 1. Real deterministic timeout boundary (<10ms)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(hung_worker(), timeout=0.005)

    # 2. Record plugin tool execution metrics on timeout error
    record_plugin_tool_execution(
        plugin_id="test_runner",
        engine_slug="hang_detector",
        tool_name="hung_worker",
        duration_s=0.005,
        error=True,
    )

    # 3. Verify metrics snapshot reflects the failure
    snapshot = get_plugin_metrics_snapshot()
    key = "test_runner:hang_detector:hung_worker"
    assert key in snapshot
    assert snapshot[key]["invocations"] >= 1
    assert snapshot[key]["errors"] >= 1
    assert snapshot[key]["total_duration_s"] >= 0.005
