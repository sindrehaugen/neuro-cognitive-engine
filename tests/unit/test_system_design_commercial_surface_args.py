"""Argument validation for the M6.W26 commercial surface (Batch 230a).

Pure: no DB, no Redis, no HTTP. Each handler's guards run before anything
touches ``engine.pg_pool``, so a stub engine is enough to exercise them.

🔴 Why each case has a THIRD assertion.
The natural shape here -- call with an argument missing, assert
``McpError(-32602)`` -- is confounded by the stub. ``_EngineStub(None)`` has a
null pool, so *every* path through these handlers raises something, and a test
that only asks "did it raise the right code" cannot tell a working guard from a
handler that never reached one. That is the B067e defect exactly: a stub whose
dependency is absent turns the unit into an error-generator.

So every tool below is also called with COMPLETE arguments, and the test asserts
the failure is *not* ``MCP_INVALID_PARAMS``. That is what proves the two missing
-argument cases were decided by the guard rather than by the stub.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_enrich_design_lines,
    handle_system_design_from_quote,
    handle_system_design_generate_sow,
    handle_system_design_to_quote,
)


class _EngineStub:
    """Engine with no pool: reaching the database is a failure of the guard."""

    def __init__(self) -> None:
        self.pg_pool = None


#: handler -> (second required argument, a complete argument set)
_CASES: dict[Any, tuple[str, dict[str, Any]]] = {
    handle_system_design_from_quote: ("quote_id", {"quote_id": "QUOTE-1"}),
    handle_system_design_to_quote: ("design_id", {"design_id": "DESIGN-1"}),
    handle_system_design_generate_sow: ("design_id", {"design_id": "DESIGN-1"}),
    handle_system_design_enrich_design_lines: ("design_id", {"design_id": "DESIGN-1"}),
}

_IDS = [h.__name__.removeprefix("handle_") for h in _CASES]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", list(_CASES), ids=_IDS)
async def test_missing_namespace_id_is_invalid_params(handler: Any) -> None:
    _second, complete = _CASES[handler]
    with pytest.raises(McpError) as exc_info:
        await handler(_EngineStub(), dict(complete))
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", list(_CASES), ids=_IDS)
async def test_missing_second_required_argument_is_invalid_params(handler: Any) -> None:
    with pytest.raises(McpError) as exc_info:
        await handler(_EngineStub(), {"namespace_id": str(uuid.uuid4())})
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", list(_CASES), ids=_IDS)
async def test_complete_arguments_get_past_the_guard(handler: Any) -> None:
    """The discriminator: with everything supplied, the guard must NOT fire.

    Without this, the two tests above would pass against a handler that raised
    invalid-params unconditionally -- or against one whose guard was deleted,
    since the null pool would raise anyway.
    """
    _second, complete = _CASES[handler]
    args = {"namespace_id": str(uuid.uuid4()), **complete}

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - any failure but that one
        await handler(_EngineStub(), args)

    code = getattr(exc_info.value, "code", None)
    assert code != MCP_INVALID_PARAMS, (
        f"{handler.__name__} reported invalid-params with every required argument "
        "present, so the two missing-argument tests above prove nothing about its "
        "guards -- they would pass for a handler that always rejects."
    )


@pytest.mark.asyncio
async def test_blank_namespace_id_is_rejected_too() -> None:
    """An empty string is a missing argument, not a namespace."""
    with pytest.raises(McpError) as exc_info:
        await handle_system_design_from_quote(
            _EngineStub(), {"namespace_id": "", "quote_id": "QUOTE-1"}
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS
