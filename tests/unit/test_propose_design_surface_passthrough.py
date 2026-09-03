"""``system_design_propose_design`` must not reshape what the core returns.

Why this test exists, specifically
----------------------------------
``do_propose_design`` is the one core in the M6.W26 group that was NOT orphaned.
It has two internal callers:

    nce/vertical_modules/sales/commission.py:189
    nce/vertical_modules/system_design/from_quote.py:231

and commission embeds the result **whole**::

    "system_design_proposal": sd_proposal

So the load-bearing property is not any single field -- it is that the returned
object arrives intact. A wrapper that renamed a key, dropped one, or nested the
payload under a "result" envelope would break sales commission silently, with no
type error and no failing call: the proposal would simply stop containing what
commission's consumers read.

This pins the pass-through itself rather than a field list, because a field list
would have to be kept in sync with the core and would go stale the first time the
core gained a key.

RED-first, demonstrated: inserting any reshaping into the handler -- an envelope,
a renamed key, a dropped key -- fails ``test_handler_returns_the_core_payload_verbatim``.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_propose_design,
)


class _EngineStub:
    def __init__(self) -> None:
        self.pg_pool = None


#: A payload shaped like the core's documented return, including the invariant
#: every line carries. Values are arbitrary; their SURVIVAL is the assertion.
_CORE_PAYLOAD: dict[str, Any] = {
    "proposed_lines": [
        {
            "product_ref": "ACME-1234",
            "qty": 2,
            "confidence": 0.87,
            "validated": False,
            "recall_memory_id": "11111111-1111-1111-1111-111111111111",
        }
    ],
    "recalled": 1,
    "room_brief": "functional location: FL-A",
}


@pytest.mark.asyncio
async def test_handler_returns_the_core_payload_verbatim() -> None:
    """The adapter adds nothing and subtracts nothing.

    The mock returns a DEEP COPY, and the comparison is against the pristine
    module-level constant. Handing the handler the constant itself made this
    test self-confirming: a handler that mutated the payload IN PLACE -- popping
    a key from each line, say -- changed both sides of the comparison together
    and passed. Found by mutating the handler and watching only the sibling test
    go red.
    """
    args = {"namespace_id": str(uuid.uuid4()), "room_brief": "a room"}

    with patch(
        "nce.vertical_modules.system_design.mcp_handlers.do_propose_design",
        new=AsyncMock(return_value=deepcopy(_CORE_PAYLOAD)),
    ):
        raw = await handle_system_design_propose_design(_EngineStub(), args)

    assert json.loads(raw) == _CORE_PAYLOAD, (
        "the handler reshaped the core's payload. sales/commission.py embeds this "
        "object whole under 'system_design_proposal', so any envelope, rename or "
        "dropped key breaks commission silently."
    )


@pytest.mark.asyncio
async def test_propose_only_invariant_survives_the_adapter() -> None:
    """``validated`` is False on every line, and the adapter must not touch it.

    The core's docstring calls this PROPOSE-ONLY: it never auto-accepts, freezes
    or applies a line. A wrapper that defaulted, coerced or stripped ``validated``
    would turn a proposal into something a downstream caller could mistake for an
    accepted line.
    """
    args = {"namespace_id": str(uuid.uuid4()), "room_brief": "a room"}

    with patch(
        "nce.vertical_modules.system_design.mcp_handlers.do_propose_design",
        new=AsyncMock(return_value=deepcopy(_CORE_PAYLOAD)),
    ):
        payload = json.loads(await handle_system_design_propose_design(_EngineStub(), args))

    assert payload["proposed_lines"], "fixture lost its lines; the test would be vacuous"
    for line in payload["proposed_lines"]:
        assert line["validated"] is False


@pytest.mark.asyncio
async def test_the_two_internal_call_sites_still_pass_only_documented_keys() -> None:
    """Guard the guard: the adapter must forward arguments, not rewrite them.

    If the handler injected or renamed a params key, the core would receive
    something its two existing callers never send, and this surface would be
    changing the contract rather than exposing it.
    """
    args = {"namespace_id": str(uuid.uuid4()), "room_brief": "a room"}
    spy = AsyncMock(return_value=deepcopy(_CORE_PAYLOAD))

    with patch("nce.vertical_modules.system_design.mcp_handlers.do_propose_design", new=spy):
        await handle_system_design_propose_design(_EngineStub(), dict(args))

    spy.assert_awaited_once()
    forwarded = spy.await_args.args[1]
    assert forwarded["namespace_id"] == args["namespace_id"]
    assert forwarded["room_brief"] == args["room_brief"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_args",
    [
        pytest.param({"room_brief": "a room"}, id="missing_namespace_id"),
        pytest.param({"namespace_id": str(uuid.uuid4())}, id="missing_room_brief"),
        pytest.param({"namespace_id": "", "room_brief": "a room"}, id="blank_namespace_id"),
    ],
)
async def test_missing_required_arguments_are_invalid_params(bad_args: dict[str, Any]) -> None:
    with pytest.raises(McpError) as exc_info:
        await handle_system_design_propose_design(_EngineStub(), bad_args)
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
async def test_complete_arguments_get_past_the_guard() -> None:
    """The discriminator for the case above -- see the B067e note in the sibling file.

    With a null-pool stub every path raises, so "missing arg -> invalid params"
    proves nothing unless a complete call is shown NOT to produce that code.
    """
    args = {"namespace_id": str(uuid.uuid4()), "room_brief": "a room"}
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - anything but invalid-params
        await handle_system_design_propose_design(_EngineStub(), args)
    assert getattr(exc_info.value, "code", None) != MCP_INVALID_PARAMS
