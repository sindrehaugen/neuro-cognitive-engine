"""
nce/vertical_modules/inventory/refusals.py
==========================================
ONE shared business-refusal mapping for the Inventory vertical module — debt
item **D38**.

The problem this closes
-----------------------
``InsufficientAvailableError`` (reserve), ``OverReleaseError`` (release),
``RmaNotFoundError`` / ``RmaAlreadySettledError`` / ``RmaNotWeeeScopeError``
(the RMA claim legs) and ``LedgerDivergenceError`` (dead-stock reconcile) are
all bare ``Exception`` subclasses. Neither surface caught them, so all six fell
through to ``MCP_INTERNAL_ERROR`` (-32603) on MCP and ``500`` on REST.

That is not a cosmetic mis-label. *"You tried to reserve more than is
available"* is a refusal the caller **caused and can fix**; delivered as an
internal error it is indistinguishable from *"the backend is down"*, so the
rational client response is to **retry** — which fails identically, forever.

Why one table and not six ``except`` clauses
--------------------------------------------
Per the **D18 precedent**: three instances of one defect class means ONE shared
mapping. This is the third (D3 reports a committed write as an internal error;
D20 is the inverse — a transient fault presented as a permanent argument
fault). So every refusal is declared **here, once**, and each handler carries a
single ``except BUSINESS_REFUSALS`` clause that delegates. Adding a seventh
refusal is a row in :data:`_REFUSALS`, not a new ``except`` on two surfaces.

The wire contract — option (b), one code plus a machine-readable ``reason``
--------------------------------------------------------------------------
``FE_UPDATE_2026-08-31_ADDENDUM.md`` asked the FE to choose between (a) a
distinct code per refusal and (b) one shared business-refusal code carrying a
machine-readable ``reason``, as the namespace opt-in gate does on the private
repo (``McpError(-32005)`` with ``data={"reason": "inventory_disabled"}``).
The addendum was unanswered when this landed, and the standing default is
**(b)** — it extends without a new code per future refusal.

NOTE for this repo: that opt-in gate (B140a) is **not ported here**, so the
``inventory_disabled`` precedent it cites lives on the private tree. The shape
is unchanged either way, and ``409`` is already this module's status for
``InsufficientStockError``, which is the local precedent.

So:

* **MCP** — ``McpError(-32005)`` with ``data={"reason": <slug>, ...fields}``.
* **REST** — ``409``, the status this module already returns for
  ``InsufficientStockError``.

Note ``-32005`` is spelled ``MCP_SCOPE_FORBIDDEN`` in ``nce/mcp_errors.py``.
Reusing it for "not enough available stock" is a **semantic stretch**, accepted
here deliberately: it introduces no new contract element the FE has not already
been shown, and ``409`` is already this module's status for a business-rule
refusal that is not a scope failure. If the FE would rather have a
dedicated business-refusal code, that is a one-line change to
:data:`MCP_BUSINESS_REFUSED` plus a doc update — the ``reason`` slugs do not
move.

**Known remaining asymmetry, deliberately NOT widened here.**
``InsufficientStockError`` — the nearest sibling of these six — is delivered on
MCP as a *successful tool result* whose JSON body carries an ``error`` key
(``mcp_handlers._insufficient_stock_error``), not as an ``McpError``. So after
this wave ``insufficient_stock`` still arrives on a different channel than
``insufficient_available``. Unifying it is an FE-visible **breaking** change to
a surface that already shipped, so it needs the FE's sign-off and is recorded
as following this wave, not folded into it. The plan scoped this wave to the
six uncaught refusals and said not to widen into D3/D20; the same discipline
applies here.

Why the payload is coerced here rather than at each surface
-----------------------------------------------------------
``nce/mcp_stdio_rpc.py`` emits ``error.data`` through a **plain**
``json.dumps`` with no ``default=str`` hook. A ``UUID`` or ``Decimal`` left in
the payload would raise ``TypeError`` *inside the error-reporting path* — a
refusal would become a crash, which is the very defect class this module
exists to remove. :func:`refusal_payload` therefore returns JSON primitives
only. (The REST surface's ``_json_safe`` round-trip stays harmless and
idempotent on an already-coerced payload.)

``Decimal`` becomes its **exact string form**, never a ``float``: these are
money and ``NUMERIC(18,3)`` stock quantities, and coercing them through
``float`` is forbidden (money-module briefing #2; see ``inventory/stock.py``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.vertical_modules.inventory.reconcile import LedgerDivergenceError
from nce.vertical_modules.inventory.reservation import (
    InsufficientAvailableError,
    OverReleaseError,
)
from nce.vertical_modules.inventory.rma import (
    RmaAlreadySettledError,
    RmaNotFoundError,
    RmaNotWeeeScopeError,
)

#: The single MCP code every Inventory business refusal carries. Option (b) of
#: the FE addendum. See the module docstring on why this is ``-32005``.
MCP_BUSINESS_REFUSED: int = MCP_SCOPE_FORBIDDEN

#: The single REST status every Inventory business refusal carries -- the one
#: this module already uses for ``InsufficientStockError``.
REST_BUSINESS_REFUSED_STATUS: int = 409

#: refusal class -> (``reason`` slug, the attributes that carry its structured
#: detail). The attribute tuples are the classes' OWN public attributes, so a
#: caller never has to re-parse ``str(exc)``.
_REFUSALS: Mapping[type[Exception], tuple[str, tuple[str, ...]]] = {
    InsufficientAvailableError: (
        "insufficient_available",
        (
            "sku",
            "location_id",
            "project_id",
            "requested",
            "on_hand",
            "reserved",
            "blocked",
            "available",
        ),
    ),
    OverReleaseError: (
        "over_release",
        ("sku", "location_id", "project_id", "requested", "currently_reserved"),
    ),
    RmaNotFoundError: ("rma_not_found", ("rma_ref",)),
    RmaAlreadySettledError: (
        "rma_already_settled",
        ("rma_ref", "stock_movement_state"),
    ),
    RmaNotWeeeScopeError: ("rma_not_weee_scope", ("rma_ref",)),
    LedgerDivergenceError: ("ledger_divergence", ("pairs",)),
}

#: Catch-tuple for the handlers. ``except BUSINESS_REFUSALS`` is the ONLY
#: refusal clause either surface should carry for this module.
BUSINESS_REFUSALS: tuple[type[Exception], ...] = tuple(_REFUSALS)

#: Every ``reason`` slug this module can emit -- for tests and for the docs
#: table, so neither has to re-derive it from the mapping.
REFUSAL_REASONS: Mapping[type[Exception], str] = {
    exc_type: reason for exc_type, (reason, _fields) in _REFUSALS.items()
}


def _neutralise_non_finite(value: Any) -> Any:
    """Replace non-finite floats with their string form, recursively.

    A caller-echoed ``NaN``/``Infinity`` must never reach an encoder with
    ``allow_nan=False``, where it would be mis-filed as a *validation* failure
    -- the D20 defect shape (a transient or odd input presented as the wrong
    error class). Mirrors ``admin_handlers/_shared.py``'s helper of the same
    name.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _neutralise_non_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neutralise_non_finite(item) for item in value]
    return value


