"""
Admin HTTP handlers for the Economy vertical module (M8.W4: cores-surface).

Exports:
  ``api_economy_match_invoice``           — POST /api/economy/match-invoice
  ``api_economy_periodisering``           — POST /api/economy/periodisering
  ``api_economy_emit_event``              — POST /api/economy/emit-event
  ``api_economy_forecast``                — GET/POST /api/economy/forecast
  ``api_economy_mrr_arr_churn``           — GET/POST /api/economy/mrr-arr-churn
  ``api_economy_dunning``                 — GET/POST /api/economy/dunning
  ``api_economy_recognition_schedule``    — GET/POST /api/economy/recognition-schedule
  ``api_economy_gl_sync_status``          — GET/POST /api/economy/gl-sync-status
  ``api_economy_close_narrative``         — GET/POST /api/economy/close-narrative

All handlers are thin REST wrappers over the pure cores; they do not
duplicate logic. Read-only Advisor routes — no mutation, no LLM in the path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nce.admin_handlers._shared import (
    JSONResponse,
    _json_safe,
    _require_namespace_id,
    admin_error_response,
    admin_state,
)
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.economy._guard import EconomyDisabledError, require_economy_enabled
from nce.vertical_modules.economy.close_narrative import do_generate_close_narrative
from nce.vertical_modules.economy.dunning import do_compute_dunning
from nce.vertical_modules.economy.events import UnbalancedPostingsError, do_emit_financial_event
from nce.vertical_modules.economy.finago import do_gl_sync_status
from nce.vertical_modules.economy.forecast import do_forecast_cashflow
from nce.vertical_modules.economy.matching import do_match_invoice, load_economy_thresholds
from nce.vertical_modules.economy.ngaap import (
    do_compute_bucket_targets,
    load_finago_account_mapping,
    load_finago_chart_of_accounts,
)
from nce.vertical_modules.economy.recurring import (
    do_compute_recognition_schedule,
    do_snapshot_mrr_arr_churn,
)

log = logging.getLogger("nce.admin_handlers.economy")

# ---------------------------------------------------------------------------
# Shared opt-in guard — applied at REST route boundary (not inside do_* cores)
# ---------------------------------------------------------------------------


async def _check_economy_enabled_rest(namespace_id: str) -> JSONResponse | None:
    """Return a 409 JSONResponse when economy vertical is not enabled; else None."""
    try:
        await require_economy_enabled(admin_state.engine.pg_pool, namespace_id)
        return None
    except EconomyDisabledError as exc:
        return JSONResponse(
            {"error": "Economy vertical is not enabled for this namespace", "detail": str(exc)},
            status_code=409,
        )


# Balance tolerance (NOK) for the emit-event dry-run validator. Mirrors the
# call-site default documented in events.py's do_emit_financial_event
# docstring (NCE_ECONOMY_BALANCE_EPSILON, default 0.01). This wave adds no
# config key (B119 orchestrator ruling) — never caller-supplied, the same
# rule as thresholds and the chart-of-accounts/mapping loaders below.
_BALANCE_EPSILON_DEFAULT: float = 0.01

# Keys reserved by this route's response envelope (``{**_json_safe(result), "status": "ok"}``
# below, plus the "error" key every 4xx/5xx branch in this module returns). do_emit_financial_event
# echoes every top-level key of the caller's event into its result by design (events.py:460), so a
# caller-supplied "status" or "error" must be rejected outright -- round 2 already stops "status"
# from overwriting the envelope; this is the matching guard for "error", which is the surface's
# sole success/failure signal. Checked with an EXACT match (see the call site) -- do not lowercase
# or strip before comparing.
_RESERVED_EVENT_KEYS: frozenset[str] = frozenset({"status", "error"})


async def api_economy_match_invoice(request) -> JSONResponse:
    """POST /api/economy/match-invoice

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        invoice      (dict, required): see ``matching.do_match_invoice``.
        candidates   (list[dict], optional): candidate pool.

    Response (JSON):
        {"status": "ok", "score": int, "tier": str, "breakdown": [...]}
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    # Validate the UUID shape at the REST boundary, BEFORE the opt-in gate:
    # the shared helper's `uuid.UUID(...)` parse is the real check, because
    # `validate_agent_id` only sanitizes free text and never raises (see
    # nce/auth.py). Without it, `_check_economy_enabled_rest` ->
    # `require_economy_enabled` would hand the raw string to asyncpg's
    # `::uuid` cast, which raises asyncpg.exceptions.DataError (not
    # ValueError) and escapes uncaught.
    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err
    # _require_namespace_id's contract: err is None => namespace_id is set.
    # mypy cannot correlate the two tuple slots, so state the invariant.
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        invoice = dict(body.get("invoice") or {})
        candidates = list(body.get("candidates") or [])
        thresholds = load_economy_thresholds()
        result = do_match_invoice(thresholds, invoice, candidates)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy match invoice error",
            exc,
            status_code=500,
            log_event="api_economy_match_invoice",
        )


