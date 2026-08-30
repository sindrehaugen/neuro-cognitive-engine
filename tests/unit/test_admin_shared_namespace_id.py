"""Contract tests for ``admin_handlers/_shared.py``'s ``_require_namespace_id``.

Why this file exists
--------------------
``_require_namespace_id`` was triplicated: ``assets.py`` and ``inventory.py``
each carried a byte-identical private copy, and ``economy.py`` inlined the same
logic three times. The copies were consolidated into ``_shared.py`` (assets and
inventory in PR #110, economy here).

The pre-existing surface tests assert only ``status_code == 422`` (or
``400 <= sc < 500``) plus ``"error" in body`` — never the message. Verified by
mutation: swapping both message strings on all four modules left 137 of those
tests green. So the "one helper, identical 422 body on every surface" property
the consolidation is *for* was completely ungated.

These tests gate it two ways:
  1. the helper's own input/output contract, message text included; and
  2. object identity — every admin handler resolves to the *same* function, so
     a module silently reintroducing a local copy fails here rather than
     drifting undetected (a naming convention is not a boundary).

Deliberately NOT covered: the handlers answering a different dialect —
``"Missing required query param: namespace_id"`` (``product.py``,
``vendors.py``) and ``"Missing namespace_id"`` (``agreements.py``). They have
different response bodies and are not folded onto this helper.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers._shared import _require_namespace_id

_VALID_NS = "11111111-2222-3333-4444-555555555555"
_MALFORMED = ["x", "not-a-uuid", "12345678-1234-1234-1234-12345678901"]

_MISSING_BODY = {"error": "Missing required field: namespace_id"}


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock (mirrors test_assets_surface.py)."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. The helper's own contract — exact bodies, not just status codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_missing_namespace_id_exact_422_body(raw: str | None) -> None:
    """Absent/blank input -> (None, 422) carrying the exact documented body."""
    namespace_id, err = _require_namespace_id(raw)
    assert namespace_id is None
    assert err is not None
    assert err.status_code == 422
    assert json.loads(err.body) == _MISSING_BODY


@pytest.mark.parametrize("raw", _MALFORMED)
def test_malformed_namespace_id_exact_422_body(raw: str) -> None:
    """Non-UUID input -> (None, 422) with the ``Invalid namespace_id: `` prefix.

    The suffix is ``ValueError``'s own text, which is a stdlib detail, so only
    the prefix is pinned — but it must not collapse into the *missing* message.
    """
    namespace_id, err = _require_namespace_id(raw)
    assert namespace_id is None
    assert err is not None
    assert err.status_code == 422
    body = json.loads(err.body)
    assert set(body) == {"error"}
    assert body["error"].startswith("Invalid namespace_id: ")
    assert body != _MISSING_BODY


def test_valid_namespace_id_returns_value_and_no_error() -> None:
    namespace_id, err = _require_namespace_id(_VALID_NS)
    assert namespace_id == _VALID_NS
    assert err is None


def test_valid_namespace_id_is_stripped() -> None:
    """Surrounding whitespace is removed before the UUID parse."""
    namespace_id, err = _require_namespace_id(f"  {_VALID_NS}\n")
    assert namespace_id == _VALID_NS
    assert err is None


def test_over_length_input_is_rejected_not_silently_truncated() -> None:
    """``validate_agent_id`` truncates to 128 chars and never raises, so the
    explicit ``uuid.UUID(...)`` parse is the only thing standing between a
    long garbage string and asyncpg's ``::uuid`` cast. Guards the reason the
    helper does not stop at ``validate_agent_id``.
    """
    namespace_id, err = _require_namespace_id("z" * 400)
    assert namespace_id is None
    assert err is not None
    assert err.status_code == 422


# ---------------------------------------------------------------------------
# 2. One helper, not three — resolved by object identity
# ---------------------------------------------------------------------------


def test_all_folded_modules_share_one_helper_object() -> None:
    """assets/inventory/economy must resolve to the *same* function object.

    A module reintroducing its own copy would still pass every status-code
    test in the suite; this is the check that fails instead.
    """
    from nce.admin_handlers import _shared, assets, economy, inventory

    for module in (assets, inventory, economy):
        assert module._require_namespace_id is _shared._require_namespace_id, (
            f"{module.__name__} no longer shares _shared._require_namespace_id"
        )


def test_folded_modules_do_not_reimport_validate_agent_id() -> None:
    """The fold removed each module's own ``validate_agent_id`` import.

    If one comes back it is a signal that inline validation was reintroduced
    alongside the shared call.
    """
    from nce.admin_handlers import assets, economy, inventory

    for module in (assets, inventory, economy):
        assert not hasattr(module, "validate_agent_id"), (
            f"{module.__name__} re-imported validate_agent_id"
        )


# ---------------------------------------------------------------------------
# 3. Cross-surface: every folded route returns the identical 422 body
# ---------------------------------------------------------------------------


async def _economy_responses(payload: dict[str, Any]) -> list[Any]:
    from nce.admin_handlers.economy import (
        api_economy_emit_event,
        api_economy_match_invoice,
        api_economy_periodisering,
    )

    out = []
    for route in (api_economy_match_invoice, api_economy_periodisering, api_economy_emit_event):
        with patch("nce.admin_handlers.economy.admin_state") as mock_state:
            mock_state.engine = MagicMock()
            out.append(await route(_make_request(body=payload)))
    return out


async def _assets_responses(ns: str | None) -> list[Any]:
    from nce import admin_state
    from nce.admin_handlers.assets import (
        api_assets_advance_lifecycle,
        api_assets_get,
        api_assets_list,
    )

    query = {} if ns is None else {"namespace_id": ns}
    body = {} if ns is None else {"namespace_id": ns}
    with patch.object(admin_state, "engine", MagicMock()):
        return [
            await api_assets_get(_make_request(path_params={"id": _VALID_NS}, query=query)),
            await api_assets_list(_make_request(query=query)),
            await api_assets_advance_lifecycle(
                _make_request(path_params={"id": _VALID_NS}, body=body)
            ),
        ]


async def _inventory_responses(ns: str | None) -> list[Any]:
    from nce import admin_state
    from nce.admin_handlers.inventory import (
        api_inventory_record_consumption,
        api_inventory_stock_levels,
        api_inventory_transfer_stock,
    )

    query = {} if ns is None else {"namespace_id": ns}
    body = {} if ns is None else {"namespace_id": ns}
    with patch.object(admin_state, "engine", MagicMock()):
        return [
            await api_inventory_stock_levels(_make_request(query=query)),
            await api_inventory_transfer_stock(_make_request(body=body)),
            await api_inventory_record_consumption(_make_request(body=body)),
        ]


@pytest.mark.asyncio
async def test_missing_namespace_id_body_identical_across_all_nine_routes() -> None:
    """The point of the extraction: one body, every surface."""
    responses = (
        await _economy_responses({})
        + await _assets_responses(None)
        + await _inventory_responses(None)
    )
    assert len(responses) == 9
    for resp in responses:
        assert resp.status_code == 422
        assert json.loads(resp.body) == _MISSING_BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED)
async def test_malformed_namespace_id_body_identical_across_all_nine_routes(bad_ns: str) -> None:
    """A malformed namespace_id is refused pre-gate with one shared body, and
    never reaches asyncpg's ``::uuid`` cast (the DataError-escape defect class).
    """
    responses = (
        await _economy_responses({"namespace_id": bad_ns})
        + await _assets_responses(bad_ns)
        + await _inventory_responses(bad_ns)
    )
    assert len(responses) == 9
    bodies = set()
    for resp in responses:
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert set(body) == {"error"}
        assert body["error"].startswith("Invalid namespace_id: ")
        bodies.add(body["error"])
    assert len(bodies) == 1, f"surfaces disagree on the malformed body: {bodies}"
