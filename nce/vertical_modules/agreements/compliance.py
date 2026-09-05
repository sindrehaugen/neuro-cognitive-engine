"""
nce/vertical_modules/agreements/compliance.py
==============================================
Kickback-governance compliance gate for the Agreements vertical module — M3.W8.

This is the **security-critical rebate-authorization gate**.  Procurement's
``do_submit_po`` calls the A2A tool ``agreements.compliance_audit`` before it
will apply a rebate/kickback override to a purchase order
(``nce/vertical_modules/procurement/po.py:_call_agreements_compliance_audit``).
Procurement treats ``approved != True`` **or any raised error** as
fail-closed → human confirm.  A wrongful ``approved=True`` leaks money; an
over-rejection merely routes to human review.  Therefore:

**FAIL-CLOSED is the golden rule.**  ``do_run_compliance_audit`` returns
``approved=True`` from exactly ONE code path — reached only when a *human-signed*
agreement (``review_status == 'auto_green'``) affirmatively governs the supplier,
carries no restricted clause, encodes a rebate/kickback provision, and yields a
derivable numeric ceiling that the requested rebate does not exceed.  Every other
outcome — missing/invalid input, unresolvable supplier, no signed agreement,
supplier-identity mismatch, ambiguity, a restricted clause, no rebate provision,
no derivable ceiling, an over-limit rebate, or malformed terms — returns
``approved=False`` with a reason.  Normal "no" answers never raise (they return
``approved=False``); exceptions are reserved for genuinely broken infrastructure,
which procurement also fails closed on.

Design constraints reused from kickback.py / coverage.py (uncle-bob-craft)
--------------------------------------------------------------------------
- **§9.3 sign-off gate.**  Money/legal fields never ``auto_green`` at OCR
  extraction (``extract.py:_map_confidence_to_status``); only a human 'confirm'
  in ``review.py`` sets ``review_status = 'auto_green'``.  Row-level
  ``auto_green`` is therefore the machine-checkable proxy for "a human signed
  the money terms" — this gate reads **only** ``auto_green`` agreements.
- **Identity via C1, never raw strings.**  The requesting ``supplier_id`` and
  each agreement's ``extracted.supplierId`` are resolved to canonical VENDOR
  ``kg_nodes`` via ``coverage._resolve_vendor_node_id`` (the vendors/registry.py
  gate(>=0.2) + exact-suffix-confirm pattern — the B108 anti-false-match
  lesson).  A supplier that cannot be resolved never matches (``None`` never
  matches ``None``).
- **Money discipline.**  Tier tables are normalized through kickback's
  ``_normalize_tiers`` (fails closed on bools, non-finite, negative, duplicate,
  localized-decimal inputs).  All money comparisons use ``Decimal``.
- **Append-only audit trail.**  Every decision (approve *and* deny) and every
  ``do_suggest_terms`` proposal is written as an append-only
  ``v3_cognitive_ledger`` INSERT; this module never UPDATEs or DELETEs a ledger
  row.  Timestamps come from the DB clock (``SELECT now()``).
- **Explicit namespace predicate.**  Every SQL query carries
  ``namespace_id = $N::uuid`` — never RLS-only (owner-pool test roles can bypass
  FORCE RLS; repo lesson).
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.agreements.coverage import (
    _resolve_vendor_node_id,
    _unwrap_field,
)
from nce.vertical_modules.agreements.kickback import (
    MalformedTermsError,
    _normalize_tiers,
)

log = logging.getLogger("nce.vertical_modules.agreements.compliance")

# model_version discriminator for this module's v3_cognitive_ledger rows.
_MODEL_VERSION = "agreements-compliance-v1"

# Zero tensor matching the NOT NULL empathic_tensor column (float[6] in the live
# schema) — mirrors kickback.py / authoring.py.
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Payload ``kind`` discriminators inside tlx_scores.
_DECISION_KIND = "compliance_audit"
_SUGGESTION_KIND = "terms_suggestion"

# The ONLY review_status that authorizes money: a human 'confirm' (§9.3).
_SIGNED_STATUS = "auto_green"

# Money values are quantized to øre (2 decimal places).
_MONEY_QUANT = Decimal("0.01")

# Restricted-clause markers.  A small, sensible default set: any agreement whose
# extracted terms mention one of these forbids (or is incompatible with) paying a
# rebate/kickback on this PO, so a hit fails the audit closed.  Matched as a
# substring against a lowercased JSON blob of the extracted terms in BOTH the
# underscore form and the space form (``"no_rebate"`` also matches ``"no rebate"``)
# so OCR text and structured flags are both covered.  A benign false-positive
# merely over-rejects (safe → human review); the risk this guards against is a
# wrongful approve.
_RESTRICTED_CLAUSE_MARKERS: tuple[str, ...] = (
    # NOTE ON VOCABULARY: "kickback" here is the NORWEGIAN commercial term for a
    # volume-based supplier rebate, and these are SEARCH KEYWORDS matched against the
    # text of third-party supplier agreements -- not this project's own vocabulary.
    # A contract that literally says "no kickback" must still match, so these strings
    # MUST NOT be renamed to "rebate": doing so silently breaks clause detection.
    # Note that ``anti_bribery`` below is a SEPARATE flag -- a rebate clause and a
    # bribery clause are different things and are detected independently.
    # See the "Terminology" section of README.md.
    "no_rebate",
    "rebate_prohibited",
    "no_kickback",
    "kickback_prohibited",
    "no_incentive",
    "exclusivity",
    "anti_bribery",
    "anti_corruption",
)

# Term fields the discount-limit ceiling is derived from.
_CEILING_FIELDS: tuple[str, ...] = ("kickbackTiers", "frameDiscountPct", "volumeCommitment")

# Term fields the negotiator (do_suggest_terms) compares against a benchmark.
_NEGOTIABLE_FIELDS: tuple[str, ...] = (
    "paymentTermsDays",
    "frameDiscountPct",
    "volumeCommitment",
    "kickbackTiers",
)


# ---------------------------------------------------------------------------
# Pure domain helpers — zero DB, zero HTTP
# ---------------------------------------------------------------------------


def _coerce_extracted(raw: Any) -> dict[str, Any]:
    """Coerce the ``extracted`` jsonb column (str | dict | None) to a dict."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _coerce_finite_nonneg(raw: Any, field_name: str) -> Decimal | None:
    """Return a present, finite, non-negative money/pct value as ``Decimal``.

    Returns ``None`` when the field is absent (``raw is None``).  Raises
    :exc:`MalformedTermsError` when the field is present but cannot be normalized
    without guessing — a boolean (``float(True) == 1.0`` would fabricate a
    number), a non-numeric / localized-decimal string (``"3,5"``), a non-finite
    value (``"nan"``/``"inf"``), or a negative value.  Money authorization must
    never guess a ceiling from a malformed term.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise MalformedTermsError(f"{field_name} is a boolean")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MalformedTermsError(f"{field_name} is not numeric: {raw!r}") from exc
    if not value.is_finite():
        raise MalformedTermsError(f"{field_name} is non-finite: {raw!r}")
    if value < 0:
        raise MalformedTermsError(f"{field_name} is negative: {raw!r}")
    return value


def _evaluate_restricted_clause(extracted: dict[str, Any]) -> tuple[bool, list[str]]:
    """Scan the extracted terms for restricted-clause markers.

    Returns ``(clean, hits)`` where ``clean`` is ``True`` when NO marker was
    found (the check passes) and ``hits`` lists the markers that matched.  The
    whole extracted dict — keys and values, at any depth — is flattened to a
    lowercased JSON blob so a marker is caught whether it appears as a structured
    flag key, a list entry, or free OCR text.
    """
    blob = json.dumps(extracted, sort_keys=True, default=str).lower()
    hits: list[str] = []
    for marker in _RESTRICTED_CLAUSE_MARKERS:
        if marker in blob or marker.replace("_", " ") in blob:
            hits.append(marker)
    return (not hits), hits


def _evaluate_discount_limit(terms: dict[str, Any], rebate: Decimal) -> tuple[bool, str | None]:
    """Decide whether ``rebate`` is within the ceiling derivable from signed terms.

    ``terms`` holds the UNWRAPPED ``kickbackTiers`` / ``frameDiscountPct`` /
    ``volumeCommitment`` values.  Returns ``(within_limit, reason)`` where a
    ``False`` result carries the reason.  Fail-closed at every ambiguity:

    - Malformed tiers / frame / volume → ``False`` (never a guessed number).
    - No rebate/kickback provision at all (no tiers *and* no frame discount) →
      ``False`` — there is no signed basis for ANY rebate.
    - A provision exists but no ``volumeCommitment`` spend basis is present →
      ``False`` — a percentage alone yields no derivable absolute NOK ceiling,
      so the amount cannot be confirmed within a signed limit.
    - ``rebate`` exceeds the derived ceiling → ``False``.

    The ceiling is the most-generous absolute rebate the signed terms could
    justify AT THE COMMITTED VOLUME: ``volumeCommitment × rate% / 100``.

    For kickback tiers that rate is the ACTIVE tier's pct under the same
    retroactive-on-total model the rest of the engine uses
    (``kickback._tier_progression``): the active tier is the highest tier whose
    threshold is <= the committed volume, and it is singular.  Tiers whose
    threshold the committed volume never reaches are NOT authorized by the
    agreement at that volume and must not raise the ceiling.

    Taking the global maximum tier pct instead — regardless of threshold — is
    precisely how a ceiling becomes an over-approval: with tiers
    ``[100k@2%, 10M@25%]`` and ``volumeCommitment = 200k`` the entitlement the
    engine itself computes is ``200k × 2% = 4 000``, while a global-max ceiling
    would authorize ``200k × 25% = 50 000``.  A frame discount, where present,
    is an independent basis and still contributes its own rate.
    """
    try:
        tiers = _normalize_tiers(terms.get("kickbackTiers"))
        frame_pct = _coerce_finite_nonneg(terms.get("frameDiscountPct"), "frameDiscountPct")
        volume = _coerce_finite_nonneg(terms.get("volumeCommitment"), "volumeCommitment")
    except MalformedTermsError as exc:
        return False, f"malformed signed terms: {exc}"

    if not tiers and frame_pct is None:
        return False, "no signed rebate or kickback provision — no basis for any rebate"

    if volume is None:
        return False, (
            "no derivable numeric rebate ceiling — signed terms lack a "
            "volumeCommitment spend basis to bound the rebate amount"
        )

    rates: list[Decimal] = []

    # Tier basis: ONLY the active tier at the committed volume.  ``tiers`` is
    # threshold-ascending (guaranteed by ``_normalize_tiers``), so the last
    # tier at or below ``volume`` is the active one.
    active_tier: dict[str, float] | None = None
    for tier in tiers:
        if Decimal(str(tier["threshold"])) <= volume:
            active_tier = tier
        else:
            break
    if active_tier is not None:
        rates.append(Decimal(str(active_tier["pct"])))

    if frame_pct is not None:
        rates.append(frame_pct)

    if not rates:
        # A tier table exists but the committed volume reaches no tier, and no
        # frame discount applies.  Entitlement is zero (``_tier_progression``:
        # "Below the first tier: earned 0"), so no rebate is authorizable.
        return False, (
            f"committed volume {volume} reaches no kickback tier "
            f"(lowest threshold {tiers[0]['threshold']}) and no frame discount "
            "applies — no signed basis for a rebate"
        )

    max_rate = max(rates)
    ceiling = (volume * max_rate / Decimal("100")).quantize(_MONEY_QUANT, ROUND_HALF_UP)
    if rebate > ceiling:
        if active_tier is not None and Decimal(str(active_tier["pct"])) == max_rate:
            basis = f"active tier {active_tier['threshold']}@{active_tier['pct']}%"
        else:
            basis = f"frame discount {max_rate}%"
        return False, (
            f"rebate {rebate} exceeds signed ceiling {ceiling} "
            f"(volumeCommitment {volume} × {basis})"
        )
    return True, None


def _evaluate_term_recommendations(
    current: dict[str, Any],
    benchmark: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare current terms to a peer benchmark; return propose-only revisions.

    Only fields present in ``benchmark`` are considered, and a recommendation is
    emitted only when the values differ — every number in the output comes from
    either the current agreement or the supplied benchmark, never invented.  With
    an empty benchmark this returns ``[]``.
    """
    recommendations: list[dict[str, Any]] = []
    for field in _NEGOTIABLE_FIELDS:
        if field not in benchmark:
            continue
        peer = benchmark[field]
        ours = current.get(field)
        if ours is None:
            recommendations.append(
                {
                    "field": field,
                    "current_value": None,
                    "benchmark_value": peer,
                    "recommendation": (
                        f"current agreement has no {field}; peer benchmark is {peer} "
                        f"— propose adding it"
                    ),
                }
            )
            continue

        ours_num: float | None
        peer_num: float | None
        try:
            ours_num = float(ours)
            peer_num = float(peer)
        except (TypeError, ValueError):
            ours_num = peer_num = None

        if ours_num is not None and peer_num is not None:
            if ours_num == peer_num:
                continue
            recommendations.append(
                {
                    "field": field,
                    "current_value": ours,
                    "benchmark_value": peer,
                    "recommendation": _phrase_numeric_gap(field, ours, peer, ours_num, peer_num),
                }
            )
        elif ours != peer:
            recommendations.append(
                {
                    "field": field,
                    "current_value": ours,
                    "benchmark_value": peer,
                    "recommendation": (
                        f"{field} differs from peer benchmark ({ours} vs {peer}) "
                        f"— propose reviewing"
                    ),
                }
            )
    return recommendations