async def api_economy_periodisering(request) -> JSONResponse:
    """POST /api/economy/periodisering

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        params       (dict, required): see ``ngaap.do_compute_bucket_targets``
                     (``buckets``/``project_id``/``period_end``).

    Response (JSON):
        {"status": "ok", "buckets": [...], "totals": {...}, ...}
        Every amount is serialised as an exact decimal string (never a
        ``float``) — see ``_json_safe``.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    # See api_economy_match_invoice: the shared helper's explicit UUID check
    # must precede the opt-in gate — validate_agent_id() never raises.
    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err
    # _require_namespace_id's contract: err is None => namespace_id is set.
    # mypy cannot correlate the two tuple slots, so state the invariant.
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        params = dict(body.get("params") or {})
        chart = load_finago_chart_of_accounts()
        mapping = load_finago_account_mapping()
        result = do_compute_bucket_targets(chart, mapping, params)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy periodisering error",
            exc,
            status_code=500,
            log_event="api_economy_periodisering",
        )


async def api_economy_emit_event(request) -> JSONResponse:
    """POST /api/economy/emit-event

    DRY-RUN balance-guarantee validator: validates *event* and returns its
    normalised/hashed form. Never persists anything (persistence is Wave 6).

    Request body (JSON):
        namespace_id (str, required): Active namespace UUID.
        event        (dict, required): see ``events.do_emit_financial_event``
                     (``type``, optional ``postings``).

    Response (JSON):
        {"status": "ok", "hash": str, "postings": [...], ...} on success, or
        a 422 with a structured error (``event_type``/``diff``/``tolerance``)
        when the postings are unbalanced — never a crash, never auto-balanced.

        An ``event`` that itself carries a reserved envelope key (``status``/
        ``error``, see ``_RESERVED_EVENT_KEYS``) is rejected with a 422
        ``{"error": ...}`` naming the offending key(s), before the core ever
        runs — otherwise ``do_emit_financial_event``'s documented key-echo
        (events.py:460) could make a correctly-balanced success carry an
        ``"error"`` key and be mistaken for a failure.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=422)

    # See api_economy_match_invoice: the shared helper's explicit UUID check
    # must precede the opt-in gate — validate_agent_id() never raises.
    namespace_id, err = _require_namespace_id(body.get("namespace_id"))
    if err is not None:
        return err
    # _require_namespace_id's contract: err is None => namespace_id is set.
    # mypy cannot correlate the two tuple slots, so state the invariant.
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        event = dict(body.get("event") or {})
        # Exact match against dict keys -- do NOT lowercase/strip before comparing.
        # "Status"/"status " etc. cannot collide with the literal envelope keys and
        # must stay accepted; normalising here would both reject legitimate keys and
        # reintroduce the validate-one-thing-read-another trap from Batch 116's
        # round-3 defect. Checked before the core runs, so a rejected request does
        # no work.
        reserved_hit = _RESERVED_EVENT_KEYS & event.keys()
        if reserved_hit:
            return JSONResponse(
                {"error": f"event contains reserved key(s): {sorted(reserved_hit)}"},
                status_code=422,
            )
        result = do_emit_financial_event(_BALANCE_EPSILON_DEFAULT, event)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except UnbalancedPostingsError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "event_type": exc.event_type,
                "diff": str(exc.diff),
                "tolerance": str(exc.tolerance),
            },
            status_code=422,
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy emit event error",
            exc,
            status_code=500,
            log_event="api_economy_emit_event",
        )


async def _extract_request_data(request: Any) -> tuple[dict[str, Any], JSONResponse | None]:
    """Extract request dictionary from JSON body or query params."""
    body: dict[str, Any] = {}
    if hasattr(request, "query_params"):
        try:
            if isinstance(request.query_params, dict) or hasattr(request.query_params, "items"):
                body.update(dict(request.query_params))
        except Exception:
            pass
    if hasattr(request, "json") and callable(request.json):
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body.update(raw)
            elif raw is not None:
                return {}, JSONResponse(
                    {"error": "Invalid JSON body: expected object"}, status_code=422
                )
        except Exception:
            method = getattr(request, "method", None)
            if method == "POST":
                return {}, JSONResponse({"error": "Invalid JSON body"}, status_code=422)
    return body, None


