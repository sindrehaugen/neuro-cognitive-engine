"""D49a — an ownership denial must arrive as a *stated refusal*.

``assert_owner`` is the Contract-A guard on every guarded write in the estate.
Before this contract landed, ``@mcp_handler`` never named ``OwnershipError``, so
every denial fell to the generic branch and reached the client as
``-32603 Internal error`` — indistinguishable from "the backend is down", which
makes the rational client response *retry*, forever.

What these tests pin
--------------------
1. The code is ``-32005`` (``MCP_SCOPE_FORBIDDEN``) and **not** ``-32603``. The
   negative half is the point: it discriminates the new contract from the old
   one rather than merely asserting that *some* refusal happened.
2. ``data.reason == "ownership_denied"`` — the same slug the REST surface
   already returns (``admin_handlers/system_design.py::_ownership_denied_response``).
3. ``data`` survives a **plain** ``json.dumps`` with no ``default``. That is the
   crash path: ``nce/mcp_stdio_rpc.py`` serialises ``error.data`` with no
   ``default=str`` hook, so a stray ``UUID``/``Decimal`` would raise
   ``TypeError`` inside the error-reporting path.
4. ``data`` leaks neither the namespace id nor the registry's ``owner_engine``
   row content — a refusal payload goes to a caller who may not be entitled to
   either.
"""

from __future__ import annotations

import json
import uuid

import pytest

from nce.entity_resolution.ownership import OwnershipError
from nce.mcp_errors import (
    MCP_INTERNAL_ERROR,
    MCP_SCOPE_FORBIDDEN,
    McpError,
    mcp_handler,
)

_NODE_TYPE = "system_design_node"
_WRITER = "sales"
_OWNER = "procurement"  # distinct substring from node_type/writer, so the leak check bites
_TRANSITION = "APPROVED"


@mcp_handler
async def _guarded_write(transition: str | None = None) -> str:
    """Stand-in for any handler whose write is refused by ``assert_owner``."""
    raise OwnershipError(
        node_type=_NODE_TYPE,
        writer_engine=_WRITER,
        owner_engine=_OWNER,
        transition=transition,
    )


async def _refusal(transition: str | None = None) -> McpError:
    with pytest.raises(McpError) as excinfo:
        await _guarded_write(transition)
    return excinfo.value


@pytest.mark.asyncio
async def test_ownership_denial_is_minus_32005_not_minus_32603() -> None:
    err = await _refusal(_TRANSITION)
    assert err.code == MCP_SCOPE_FORBIDDEN
    assert err.code == -32005
    assert err.code != MCP_INTERNAL_ERROR, "regressed to the pre-D49a internal-error branch"


@pytest.mark.asyncio
async def test_payload_carries_the_reason_slug_and_the_three_actionable_fields() -> None:
    err = await _refusal(_TRANSITION)
    assert err.data is not None
    assert err.data["reason"] == "ownership_denied"
    assert err.data["node_type"] == _NODE_TYPE
    assert err.data["writer_engine"] == _WRITER
    assert err.data["transition"] == _TRANSITION


@pytest.mark.asyncio
async def test_transition_is_null_not_absent_for_a_node_type_wide_denial() -> None:
    err = await _refusal(None)
    assert err.data is not None
    assert err.data["transition"] is None
    assert err.data["reason"] == "ownership_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", [_TRANSITION, None])
async def test_payload_is_serialisable_by_a_plain_json_dumps(transition: str | None) -> None:
    """The crash path: ``mcp_stdio_rpc`` dumps ``error.data`` with no ``default``."""
    err = await _refusal(transition)
    encoded = json.dumps(err.data)  # no default= — deliberately
    assert json.loads(encoded) == err.data


@pytest.mark.asyncio
async def test_payload_leaks_neither_namespace_nor_registry_row_content() -> None:
    err = await _refusal(_TRANSITION)
    assert err.data is not None
    assert "owner_engine" not in err.data
    encoded = json.dumps(err.data)
    assert _OWNER not in encoded
    assert "namespace" not in encoded
    assert str(uuid.UUID(int=0)) not in encoded
    assert set(err.data) == {"reason", "node_type", "writer_engine", "transition"}


@pytest.mark.asyncio
async def test_successful_handler_still_passes_through() -> None:
    @mcp_handler
    async def _ok() -> str:
        return "fine"

    assert await _ok() == "fine"
