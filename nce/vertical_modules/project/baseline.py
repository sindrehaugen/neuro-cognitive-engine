"""
nce/vertical_modules/project/baseline.py
=========================================
Signed-baseline READ accessor + margin-trinity snapshot for the Project engine.

CRITICAL invariant (roadmap §9.1, Contract A)
----------------------------------------------
``SIGNED_BASELINE`` is owned and frozen **ONCE by Sales** in
``sales_signed_baselines``.  This module:

  - READS the Sales-frozen row via the injectable A2A seam
    ``_read_signed_baseline(engine, namespace_id, quote_id)``.
  - WRITES only the ``estimated`` margin dimension (onto the PROJECT node /
    caller-supplied mutable dict).
  - NEVER creates a ``project_signed_baselines`` table.
  - NEVER writes a ``SIGNED_BASELINE`` node.
  - NEVER mutates ``signed`` or ``actual``.

Margin-trinity owners
---------------------
- ``signed``    — Sales (frozen at signature; read-only here).
- ``estimated`` — Project (the ONLY dimension this engine writes).
- ``actual``    — Economy cascade (out of scope for this module; placeholder None).

If the Sales baseline is unavailable (Sales engine not built yet, or no row
for the given quote_id), every function degrades gracefully and returns an
explicit ``"unknown"`` signal — no fabrication, no blocking.

A2A seam
--------
``_read_signed_baseline`` is a module-level coroutine that the test suite
replaces via ``unittest.mock.patch``.  The default implementation raises
``NotImplementedError`` (Sales engine not yet built) so tests that forget
to mock it fail loudly.

Design invariants (uncle-bob-craft)
-------------------------------------
- SRP per function: one job each.
- Dependencies point inward: no web/HTTP/admin imports at module level.
- ``confidence`` only on kg_edges (rule 7); this module writes no graph rows.
- No slow I/O inside ``scoped_pg_session`` transactions.
- Single level of abstraction in ``build_margin_trinity``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

log = logging.getLogger("nce.vertical_modules.project.baseline")

# Sentinel used when the Sales baseline is unavailable.
BASELINE_UNAVAILABLE: str = "unknown"

# Margin dimension keys — used as dict keys in the trinity snapshot.
_DIM_SIGNED: str = "signed"
_DIM_ESTIMATED: str = "estimated"
_DIM_ACTUAL: str = "actual"


# ---------------------------------------------------------------------------
# A2A seam — injectable for tests
# ---------------------------------------------------------------------------


async def _read_signed_baseline(
    engine: Any,
    namespace_id: UUID,
    quote_id: str,
) -> dict[str, Any] | None:
    """Read the Sales-frozen ``sales_signed_baselines`` row for *quote_id*.

    This is the **loose-coupling seam** between Project and Sales.
    At runtime the call is resolved via the generic A2A transport (tool name
    ``sales_get_signed_baseline``).  In tests this function is replaced with a
    mock via ``unittest.mock.patch``.

    The returned dict must have at minimum::

        {
            "id": str,                      # primary key of the signed baseline
            "quote_id": str,               # foreign key back to the QUOTE
            "signed_margin_pct": float,    # signed gross-margin percentage (0–1)
            "signed_total_nok": float,     # total signed value in NOK
            "signed_at": str,              # ISO-8601 timestamp of the signing event
        }

    Returns ``None`` when Sales has no row for this quote_id.

    The Sales engine is NOT built yet.  Until it ships, this function must be
    mocked in tests.  Do NOT add a direct import of any Sales module here.

    A2A tool name (resolved at runtime): ``sales_get_signed_baseline``

    Raises
    ------
    NotImplementedError
        Always (until Sales engine Module 5 ships).  Tests must patch this.
    """
    raise NotImplementedError(
        "_read_signed_baseline: Sales engine (Module 5) is not built yet. "
        "Mock this function in integration tests: "
        "patch('nce.vertical_modules.project.baseline._read_signed_baseline', ...)"
    )


# ---------------------------------------------------------------------------
# Pure domain helpers — zero DB, zero HTTP
# ---------------------------------------------------------------------------


def _clamp_margin_pct(value: Any) -> float:
    """Clamp a raw margin percentage to a finite float.

    Accepts raw values from DB or A2A responses; returns 0.0 on bad input
    rather than propagating NaN/Inf into the trinity snapshot.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")):  # NaN / Inf check
        return 0.0
    return v


def _estimated_margin_pct(
    estimated_cost_nok: float,
    estimated_revenue_nok: float,
) -> float:
    """Compute the estimated gross-margin percentage.

    margin_pct = (revenue - cost) / revenue

    Returns 0.0 when ``estimated_revenue_nok`` is zero (avoids division by
    zero; a project with no revenue has undefined margin, not infinite).
    """
    if estimated_revenue_nok == 0.0:
        return 0.0
    return (estimated_revenue_nok - estimated_cost_nok) / estimated_revenue_nok


