"""
nce/vertical_modules/economy/mcp_handlers.py
=============================================
MCP tool handlers for the Economy vertical module (M8.W4: cores-surface).

Public entry-points:
  ``handle_economy_match_invoice``          — invoice-match triage (W1 core).
  ``handle_economy_compute_periodisering``  — NGAAP bucket periodisering (W2 core).
  ``handle_economy_emit_event``             — balance-guarantee dry-run validator (W3 core).

All three are read-only Advisor tools (cacheable=True, admin_only=False,
mutation=False). No new logic — thin wrappers over the W1–W3 pure cores in
``matching.py`` / ``ngaap.py`` / ``events.py``. The emit-event handler is a
DRY-RUN validator: it returns the balance verdict and the normalised/hashed
event but never persists anything (persistence is Wave 6).

Config-as-IP is always loaded via the W1/W2 loaders
(``load_economy_thresholds`` / ``load_finago_chart_of_accounts`` /
``load_finago_account_mapping``) — never accepted from caller-supplied MCP
arguments, so a request can never auto-approve its own invoice or redirect
its own postings (money-module briefing, Batch 116 handoff to B119).

Registered in ``nce/tool_registry.py`` via ``_h(economy_mcp_handlers, "handle_*")``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import McpError, mcp_handler
from nce.vertical_modules.economy._guard import EconomyDisabledError, require_economy_enabled
from nce.vertical_modules.economy.events import UnbalancedPostingsError, do_emit_financial_event
from nce.vertical_modules.economy.matching import do_match_invoice, load_economy_thresholds
from nce.vertical_modules.economy.ngaap import (
    do_compute_bucket_targets,
    load_finago_account_mapping,
    load_finago_chart_of_accounts,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.economy.mcp_handlers")

# ---------------------------------------------------------------------------
# Shared opt-in guard — applied at handler boundary (not inside do_* cores)
# ---------------------------------------------------------------------------

_MCP_ECONOMY_DISABLED_CODE: int = -32005  # MCP_SCOPE_FORBIDDEN


async def _check_economy_enabled(engine: NCEEngine, arguments: dict[str, Any]) -> None:
    """Check namespace opt-in; raise McpError(-32005) if not enabled.

    Also raises the pre-existing ``ValueError("namespace_id is required")``
    (via ``require_namespace_id``) when ``namespace_id`` is absent — callers
    must catch that themselves if they need the pre-existing "missing
    namespace_id" behaviour (see call sites below); this function does not
    swallow it.
    """
    namespace_id = require_namespace_id(arguments)
    try:
        await require_economy_enabled(engine.pg_pool, namespace_id)
    except EconomyDisabledError as exc:
        raise McpError(
            _MCP_ECONOMY_DISABLED_CODE,
            "Economy vertical is not enabled for this namespace",
            data={"reason": "economy_disabled", "detail": str(exc)},
        ) from exc


# Balance tolerance (NOK) for the emit-event dry-run validator. Mirrors the
# call-site default documented in events.py's do_emit_financial_event
# docstring (NCE_ECONOMY_BALANCE_EPSILON, default 0.01) and the same literal
# convention already used by tests/unit/test_economy_financial_event.py.
# This wave adds no config key (B119 orchestrator ruling) — epsilon is never
# caller-supplied, the same rule as thresholds and the chart-of-accounts /
# account-mapping loaders below (money-module briefing #5).
_BALANCE_EPSILON_DEFAULT: float = 0.01

# Keys reserved by this surface's response envelope. Unlike the REST route, the MCP
# result carries no "status" field at all -- the mere *presence* of "error" is the
# sole success/failure signal a caller reads. do_emit_financial_event echoes every
# top-level key of the caller's event into its result by design (events.py:460), so a
# caller-supplied "status" or "error" must be rejected outright rather than allowed
# through to collide with that signal. Checked with an EXACT match (see the call
# site) -- do not lowercase or strip before comparing.
_RESERVED_EVENT_KEYS: frozenset[str] = frozenset({"status", "error"})


@mcp_handler
async def handle_economy_match_invoice(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_match_invoice — invoice-match triage (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        invoice      (dict) — see ``matching.do_match_invoice`` for the exact shape.
    Optional arguments:
        candidates   (list[dict]) — candidate pool; an empty/absent pool still
                     scores header/context components against a synthetic
                     empty candidate (see ``matching.py``).

    Thresholds always come from ``load_economy_thresholds()`` — never from
    caller-supplied arguments, so a request cannot auto-approve its own
    invoice (money-module briefing #5).

    Returns a JSON string: the core's result dict on success, or
    ``{"error": "..."}`` on any validation failure (missing namespace_id,
    malformed invoice/candidates, incoherent thresholds config) — never a
    crash.

    A non-finite value (e.g. a poisoned ``candidate_id``) echoed into the
    result is caught separately from domain validation below: ``allow_nan=False``
    makes ``json.dumps`` raise rather than emit an RFC-8259-invalid bare ``NaN``
    token, and that failure is reported as a serialization problem, never as
    "your invoice is invalid".
    """
    try:
        await _check_economy_enabled(engine, arguments)
        thresholds = load_economy_thresholds()
        invoice: dict[str, Any] = dict(arguments.get("invoice") or {})
        candidates: list[dict[str, Any]] = list(arguments.get("candidates") or [])
        result = do_match_invoice(thresholds, invoice, candidates)
    except McpError:
        # Namespace opt-in refusal — a structured JSON-RPC error, not a
        # domain-validation failure; must propagate to @mcp_handler unchanged
        # rather than be flattened into a returned {"error": ...} string.
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_match_invoice unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error("[economy] handle_economy_match_invoice result not JSON-serializable: %s", exc)
        return json.dumps(
            {
                "error": (
                    f"economy_match_invoice: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_compute_periodisering(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_compute_periodisering — NGAAP bucket periodisering (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        params       (dict) — see ``ngaap.do_compute_bucket_targets`` for the exact
                     shape (``buckets``/``project_id``/``period_end``).

    The chart-of-accounts and account-mapping are always loaded via
    ``load_finago_chart_of_accounts()`` / ``load_finago_account_mapping()`` —
    never from caller-supplied arguments (money-module briefing #5).

    Returns a JSON string: the core's result dict on success (every amount
    serialised via ``default=str`` so ``Decimal`` never round-trips through
    ``float``), or ``{"error": "..."}`` on any validation failure — never a
    crash.

    A non-finite value (e.g. a poisoned ``period_end``) echoed into the
    result is caught separately from domain validation below: ``allow_nan=False``
    makes ``json.dumps`` raise rather than emit an RFC-8259-invalid bare ``NaN``
    token, and that failure is reported as a serialization problem, never as
    "your invoice is invalid".
    """
    try:
        await _check_economy_enabled(engine, arguments)
        chart = load_finago_chart_of_accounts()
        mapping = load_finago_account_mapping()
        params: dict[str, Any] = dict(arguments.get("params") or {})
        result = do_compute_bucket_targets(chart, mapping, params)
    except McpError:
        # See handle_economy_match_invoice: must propagate unchanged.
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_compute_periodisering unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_compute_periodisering result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_compute_periodisering: result contains a non-finite value and "
                    f"cannot be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_emit_event(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_emit_event — balance-guarantee dry-run validator (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        event        (dict) — see ``events.do_emit_financial_event`` for the exact
                     shape (``type``, optional ``postings``).

    This is a DRY-RUN: it validates the event's balance and returns its
    normalised/hashed form but never persists anything (persistence is
    Wave 6). An unbalanced event returns a structured ``{"error": ...}``
    (never a crash, and never auto-balanced/repaired/re-ordered — money-
    module briefing #7).

    Note: this core is already immune to the non-finite-serialization hazard
    described on the other two handlers — ``do_emit_financial_event`` validates
    every leaf (including every posting amount) as finite before it ever
    returns. The ``allow_nan=False`` dump below is defence in depth, not a
    documented exposure.

    An ``event`` that itself carries a reserved envelope key (``status``/
    ``error``, see ``_RESERVED_EVENT_KEYS``) is rejected with a structured
    ``{"error": ...}`` naming the offending key(s), before the core ever
    runs — this surface has no ``status`` field, so a smuggled ``error`` key
    echoed into the result would otherwise make a correctly-balanced success
    indistinguishable from a failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        event: dict[str, Any] = dict(arguments.get("event") or {})
        # Exact match against dict keys -- do NOT lowercase/strip before comparing.
        # "Status"/"status " etc. cannot collide with the literal envelope keys and
        # must stay accepted; normalising here would both reject legitimate keys and
        # reintroduce the validate-one-thing-read-another trap from Batch 116's
        # round-3 defect. Checked before the core runs, so a rejected request does
        # no work.
        reserved_hit = _RESERVED_EVENT_KEYS & event.keys()
        if reserved_hit:
            return json.dumps(
                {"error": f"event contains reserved key(s): {sorted(reserved_hit)}"},
                default=str,
            )
        result = do_emit_financial_event(_BALANCE_EPSILON_DEFAULT, event)
    except McpError:
        # See handle_economy_match_invoice: must propagate unchanged.
        raise
    except UnbalancedPostingsError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "event_type": exc.event_type,
                "diff": exc.diff,
                "tolerance": exc.tolerance,
            },
            default=str,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_emit_event unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error("[economy] handle_economy_emit_event result not JSON-serializable: %s", exc)
        return json.dumps(
            {
                "error": (
                    f"economy_emit_event: result contains a non-finite value and cannot be "
                    f"serialized ({exc})"
                )
            },
            default=str,
        )