async def api_economy_forecast(request: Any) -> JSONResponse:
    """GET/POST /api/economy/forecast — Monte Carlo cashflow forecast."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err_resp = await _extract_request_data(request)
    if err_resp is not None:
        return err_resp

    raw_ns = body.get("namespace_id") or (
        request.query_params.get("namespace_id") if hasattr(request, "query_params") else None
    )
    namespace_id, err = _require_namespace_id(raw_ns)
    if err is not None:
        return err
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        seed = body.get("seed")
        if isinstance(seed, str) and not isinstance(seed, bool):
            try:
                seed = int(seed)
            except ValueError:
                pass
        if "params" in body and isinstance(body["params"], dict):
            params = dict(body["params"])
        else:
            params = {k: v for k, v in body.items() if k not in ("namespace_id", "seed")}
        if isinstance(params.get("periods"), str):
            try:
                params["periods"] = json.loads(params["periods"])
            except Exception:
                pass
        result = do_forecast_cashflow(seed, params)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy forecast error",
            exc,
            status_code=500,
            log_event="api_economy_forecast",
        )


async def api_economy_mrr_arr_churn(request: Any) -> JSONResponse:
    """GET/POST /api/economy/mrr-arr-churn — MRR/ARR/churn snapshot."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err_resp = await _extract_request_data(request)
    if err_resp is not None:
        return err_resp

    raw_ns = body.get("namespace_id") or (
        request.query_params.get("namespace_id") if hasattr(request, "query_params") else None
    )
    namespace_id, err = _require_namespace_id(raw_ns)
    if err is not None:
        return err
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        if "params" in body and isinstance(body["params"], dict):
            params = dict(body["params"])
        else:
            contracts = body.get("contracts")
            if isinstance(contracts, str):
                try:
                    contracts = json.loads(contracts)
                except Exception:
                    pass
            params = {"contracts": contracts}
        result = do_snapshot_mrr_arr_churn(params)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy MRR/ARR/churn error",
            exc,
            status_code=500,
            log_event="api_economy_mrr_arr_churn",
        )


async def api_economy_dunning(request: Any) -> JSONResponse:
    """GET/POST /api/economy/dunning — Norwegian dunning / credit policy."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err_resp = await _extract_request_data(request)
    if err_resp is not None:
        return err_resp

    raw_ns = body.get("namespace_id") or (
        request.query_params.get("namespace_id") if hasattr(request, "query_params") else None
    )
    namespace_id, err = _require_namespace_id(raw_ns)
    if err is not None:
        return err
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        if "customer" in body and isinstance(body["customer"], dict):
            customer = dict(body["customer"])
        else:
            customer = {k: v for k, v in body.items() if k != "namespace_id"}
        if "credit_risk_score" in customer and isinstance(customer["credit_risk_score"], str):
            try:
                customer["credit_risk_score"] = float(customer["credit_risk_score"])
            except ValueError:
                pass
        result = do_compute_dunning(customer)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy dunning error",
            exc,
            status_code=500,
            log_event="api_economy_dunning",
        )


async def api_economy_recognition_schedule(request: Any) -> JSONResponse:
    """GET/POST /api/economy/recognition-schedule — 12-month ratable recognition schedule."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err_resp = await _extract_request_data(request)
    if err_resp is not None:
        return err_resp

    raw_ns = body.get("namespace_id") or (
        request.query_params.get("namespace_id") if hasattr(request, "query_params") else None
    )
    namespace_id, err = _require_namespace_id(raw_ns)
    if err is not None:
        return err
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        if "params" in body and isinstance(body["params"], dict):
            params = dict(body["params"])
        else:
            params = {k: v for k, v in body.items() if k != "namespace_id"}
        result = do_compute_recognition_schedule(params)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy recognition schedule error",
            exc,
            status_code=500,
            log_event="api_economy_recognition_schedule",
        )


async def api_economy_gl_sync_status(request: Any) -> JSONResponse:
    """GET/POST /api/economy/gl-sync-status — GL reconciliation sync status."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err_resp = await _extract_request_data(request)
    if err_resp is not None:
        return err_resp

    raw_ns = body.get("namespace_id") or (
        request.query_params.get("namespace_id") if hasattr(request, "query_params") else None
    )
    namespace_id, err = _require_namespace_id(raw_ns)
    if err is not None:
        return err
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    try:
        params: dict[str, Any] = {"namespace_id": namespace_id}
        if "window_hours" in body:
            params["window_hours"] = body["window_hours"]
        result = await do_gl_sync_status(admin_state.engine, params)
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy GL sync status error",
            exc,
            status_code=500,
            log_event="api_economy_gl_sync_status",
        )


async def api_economy_close_narrative(request: Any) -> JSONResponse:
    """GET/POST /api/economy/close-narrative — C9a grounded period-close narrative."""
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    body, err_resp = await _extract_request_data(request)
    if err_resp is not None:
        return err_resp

    raw_ns = body.get("namespace_id") or (
        request.query_params.get("namespace_id") if hasattr(request, "query_params") else None
    )
    namespace_id, err = _require_namespace_id(raw_ns)
    if err is not None:
        return err
    assert namespace_id is not None

    disabled = await _check_economy_enabled_rest(namespace_id)
    if disabled is not None:
        return disabled

    period_id = body.get("period_id")
    if not period_id:
        return JSONResponse({"error": "period_id is required"}, status_code=422)

    try:
        async with scoped_pg_session(admin_state.engine.pg_pool, namespace_id) as conn:
            result = await do_generate_close_narrative(
                conn, namespace_id=namespace_id, period_id=str(period_id)
            )
        return JSONResponse({**_json_safe(result), "status": "ok"})
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return admin_error_response(
            "Economy close narrative error",
            exc,
            status_code=500,
            log_event="api_economy_close_narrative",
        )
