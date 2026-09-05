"""D49b — a deployment-configuration failure must not be reported as the caller's mistake.

Two config keys fail closed when unset, correctly. But both used to raise a bare
``ValueError``, which ``@mcp_handler`` maps to ``-32602 Invalid parameters`` and
``admin_handlers/system_design.py`` maps to ``422``. Both say *"you sent
something wrong."* **No argument the caller can send will set an unset key**, so
a client that routes on status retries forever against a misconfiguration.

What these tests pin
--------------------
1. ``DeploymentConfigurationError`` is **not** a ``ValueError``. That is the one
   constraint the whole wave rests on: ``ValueError`` is the caller-error channel
   on *both* wire surfaces, so subclassing it would fix the MCP surface in
   appearance while leaving the HTTP surface exactly as broken.
2. MCP: the code stays ``-32603`` (a server-side problem really is what this is)
   and is **not** ``-32602``. The negative half is the point — it discriminates
   the new contract from the old rather than merely asserting a raise happened.
   ``data.reason == "deployment_not_configured"`` carries the distinction a new
   error code would otherwise cost every client a branch to learn.
3. ``data`` survives a **plain** ``json.dumps`` with no ``default=``. That is the
   crash path: ``nce/mcp_stdio_rpc.py`` serialises ``error.data`` with no hook,
   so a stray non-primitive raises ``TypeError`` *inside the error-reporting
   path*, turning a refusal into a crash.
4. ``data`` carries the key's **name** and never its **value** — the value may be
   a secret and the payload goes to a caller.
5. **Both directions** on REST. An unset key becomes the server-class status,
   **and a genuinely missing ``design_id`` is still 422**. Fixing one
   misclassification by introducing its mirror image would be the same defect
   pointed the other way: ``sow.py``'s three argument guards are *real* caller
   errors and ``422``/``-32602`` is correct for them.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from nce.config import DeploymentConfigurationError, cfg
from nce.mcp_errors import (
    MCP_INTERNAL_ERROR,
    MCP_INVALID_PARAMS,
    McpError,
    mcp_handler,
)
from nce.vertical_modules.sales.source_adapters import d365
from nce.vertical_modules.system_design.sow import _supplier_name, do_generate_sow

_NS = "11111111-1111-1111-1111-111111111111"


# ── (1) the type itself ──────────────────────────────────────────────────────
def test_the_exception_is_not_a_valueerror() -> None:
    """The single constraint the wave rests on.

    ``admin_handlers/system_design.py`` catches ``ValueError -> 422`` *before*
    its generic handler, and ``@mcp_handler`` catches it as ``-32602``. A
    subclass would leave both surfaces saying "you sent something wrong".
    """
    exc = DeploymentConfigurationError("SOME_KEY", "SOME_KEY is not set")
    assert isinstance(exc, Exception)
    assert not isinstance(exc, ValueError)
    assert not issubclass(DeploymentConfigurationError, ValueError)
    assert exc.config_key == "SOME_KEY"
    assert "SOME_KEY" in str(exc)


# ── (2) the raise sites ──────────────────────────────────────────────────────
def test_unset_d365_prefix_refuses_as_a_deployment_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", "", raising=False)
    with pytest.raises(DeploymentConfigurationError) as exc:
        d365.publisher_prefix()
    assert exc.value.config_key == "NCE_D365_PUBLISHER_PREFIX"
    assert "NCE_D365_PUBLISHER_PREFIX" in str(exc.value)


def test_malformed_d365_prefix_refuses_as_a_deployment_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed prefix is still the operator's key, not the caller's argument."""
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", "Ac'me 1", raising=False)
    with pytest.raises(DeploymentConfigurationError) as exc:
        d365.publisher_prefix()
    assert exc.value.config_key == "NCE_D365_PUBLISHER_PREFIX"


def test_unset_supplier_name_refuses_as_a_deployment_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_SUPPLIER_NAME", "   ", raising=False)
    with pytest.raises(DeploymentConfigurationError) as exc:
        _supplier_name()
    assert exc.value.config_key == "NCE_SUPPLIER_NAME"


# ── (3) the MCP wire contract ────────────────────────────────────────────────
@mcp_handler
async def _handler_unset_prefix(engine: Any, arguments: dict[str, Any]) -> str:
    return d365.publisher_prefix()


@mcp_handler
async def _handler_unset_supplier(engine: Any, arguments: dict[str, Any]) -> str:
    return _supplier_name()