def _phrase_numeric_gap(
    field: str,
    ours: Any,
    peer: Any,
    ours_num: float,
    peer_num: float,
) -> str:
    """Direction-aware, buyer-favourable phrasing for a numeric term gap."""
    if field == "paymentTermsDays":
        if peer_num > ours_num:
            return f"payment terms Net {ours} vs peer Net {peer} — propose extending to Net {peer}"
        return f"payment terms Net {ours} already exceed peer Net {peer} — hold"
    if field == "frameDiscountPct":
        if peer_num > ours_num:
            return f"frame discount {ours}% vs peer {peer}% — propose increasing toward {peer}%"
        return f"frame discount {ours}% already exceeds peer {peer}% — hold"
    return f"{field} {ours} vs peer benchmark {peer} — propose reviewing"


# ---------------------------------------------------------------------------
# Ledger helper — append-only decision + suggestion trail
# ---------------------------------------------------------------------------


async def _append_ledger_entry(conn: Any, ns_uuid: uuid.UUID, payload: dict[str, Any]) -> str:
    """Append ONE row to ``v3_cognitive_ledger`` and return its id (append-only).

    ``payload`` must already carry its ``kind`` discriminator; the recorded
    timestamp is stamped here from the DB clock (``SELECT now()``) so the audit
    trail is single-sourced.  Rows written here are never mutated or removed.
    """
    ledger_id = uuid.uuid4()
    recorded_at = await conn.fetchval("SELECT now()")
    full_payload: dict[str, Any] = {**payload, "recorded_at_iso": recorded_at.isoformat()}
    await conn.execute(
        """
        INSERT INTO v3_cognitive_ledger (
            id, namespace_id, memory_id,
            empathic_tensor, tlx_scores, vad_scores, model_version
        ) VALUES (
            $1::uuid, $2::uuid, NULL,
            $3::float[], $4::jsonb, $5::jsonb, $6
        )
        """,
        str(ledger_id),
        str(ns_uuid),
        _ZERO_TENSOR,
        json.dumps(full_payload),
        json.dumps({}),
        _MODEL_VERSION,
    )
    return str(ledger_id)


