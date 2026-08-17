"""
Admin HTTP handlers for the Economy vertical module (M8.W4: cores-surface).

Exports:
  ``api_economy_match_invoice``   — POST /api/economy/match-invoice
  ``api_economy_periodisering``   — POST /api/economy/periodisering
  ``api_economy_emit_event``      — POST /api/economy/emit-event

All handlers are thin REST wrappers over the W1–W3 pure cores; they do not
duplicate logic. Read-only Advisor routes — no mutation, no LLM in the path.
The emit-event route is a DRY-RUN validator: it returns the balance-
guarantee verdict and the normalised/hashed event but does not persist
anything (persistence is Wave 6).

Config-as-IP (thresholds / chart-of-accounts / account-mapping) is always
loaded via the W1/W2 loaders — never accepted from the request body, so a
caller can never auto-approve its own invoice or redirect its own postings
(money-module briefing, Batch 116 handoff to B119).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any
from uuid import UUID

from nce.admin_handlers._shared import (
    JSONResponse,
    admin_error_response,
    admin_state,
)
from nce.auth import validate_agent_id
from nce.vertical_modules.economy._guard import EconomyDisabledError, require_economy_enabled
from nce.vertical_modules.economy.events import UnbalancedPostingsError, do_emit_financial_event
from nce.vertical_modules.economy.matching import do_match_invoice, load_economy_thresholds
from nce.vertical_modules.economy.ngaap import (
    do_compute_bucket_targets,
    load_finago_account_mapping,
    load_finago_chart_of_accounts,
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


def _neutralise_non_finite(value: Any) -> Any:
    """Recursively replace non-finite ``float``\\ s (``nan``/``inf``/``-inf``) with their
    string form, so ``json.dumps`` never has to fall back to emitting the bare
    ``NaN``/``Infinity`` tokens that Starlette's ``JSONResponse.render`` (``allow_nan=False``)
    rejects with a ``ValueError``.

    ``json.dumps``'s ``default=`` hook (used below in :func:`_json_safe` for ``Decimal``) is
    **never** invoked for ``float`` — floats are natively handled — so a non-finite float
    silently sails through ``_json_safe`` unconverted and only blows up later, inside
    ``JSONResponse``'s own encoder. At that point it is indistinguishable from a genuine
    domain-validation ``ValueError`` and gets misreported as an invalid invoice/event instead
    of what it actually is: a correct computation that merely echoed a non-finite caller value
    (e.g. a poisoned ``candidate_id`` or ``period_end``). Converting here, before ``json.dumps``
    ever sees the value, avoids that exception entirely — the same treatment ``Decimal``
    already gets via ``default=str``.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, dict):
        return {key: _neutralise_non_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_neutralise_non_finite(item) for item in value]
    return value


def _json_safe(value: Any) -> Any:
    """Round-trip *value* through a ``Decimal``-aware ``json.dumps`` so every
    ``Decimal`` becomes its exact string form before Starlette's own JSON
    encoder (which has no ``default=`` hook here) ever sees it. Non-finite
    ``float`` values are neutralised the same way (see
    :func:`_neutralise_non_finite`) so a caller-echoed NaN/Infinity can never reach
    Starlette's ``allow_nan=False`` encoder and be mis-filed as a domain-validation error.

    Money must never be coerced through ``float`` (money-module briefing #2;
    see also ``ngaap.py``'s module docstring) — this is the route layer's
    job, not the core's.
    """
    return json.loads(json.dumps(_neutralise_non_finite(value), default=str))


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

    namespace_id = str(body.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    # Validate the UUID shape at the REST boundary, BEFORE the opt-in gate:
    # `validate_agent_id` only sanitizes free text and never raises (see
    # nce/auth.py), so it cannot catch a malformed namespace_id. Without this
    # explicit check, `_check_economy_enabled_rest` -> `require_economy_enabled`
    # would hand the raw string to asyncpg's `::uuid` cast, which raises
    # asyncpg.exceptions.DataError (not ValueError) and escapes uncaught.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

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

    namespace_id = str(body.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    # See api_economy_match_invoice: explicit UUID check must precede the
    # opt-in gate — validate_agent_id() never raises, so it cannot do this job.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

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

    namespace_id = str(body.get("namespace_id") or "").strip()
    if not namespace_id:
        return JSONResponse({"error": "Missing required field: namespace_id"}, status_code=422)

    # See api_economy_match_invoice: explicit UUID check must precede the
    # opt-in gate — validate_agent_id() never raises, so it cannot do this job.
    namespace_id = validate_agent_id(namespace_id)
    try:
        UUID(namespace_id)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

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