def _json_primitives(value: Any) -> Any:
    """Round-trip *value* into JSON primitives, ``Decimal`` -> exact string."""
    return json.loads(json.dumps(_neutralise_non_finite(value), default=str))


def is_business_refusal(exc: BaseException) -> bool:
    """True when *exc* is one of this module's declared business refusals."""
    return isinstance(exc, BUSINESS_REFUSALS)


def refusal_reason(exc: BaseException) -> str:
    """The machine-readable ``reason`` slug for *exc*.

    Exact type first, then ``isinstance``, so a future subclass of a declared
    refusal still resolves rather than raising.

    Raises ``KeyError`` for anything not declared in :data:`_REFUSALS` -- a
    caller reaching here with an undeclared exception is a bug in the caller's
    ``except`` clause, and must not be papered over with a generic slug that
    would tell the FE "this is a refusal you caused" about a server fault.
    """
    for exc_type, (reason, _fields) in _REFUSALS.items():
        if type(exc) is exc_type:
            return reason
    for exc_type, (reason, _fields) in _REFUSALS.items():
        if isinstance(exc, exc_type):
            return reason
    raise KeyError(f"{type(exc).__name__} is not a declared Inventory business refusal")


def _fields_for(reason: str) -> tuple[str, ...]:
    for _exc_type, (candidate, candidate_fields) in _REFUSALS.items():
        if candidate == reason:
            return candidate_fields
    return ()


def refusal_payload(exc: BaseException) -> dict[str, Any]:
    """The full machine-readable body for *exc* -- JSON primitives only.

    Always carries ``error`` (the human-readable message) and ``reason`` (the
    stable slug). Every remaining key is one of the exception's own declared
    attributes. An attribute that is absent is omitted rather than sent as
    ``null``, so a payload never asserts a field the core did not set.
    """
    reason = refusal_reason(exc)
    payload: dict[str, Any] = {"error": str(exc), "reason": reason}
    for name in _fields_for(reason):
        if hasattr(exc, name):
            payload[name] = getattr(exc, name)
    return _json_primitives(payload)


def mcp_refusal(exc: BaseException) -> McpError:
    """Build the ``McpError`` for *exc*. Raise the result; never return it."""
    payload = refusal_payload(exc)
    return McpError(MCP_BUSINESS_REFUSED, payload["error"], data=payload)