# ---------------------------------------------------------------------------
# Signed-agreement resolution (DB, C1 identity)
# ---------------------------------------------------------------------------


async def _find_signed_agreement(
    conn: Any,
    ns_uuid: uuid.UUID,
    supplier_node_id: uuid.UUID,
    agreement_id_raw: Any,
) -> tuple[tuple[str, dict[str, Any]] | None, str | None]:
    """Resolve the single SIGNED agreement governing ``supplier_node_id``.

    Only ``review_status == 'auto_green'`` (human-confirmed) rows count.  Identity
    is matched on the resolved canonical VENDOR node, never on raw supplier
    strings.  Returns ``((agreement_id_str, extracted), None)`` on a unique match
    or ``(None, reason)`` on any failure or ambiguity (fail closed).
    """
    explicit = agreement_id_raw is not None
    if explicit:
        try:
            ag_uuid = uuid.UUID(str(agreement_id_raw))
        except (ValueError, TypeError):
            return None, f"agreement_id is not a valid UUID: {agreement_id_raw!r}"
        row = await conn.fetchrow(
            """
            SELECT agreement_id, review_status, extracted
            FROM   agreement_review_queue
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            ag_uuid,
            str(ns_uuid),
        )
        if row is None:
            return None, "agreement_id not found in the review queue for this namespace"
        if row["review_status"] != _SIGNED_STATUS:
            return None, (
                f"agreement is not signed (review_status={row['review_status']!r}); only "
                f"human-confirmed (auto_green) agreements may authorize a rebate"
            )
        candidates = [row]
    else:
        candidates = await conn.fetch(
            """
            SELECT agreement_id, review_status, extracted
            FROM   agreement_review_queue
            WHERE  namespace_id = $1::uuid AND review_status = $2
            """,
            str(ns_uuid),
            _SIGNED_STATUS,
        )

    matched: list[tuple[str, dict[str, Any]]] = []
    for row in candidates:
        extracted = _coerce_extracted(row["extracted"])
        supplier_raw = _unwrap_field(extracted, "supplierId")
        ag_node = await _resolve_vendor_node_id(
            conn,
            ns_uuid,
            raw_id=str(supplier_raw) if supplier_raw else None,
        )
        # None never matches None: an unresolvable agreement supplier can never
        # authorize a rebate (the B108 anti-false-match lesson).
        if ag_node is not None and ag_node == supplier_node_id:
            matched.append((str(row["agreement_id"]), extracted))

    if not matched:
        if explicit:
            return None, (
                "the specified agreement does not govern supplier_id (vendor identity mismatch)"
            )
        return None, "no signed agreement governs supplier"
    if len(matched) > 1:
        return None, (
            "ambiguous: multiple signed agreements govern supplier — "
            "pass agreement_id to disambiguate"
        )
    return matched[0], None


async def _record_decision(
    conn: Any,
    ns_uuid: uuid.UUID,
    *,
    po_number: Any,
    supplier_id: Any,
    agreement_id: str | None,
    rebate_amount: float,
    approved: bool,
    reasons: list[str],
    checks: dict[str, bool],
) -> str:
    """Append the audit decision (approve OR deny) to the append-only ledger."""
    payload: dict[str, Any] = {
        "kind": _DECISION_KIND,
        "po_number": po_number,
        "supplier_id": supplier_id,
        "agreement_id": agreement_id,
        "rebate_amount": rebate_amount,
        "approved": approved,
        "reasons": reasons,
        "checks": checks,
    }
    return await _append_ledger_entry(conn, ns_uuid, payload)


# ---------------------------------------------------------------------------
# Public entry point — THE GATE
# ---------------------------------------------------------------------------


async def do_run_compliance_audit(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Authorize (or refuse) a rebate/kickback override against SIGNED agreements.

    This is the core wrapped by the A2A tool ``agreements.compliance_audit``
    (wiring is a later wave).  Procurement fails closed on ``approved != True`` or
    any raised error, so this returns ``approved=False`` for every normal "no".

    Parameters
    ----------
    engine:
        NCEEngine instance (or test stub) providing ``pg_pool``.
    params:
        ``{
            "namespace_id":  str | UUID,   # required
            "po_number":     str,          # required
            "supplier_id":   str,          # required
            "rebate_amount": float,        # required — finite, non-negative
            "agreement_id":  str | UUID,   # optional — disambiguates the governing agreement
        }``

    Returns
    -------
    dict::

        {
            "approved":      bool,
            "reasons":       [str],
            "supplier_id":   Any,
            "po_number":     Any,
            "agreement_id":  str | None,
            "checks": {
                "signed_agreement": bool,   # True == a signed agreement governs the supplier
                "restricted_clause": bool,  # True == no restricted clause present (check passed)
                "discount_limit":   bool,   # True == rebate within the signed ceiling (check passed)
            },
        }

    ``approved`` is ``True`` iff all three checks passed.
    """
    po_number = params.get("po_number")
    supplier_id = params.get("supplier_id")
    checks: dict[str, bool] = {
        "signed_agreement": False,
        "restricted_clause": False,
        "discount_limit": False,
    }

    def _deny_no_ledger(reasons: list[str]) -> dict[str, Any]:
        """Refuse before any DB scope is available (nothing to audit-write yet)."""
        return {
            "approved": False,
            "reasons": reasons,
            "supplier_id": supplier_id,
            "po_number": po_number,
            "agreement_id": None,
            "checks": dict(checks),
        }

    # Step 1: parameter validation (fail closed, never crash on a normal "no").
    try:
        namespace_id = require_namespace_id(params)
    except ValueError as exc:
        return _deny_no_ledger([f"namespace_id missing or invalid: {exc}"])
    ns_uuid = uuid.UUID(namespace_id)

    if not isinstance(po_number, str) or not po_number.strip():
        return _deny_no_ledger(["po_number is required"])
    if not isinstance(supplier_id, str) or not supplier_id.strip():
        return _deny_no_ledger(["supplier_id is required"])

    rebate_raw = params.get("rebate_amount")
    if isinstance(rebate_raw, bool) or not isinstance(rebate_raw, (int, float)):
        return _deny_no_ledger(["rebate_amount must be a number"])
    rebate_dec = Decimal(str(rebate_raw))
    if not rebate_dec.is_finite():
        return _deny_no_ledger(["rebate_amount must be a finite number"])
    if rebate_dec < 0:
        return _deny_no_ledger(["rebate_amount must be non-negative"])

    agreement_id_raw = params.get("agreement_id")
    rebate_float = float(rebate_dec)

    # Steps 2-6 run inside ONE scoped session (resolution + checks + audit write).
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:

        async def _finish(
            approved: bool, reasons: list[str], agreement_id: str | None
        ) -> dict[str, Any]:
            await _record_decision(
                conn,
                ns_uuid,
                po_number=po_number,
                supplier_id=supplier_id,
                agreement_id=agreement_id,
                rebate_amount=rebate_float,
                approved=approved,
                reasons=reasons,
                checks=dict(checks),
            )
            return {
                "approved": approved,
                "reasons": reasons,
                "supplier_id": supplier_id,
                "po_number": po_number,
                "agreement_id": agreement_id,
                "checks": dict(checks),
            }

        # Step 2a: resolve the requesting supplier to a canonical VENDOR node.
        supplier_node_id = await _resolve_vendor_node_id(conn, ns_uuid, raw_id=supplier_id)
        if supplier_node_id is None:
            return await _finish(
                False,
                ["supplier_id could not be resolved to a known vendor node"],
                None,
            )

        # Step 2b: find the single SIGNED agreement governing this supplier.
        matched, reason = await _find_signed_agreement(
            conn, ns_uuid, supplier_node_id, agreement_id_raw
        )
        if matched is None:
            return await _finish(False, [reason or "no signed agreement governs supplier"], None)
        agreement_id_str, extracted = matched
        checks["signed_agreement"] = True

        # Step 3: restricted-clause check.
        clean, hits = _evaluate_restricted_clause(extracted)
        if not clean:
            return await _finish(
                False,
                [f"restricted clause present: {', '.join(hits)}"],
                agreement_id_str,
            )
        checks["restricted_clause"] = True

        # Step 4: discount-limit check.
        terms = {field: _unwrap_field(extracted, field) for field in _CEILING_FIELDS}
        within_limit, limit_reason = _evaluate_discount_limit(terms, rebate_dec)
        if not within_limit:
            return await _finish(
                False,
                [limit_reason or "rebate exceeds the signed limit"],
                agreement_id_str,
            )
        checks["discount_limit"] = True

        # Step 5: all checks passed — the ONLY approve path.
        return await _finish(True, [], agreement_id_str)


# ---------------------------------------------------------------------------
# Public entry point — THE AI NEGOTIATOR (advisor, propose-only)
# ---------------------------------------------------------------------------


async def do_suggest_terms(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Propose (never apply) term revisions for one agreement versus a benchmark.

    An ADVISOR action: it reads the agreement's terms, compares them to an
    optional peer ``benchmark`` dict, and appends ONE append-only
    ``v3_cognitive_ledger`` row (kind ``terms_suggestion``).  It MUST NOT mutate
    the AGREEMENT node or any AGREEMENT_TERM — nothing here writes to the graph,
    and ``applied`` is always ``False``.  With no benchmark supplied the
    recommendations list is empty (numbers are never invented).

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "agreement_id": str | UUID,   # required
            "benchmark":    dict | None,  # optional peer terms
        }``

    Returns
    -------
    dict::

        {"status": "ok", "agreement_id": str, "suggestion_id": str,
         "applied": False, "recommendations": [...]}
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(namespace_id)

    agreement_id_raw = params.get("agreement_id")
    if not agreement_id_raw:
        raise ValueError("agreement_id is required")
    agreement_uuid = uuid.UUID(str(agreement_id_raw))

    benchmark = params.get("benchmark")
    if benchmark is not None and not isinstance(benchmark, dict):
        raise ValueError("benchmark must be a JSON object (dict) when provided")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT extracted
            FROM   agreement_review_queue
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            agreement_uuid,
            str(ns_uuid),
        )
        extracted = _coerce_extracted(row["extracted"]) if row is not None else {}
        current_terms = {field: _unwrap_field(extracted, field) for field in _NEGOTIABLE_FIELDS}
        recommendations = _evaluate_term_recommendations(current_terms, benchmark or {})

        payload: dict[str, Any] = {
            "kind": _SUGGESTION_KIND,
            "agreement_id": str(agreement_uuid),
            "benchmark": benchmark,
            "recommendations": recommendations,
            "applied": False,
        }
        suggestion_id = await _append_ledger_entry(conn, ns_uuid, payload)

    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "suggestion_id": suggestion_id,
        "applied": False,
        "recommendations": recommendations,
    }