def _build_unavailable_trinity() -> dict[str, Any]:
    """Return a trinity snapshot where Sales baseline is unavailable.

    ``signed`` and ``actual`` are both ``BASELINE_UNAVAILABLE`` — no
    fabrication.  ``estimated`` is None because without a signed baseline
    the caller has not supplied cost/revenue figures.
    """
    return {
        _DIM_SIGNED: BASELINE_UNAVAILABLE,
        _DIM_ESTIMATED: None,
        _DIM_ACTUAL: None,
        "signed_baseline_id": None,
        "sales_available": False,
    }


def _build_trinity_from_row(
    signed_row: dict[str, Any],
    estimated_cost_nok: float,
    estimated_revenue_nok: float,
) -> dict[str, Any]:
    """Assemble the margin-trinity snapshot given the Sales-frozen row.

    ``signed`` is taken verbatim from *signed_row* — never overwritten.
    ``estimated`` is computed by this engine from the supplied cost/revenue.
    ``actual`` is None (Economy cascade; out of scope for Project).

    Parameters
    ----------
    signed_row:
        The Sales-frozen ``sales_signed_baselines`` row dict.  Required key:
        ``"signed_margin_pct"`` (float).
    estimated_cost_nok:
        Project's current estimated cost in NOK.
    estimated_revenue_nok:
        Project's current estimated revenue in NOK.
    """
    signed_margin = _clamp_margin_pct(signed_row.get("signed_margin_pct", 0.0))
    estimated_margin = _estimated_margin_pct(estimated_cost_nok, estimated_revenue_nok)

    return {
        _DIM_SIGNED: signed_margin,  # Sales-frozen; read-only
        _DIM_ESTIMATED: estimated_margin,  # Project writes this dimension only
        _DIM_ACTUAL: None,  # Economy cascade; out of scope
        "signed_baseline_id": signed_row.get("id"),
        "sales_available": True,
    }


# ---------------------------------------------------------------------------
# Public: build_margin_trinity
# ---------------------------------------------------------------------------


async def build_margin_trinity(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Read the Sales-frozen baseline and compute the margin-trinity snapshot.

    This is the top-level entry point for ``baseline.py``.  It:

    1. Calls ``_read_signed_baseline`` (A2A seam) to fetch the Sales row.
    2. If unavailable, returns an explicit ``unknown`` trinity (graceful degradation).
    3. If available, computes ``estimated`` from the caller-supplied cost/revenue.
    4. Returns the trinity: ``{signed (read), estimated (Project), actual (None)}``.

    **Invariants enforced here:**
    - ``signed`` is NEVER overwritten — taken verbatim from the Sales row.
    - ``actual`` is NEVER set — Economy's responsibility.
    - No DB write, no graph mutation — this is a READ + COMPUTE only function.

    Parameters
    ----------
    engine:
        NCEEngine instance (passed to the A2A seam; may be a test stub).
    params:
        ``{
            "namespace_id": str | UUID,        # required
            "quote_id": str,                   # required — Sales QUOTE identifier
            "estimated_cost_nok": float,       # required — current estimated cost
            "estimated_revenue_nok": float,    # required — current estimated revenue
        }``

    Returns
    -------
    dict
        ``{
            "signed": float | "unknown",       # Sales-frozen margin pct (read-only)
            "estimated": float | None,         # Project's estimated margin pct
            "actual": None,                    # Economy cascade; always None here
            "signed_baseline_id": str | None,  # id of the sales_signed_baselines row
            "sales_available": bool,           # False when Sales baseline is absent
        }``

    Raises
    ------
    ValueError
        When required params are missing.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("build_margin_trinity: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    quote_id: str = params.get("quote_id", "")
    if not quote_id:
        raise ValueError("build_margin_trinity: 'quote_id' is required in params")

    estimated_cost_nok: float = float(params.get("estimated_cost_nok", 0.0))
    estimated_revenue_nok: float = float(params.get("estimated_revenue_nok", 0.0))

    # Step 1: Fetch Sales-frozen baseline via A2A seam.
    signed_row: dict[str, Any] | None = None
    try:
        signed_row = await _read_signed_baseline(engine, ns_uuid, quote_id)
    except NotImplementedError:
        # Sales engine not yet built — degrade gracefully, never block.
        log.info(
            "build_margin_trinity: Sales engine not available (NotImplementedError) "
            "for quote=%s ns=%s — returning unknown baseline",
            quote_id,
            ns_uuid,
        )
    except Exception:
        log.warning(
            "build_margin_trinity: A2A read failed for quote=%s ns=%s — returning unknown baseline",
            quote_id,
            ns_uuid,
            exc_info=True,
        )

    # Step 2: Graceful degradation when Sales is absent or has no row.
    if signed_row is None:
        log.debug(
            "build_margin_trinity: no Sales baseline for quote=%s ns=%s",
            quote_id,
            ns_uuid,
        )
        return _build_unavailable_trinity()

    # Step 3: Assemble the trinity from the Sales-frozen row + estimated figures.
    trinity = _build_trinity_from_row(
        signed_row,
        estimated_cost_nok=estimated_cost_nok,
        estimated_revenue_nok=estimated_revenue_nok,
    )

    log.info(
        "build_margin_trinity: ns=%s quote=%s signed=%.4f estimated=%.4f",
        ns_uuid,
        quote_id,
        trinity[_DIM_SIGNED],
        trinity[_DIM_ESTIMATED] or 0.0,
    )

    return trinity