@mcp_handler
async def _handler_missing_argument(engine: Any, arguments: dict[str, Any]) -> str:
    raise ValueError("do_generate_sow: 'design_id' is required in params")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "key", "blank_attr"),
    [
        (_handler_unset_prefix, "NCE_D365_PUBLISHER_PREFIX", "NCE_D365_PUBLISHER_PREFIX"),
        (_handler_unset_supplier, "NCE_SUPPLIER_NAME", "NCE_SUPPLIER_NAME"),
    ],
)
async def test_mcp_reports_a_deployment_fault_as_32603_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    key: str,
    blank_attr: str,
) -> None:
    monkeypatch.setattr(cfg, blank_attr, "", raising=False)
    with pytest.raises(McpError) as exc:
        await handler(None, {})
    err = exc.value
    # The negative half is the discriminator: -32602 is the OLD behaviour.
    assert err.code != MCP_INVALID_PARAMS, "regressed to 'invalid parameters' (-32602)"
    assert err.code == MCP_INTERNAL_ERROR
    assert err.data is not None
    assert err.data["reason"] == "deployment_not_configured"
    assert err.data["config_key"] == key


@pytest.mark.asyncio
async def test_mcp_payload_survives_a_bare_json_dumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nce/mcp_stdio_rpc.py`` has no ``default=`` hook on ``error.data``."""
    monkeypatch.setattr(cfg, "NCE_SUPPLIER_NAME", "", raising=False)
    with pytest.raises(McpError) as exc:
        await _handler_unset_supplier(None, {})
    encoded = json.dumps(exc.value.data)  # deliberately no default=
    round_tripped = json.loads(encoded)
    assert round_tripped == exc.value.data
    for value in round_tripped.values():
        assert isinstance(value, (str, int, float, bool, type(None)))


@pytest.mark.asyncio
async def test_mcp_payload_never_carries_the_config_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the key's NAME. Its value may be a secret and the payload is returned."""
    monkeypatch.setattr(cfg, "NCE_D365_PUBLISHER_PREFIX", "s3cr3t' OR 1=1", raising=False)
    with pytest.raises(McpError) as exc:
        await _handler_unset_prefix(None, {})
    assert "s3cr3t" not in json.dumps(exc.value.data)


@pytest.mark.asyncio
async def test_mcp_still_reports_a_missing_argument_as_32602() -> None:
    """The mirror image, guarded: a real caller mistake must stay ``-32602``."""
    with pytest.raises(McpError) as exc:
        await _handler_missing_argument(None, {})
    assert exc.value.code == MCP_INVALID_PARAMS


# ── (4) the three ``sow.py`` argument guards are UNCHANGED ───────────────────
@pytest.mark.asyncio
async def test_sow_argument_guards_still_raise_plain_valueerror() -> None:
    """``sow.py:723/732/750`` are genuine caller errors; 422/-32602 is correct."""
    with pytest.raises(ValueError) as exc:
        await do_generate_sow(None, {})
    assert not isinstance(exc.value, DeploymentConfigurationError)
    assert "namespace_id" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        await do_generate_sow(None, {"namespace_id": _NS})
    assert not isinstance(exc.value, DeploymentConfigurationError)
    assert "design_id" in str(exc.value)


def test_sow_still_has_exactly_three_valueerror_argument_guards() -> None:
    """Structural pin on the third guard (``:750``), which needs a DB to reach."""
    source = inspect.getsource(do_generate_sow)
    assert source.count("raise ValueError(") == 3, source.count("raise ValueError(")
    assert "DeploymentConfigurationError(" not in source


# ── (5) the REST direction — BOTH ways ───────────────────────────────────────
class _FakeRequest:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


@pytest.mark.asyncio
async def test_rest_returns_the_server_class_status_for_an_unset_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the type stops being a ``ValueError`` the route's generic handler wins."""
    from nce.admin_handlers import system_design as route_mod

    async def _boom(engine: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        raise DeploymentConfigurationError(
            "NCE_SUPPLIER_NAME", "NCE_SUPPLIER_NAME is not configured"
        )

    monkeypatch.setattr(route_mod.admin_state, "engine", object(), raising=False)
    monkeypatch.setattr(route_mod, "do_generate_sow", _boom)
    resp = await route_mod.api_system_design_generate_sow(
        _FakeRequest({"namespace_id": _NS, "design_id": "D-1"})
    )
    assert resp.status_code == 500, "a deployment fault must not read as a caller fault"
    assert resp.status_code != 422


@pytest.mark.asyncio
async def test_rest_still_returns_422_for_a_genuinely_missing_design_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror image, guarded on the HTTP surface too."""
    from nce.admin_handlers import system_design as route_mod

    monkeypatch.setattr(route_mod.admin_state, "engine", object(), raising=False)
    resp = await route_mod.api_system_design_generate_sow(_FakeRequest({"namespace_id": _NS}))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rest_still_returns_422_for_a_valueerror_from_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``except ValueError -> 422`` clause is still live and still first."""
    from nce.admin_handlers import system_design as route_mod

    async def _boom(engine: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("do_generate_sow: 'design_id' is required in params")

    monkeypatch.setattr(route_mod.admin_state, "engine", object(), raising=False)
    monkeypatch.setattr(route_mod, "do_generate_sow", _boom)
    resp = await route_mod.api_system_design_generate_sow(
        _FakeRequest({"namespace_id": _NS, "design_id": "D-1"})
    )
    assert resp.status_code == 422
