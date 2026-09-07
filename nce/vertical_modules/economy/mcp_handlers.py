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
import uuid
from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import McpError, mcp_handler
from nce.vertical_modules.economy._guard import EconomyDisabledError, require_economy_enabled
from nce.vertical_modules.economy.cascade import do_cascade_on_approval
from nce.vertical_modules.economy.close_narrative import do_generate_close_narrative
from nce.vertical_modules.economy.contracts import do_validate_contract
from nce.vertical_modules.economy.dunning import do_compute_dunning
from nce.vertical_modules.economy.events import UnbalancedPostingsError, do_emit_financial_event
from nce.vertical_modules.economy.finago import do_gl_sync_status
from nce.vertical_modules.economy.forecast import do_forecast_cashflow
from nce.vertical_modules.economy.gl import do_get_gl_records
from nce.vertical_modules.economy.matching import do_match_invoice, load_economy_thresholds
from nce.vertical_modules.economy.ngaap import (
    do_compute_bucket_targets,
    load_finago_account_mapping,
    load_finago_chart_of_accounts,
)
from nce.vertical_modules.economy.peppol import (
    do_generate_ehf,
    do_generate_kid,
    do_validate_kid,
)
from nce.vertical_modules.economy.recurring import (
    do_compute_recognition_schedule,
    do_snapshot_mrr_arr_churn,
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


@mcp_handler
async def handle_economy_forecast_cashflow(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_forecast_cashflow — Monte Carlo cashflow forecast (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        seed         (int) — deterministic seed (required, must not be None/bool).
        params       (dict) — forecast params with 'periods' (list of dicts).
                     Can also pass 'periods', 'iterations', 'opening_balance' at top level.

    Returns a JSON string: simulation results (P10/P50/P90 net and balance),
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        seed = arguments.get("seed")
        if "params" in arguments and isinstance(arguments["params"], dict):
            params = dict(arguments["params"])
        else:
            params = {k: v for k, v in arguments.items() if k not in ("namespace_id", "seed")}
        result = do_forecast_cashflow(seed, params)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_forecast_cashflow unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_forecast_cashflow result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_forecast_cashflow: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_snapshot_mrr_arr_churn(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: economy_snapshot_mrr_arr_churn — MRR/ARR/churn snapshot (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        contracts    (list[dict]) — list of contract dicts with annual_amount and status.
                     May also be passed inside params: {"contracts": [...]}.

    Returns a JSON string: snapshot dict with mrr, arr, churned_mrr, churn_rate, counts,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        if "params" in arguments and isinstance(arguments["params"], dict):
            params = dict(arguments["params"])
        else:
            params = {"contracts": arguments.get("contracts")}
        result = do_snapshot_mrr_arr_churn(params)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_snapshot_mrr_arr_churn unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_snapshot_mrr_arr_churn result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_snapshot_mrr_arr_churn: result contains a non-finite value and "
                    f"cannot be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_compute_dunning(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_compute_dunning — Norwegian dunning / credit policy (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        customer     (dict) — with 'credit_risk_score' (0-100) and optional 'customer_id'.
                     May also be passed as top-level 'credit_risk_score' and 'customer_id'.

    Returns a JSON string: tier, reminder_days, hw_signing_required, lindorff_handoff, reasons,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        if "customer" in arguments and isinstance(arguments["customer"], dict):
            customer = dict(arguments["customer"])
        else:
            customer = {k: v for k, v in arguments.items() if k != "namespace_id"}
        result = do_compute_dunning(customer)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_compute_dunning unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_compute_dunning result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_compute_dunning: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_compute_recognition_schedule(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: economy_compute_recognition_schedule — 12-month ratable recognition schedule (READ-ONLY Advisor).

    Required arguments:
        namespace_id  (str, UUID)
        contract_id   (str) — contract identifier
        annual_amount (number) — annual recurring revenue (> 0)
        start_period  (str) — 'YYYY-MM'
        (Arguments may be passed at top-level or nested under 'params'.)

    Returns a JSON string: 12-period ratable schedule with finago_ref and exact amounts,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        if "params" in arguments and isinstance(arguments["params"], dict):
            params = dict(arguments["params"])
        else:
            params = {k: v for k, v in arguments.items() if k != "namespace_id"}
        result = do_compute_recognition_schedule(params)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_compute_recognition_schedule unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_compute_recognition_schedule result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_compute_recognition_schedule: result contains a non-finite value and "
                    f"cannot be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_gl_sync_status(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_gl_sync_status — GL reconciliation sync status (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        window_hours (float, default 24.0)

    Returns a JSON string: divergence counts, last_divergence_at, clean status,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        ns_uuid = require_namespace_id(arguments)
        params: dict[str, Any] = {"namespace_id": ns_uuid}
        if "window_hours" in arguments:
            params["window_hours"] = arguments["window_hours"]
        result = await do_gl_sync_status(engine, params)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_gl_sync_status unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_gl_sync_status result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_gl_sync_status: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_generate_close_narrative(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: economy_generate_close_narrative — C9a grounded period-close narrative (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        period_id    (str) — e.g. '2026-08'

    Returns a JSON string: period_id, prose, citations, dropped claims,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        ns_uuid = require_namespace_id(arguments)
        period_id = arguments.get("period_id")
        if not period_id:
            raise ValueError("period_id is required")
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            result = await do_generate_close_narrative(
                conn, namespace_id=ns_uuid, period_id=str(period_id)
            )
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_generate_close_narrative unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_generate_close_narrative result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_generate_close_narrative: result contains a non-finite value and "
                    f"cannot be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_approve_invoice(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_approve_invoice — execute 7-effect invoice approval cascade (Governed Actor tool).

    Required arguments:
        namespace_id (str, UUID)
        approval_id  (str) — idempotency key for this approval execution
        quote_id     (str) — Sales quote identifier (actual_cost aggregation root)

    Optional arguments:
        project_id       (str) — optional project identifier echoed into effects
        invoice_id       (str) — optional invoice identifier echoed into effects
        supplier_id      (str) — optional supplier identifier echoed into effects
        invoice_amount   (int|float|Decimal) — optional invoice total amount
        invoice_postings (list[dict]) — optional invoice-level GL postings
        lines            (list[dict]) — list of matched BOM line actual cost records:
                            [{"bom_line_label": str, "actual_cost": int|float|Decimal, "postings": list[dict]|None}]
        confirm          (bool) — confirm-first gate: False (default) returns pending_approval; True executes
        is_ocr           (bool) — flag indicating whether invoice amounts are OCR-derived (fails closed)
        ocr_derived      (bool) — alias for is_ocr (fails closed)
        document_format  (str) — source document format (ocr_pdf / ocr_image fail closed)

    Returns a JSON string with approval results or status envelope:
        {"status": "pending_approval", ...} when confirm is False,
        {"status": "executed", "result": {...}} on confirmed execution,
        or {"error": "..."} on validation failure / OCR refusal.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        ns_uuid = require_namespace_id(arguments)

        approval_id = str(arguments.get("approval_id") or "").strip()
        if not approval_id:
            raise ValueError("approval_id is required (idempotency key)")

        quote_id = str(arguments.get("quote_id") or "").strip()
        if not quote_id:
            raise ValueError("quote_id is required")

        confirm = bool(arguments.get("confirm", False))
        if not confirm:
            return json.dumps(
                {
                    "status": "pending_approval",
                    "action_type": "economy_approve_invoice",
                    "approval_id": approval_id,
                    "quote_id": quote_id,
                    "message": (
                        "Human confirmation required to execute invoice approval cascade. "
                        "Set confirm=True to execute."
                    ),
                },
                default=str,
            )

        invoice_id = arguments.get("invoice_id")

        # Fail closed on OCR-derived invoice figures (OQ-2 advisor-only posture)
        is_ocr = bool(
            arguments.get("is_ocr")
            or arguments.get("ocr_derived")
            or arguments.get("document_format") in ("ocr_pdf", "ocr_image")
        )
        if not is_ocr and isinstance(arguments.get("lines"), list):
            for line in arguments["lines"]:
                if isinstance(line, dict) and (line.get("is_ocr") or line.get("ocr_derived")):
                    is_ocr = True
                    break

        if not is_ocr and invoice_id and getattr(engine, "pg_pool", None) is not None:
            try:
                async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
                    ocr_row = await conn.fetchrow(
                        """
                        SELECT metadata->>'document_format' AS fmt,
                               metadata->>'requires_review' AS req_rev
                        FROM memories
                        WHERE namespace_id = $1::uuid
                          AND metadata->>'invoice_id' = $2
                        LIMIT 1
                        """,
                        str(ns_uuid),
                        str(invoice_id),
                    )
                    if ocr_row and (
                        ocr_row["fmt"] in ("ocr_pdf", "ocr_image")
                        or str(ocr_row["req_rev"]).lower() == "true"
                    ):
                        is_ocr = True
            except Exception as e:
                log.warning("[economy] Failed to check memories for OCR invoice: %s", e)

        if is_ocr:
            raise ValueError(
                "Refusing automated approval on OCR-derived invoice amount: "
                "OCR extraction requires human review / advisor-only (OQ-2)"
            )

        params = {
            "namespace_id": ns_uuid,
            "approval_id": approval_id,
            "quote_id": quote_id,
            "project_id": arguments.get("project_id"),
            "invoice_id": invoice_id,
            "supplier_id": arguments.get("supplier_id"),
            "invoice_amount": arguments.get("invoice_amount"),
            "invoice_postings": arguments.get("invoice_postings"),
            "lines": arguments.get("lines"),
        }
        cascade_result = await do_cascade_on_approval(engine, params)
        result_payload = {
            "status": "executed",
            "approval_id": approval_id,
            "result": cascade_result,
        }
    except McpError:
        raise
    except (ValueError, KeyError, TypeError, UnbalancedPostingsError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_approve_invoice unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result_payload, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_approve_invoice result not JSON-serializable: %s",
            exc,
        )
        return json.dumps(
            {
                "error": (
                    f"economy_approve_invoice: result contains a non-finite value and "
                    f"cannot be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_generate_kid(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_generate_kid — Norwegian KID generator (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        base_number  (str) — 1-24 ASCII digits
    Optional arguments:
        variant      (str) — check digit scheme, default "MOD10"

    Returns JSON string with base_number, check_digit, kid, and variant,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        base_number = arguments.get("base_number")
        if not base_number:
            raise ValueError("economy_generate_kid: 'base_number' is required")
        variant = arguments.get("variant", "MOD10")
        result = do_generate_kid(str(base_number), variant=str(variant))
    except McpError:
        raise
    except (ValueError, KeyError, TypeError, NotImplementedError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_generate_kid unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error("[economy] handle_economy_generate_kid result not JSON-serializable: %s", exc)
        return json.dumps(
            {
                "error": (
                    f"economy_generate_kid: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_validate_kid(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_validate_kid — Norwegian KID validator (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
        kid          (str) — at least 2 ASCII digits (base + check digit)
    Optional arguments:
        variant      (str) — check digit scheme, default "MOD10"

    Returns JSON string with kid, valid (bool), and variant,
    or {"error": "..."} on validation failure.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        kid = arguments.get("kid")
        if not kid:
            raise ValueError("economy_validate_kid: 'kid' is required")
        variant = arguments.get("variant", "MOD10")
        result = do_validate_kid(str(kid), variant=str(variant))
    except McpError:
        raise
    except (ValueError, KeyError, TypeError, NotImplementedError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_validate_kid unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error("[economy] handle_economy_validate_kid result not JSON-serializable: %s", exc)
        return json.dumps(
            {
                "error": (
                    f"economy_validate_kid: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_generate_ehf(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_generate_ehf — EHF 3.0 UBL XML generator ([ADMIN] privileged).

    Required arguments:
        namespace_id (str, UUID)
        invoice      (dict) — UBL invoice dictionary
    Optional arguments:
        idempotency_key (str) — stable transmission correlation key

    Returns JSON string with ehf_xml, sent, peppol_enabled, mode, and reason or transport_result.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        ns_uuid = require_namespace_id(arguments)
        invoice = arguments.get("invoice")
        if not isinstance(invoice, dict) or not invoice:
            raise ValueError("economy_generate_ehf: 'invoice' dict is required")
        idempotency_key = str(arguments.get("idempotency_key") or uuid.uuid4())
        result = await do_generate_ehf(
            invoice, namespace_id=str(ns_uuid), idempotency_key=idempotency_key
        )
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_generate_ehf unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error("[economy] handle_economy_generate_ehf result not JSON-serializable: %s", exc)
        return json.dumps(
            {
                "error": (
                    f"economy_generate_ehf: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_validate_contract(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_validate_contract — CPI uplift validator (READ-ONLY Advisor).

    Required arguments:
        namespace_id     (str, UUID)
        contract_id      (str) — contract identifier in economy_contracts
        proposed_cpi_pct (int, float, Decimal, str) — proposed CPI uplift fraction

    Returns JSON string with ok, contract_id, cpi_cap, proposed_cpi_pct,
    current_annual_amount, and renewal_annual_amount, or {"error": "..."} on validation refusal.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        ns_uuid = require_namespace_id(arguments)
        contract_id = arguments.get("contract_id")
        if not contract_id:
            raise ValueError("economy_validate_contract: 'contract_id' is required")
        proposed_cpi_pct = arguments.get("proposed_cpi_pct")
        if proposed_cpi_pct is None:
            raise ValueError("economy_validate_contract: 'proposed_cpi_pct' is required")
        params = {
            "namespace_id": ns_uuid,
            "contract_id": str(contract_id),
            "proposed_cpi_pct": proposed_cpi_pct,
        }
        result = await do_validate_contract(engine, params)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_validate_contract unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error(
            "[economy] handle_economy_validate_contract result not JSON-serializable: %s", exc
        )
        return json.dumps(
            {
                "error": (
                    f"economy_validate_contract: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )


@mcp_handler
async def handle_economy_get_gl_records(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: economy_get_gl_records — query GL postings with C8 allow-list projection (READ-ONLY Advisor).

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        since_iso         (str) — lower bound ISO datetime/date string
        until_iso         (str) — upper bound ISO datetime/date string
        account           (str) — exact GL account code
        account_prefix    (str) — account prefix string
        period_id         (str) — accounting period identifier
        economy_source_id (str) — source identifier
        limit             (int) — max records to return (default 5000, max 10000)

    Returns JSON string with {"ok": true, "records": [...], "count": int}.
    """
    try:
        await _check_economy_enabled(engine, arguments)
        result = await do_get_gl_records(engine, arguments)
    except McpError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        return json.dumps({"error": str(exc)}, default=str)
    except Exception as exc:
        log.exception("[economy] handle_economy_get_gl_records unexpected error")
        return json.dumps({"error": str(exc)}, default=str)

    try:
        return json.dumps(result, default=str, allow_nan=False)
    except ValueError as exc:
        log.error("[economy] handle_economy_get_gl_records result not JSON-serializable: %s", exc)
        return json.dumps(
            {
                "error": (
                    f"economy_get_gl_records: result contains a non-finite value and cannot "
                    f"be serialized ({exc})"
                )
            },
            default=str,
        )
