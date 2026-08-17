"""
nce/vertical_modules/agreements/coverage.py
============================================
Coverage-matrix computation for the Agreements vertical module — M3.W4.

Cross-joins Economy General Ledger (GL) data against Agreement terms to detect:
  - ``leakage``  — GL spend for a reconciled vendor that violates or is uncovered
                   by an active agreement term (e.g. exceeds volumeCommitment cap,
                   or no agreement exists at all for the vendor).
  - ``expiry``   — An agreement term whose validTo date is in the past.
  - ``review``   — A term that remains in a non-confirmed review state.

GL data is consumed via the A2A Economy-engine seam ``_read_economy_gl_rows``,
which raises ``NotImplementedError`` until Module 8 ships.  Tests replace the
seam with ``unittest.mock.AsyncMock``.

Identity reconciliation (the crux of false-leakage avoidance)
--------------------------------------------------------------
Vendor identity is reconciled via the C1 entity-resolution primitive
(``nce.entity_resolution.resolver.resolve``).  Both the GL supplier id and the
agreement's ``supplierId`` are resolved to canonical VENDOR kg_nodes rows.

Production VENDOR labels have the form ``Vendor:{identifier}`` (e.g.
``Vendor:912345678``).  pg_trgm similarity between a raw identifier
``"912345678"`` and the label ``"Vendor:912345678"`` is substantially below
0.80, so a naïve score-threshold gate would silently suppress every match.
Instead we adopt the established project pattern from
``nce/vertical_modules/vendors/registry.py``:

  1. Call ``resolve()`` with a permissive gate (``_VENDOR_CANDIDATE_GATE``).
  2. For gated candidates fetch the ``label`` column from ``kg_nodes``.
  3. Confirm identity by exact-matching ``label.split(":")[-1]`` against the
     raw identifier supplied by the caller.  This is an EXACT identity check,
     not a second fuzzy pass — it guards against the prefix inflating scores
     for the wrong node.

A GL row that only carries a fuzzy ``supplier_name`` and no identifier cannot
be confirmed via this exact-suffix path, so it legitimately returns ``None``
(we cannot confirm vendor identity from a fuzzy name alone).

Contract A (ownership / write guard)
-------------------------------------
This module is **read-only**: it never writes to kg_nodes, kg_edges, or any
other table.  It returns a structured result dict.

Design invariants (uncle-bob-craft)
-------------------------------------
- SRP per function: one job each.
- Dependencies point inward: only shared db/session helpers imported, not web.
- ``confidence`` only on kg_edges (rule 7); this module writes nothing.
- Explicit ``namespace_id = $N`` predicate on every SQL query (no RLS-only
  reliance; owner-pool test roles can bypass FORCE RLS).
- Secrets never logged or hard-coded.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.resolver import resolve
from nce.mcp_args import require_namespace_id

log = logging.getLogger("nce.vertical_modules.agreements.coverage")

# Permissive gate for C1 resolve — mirrors the vendor upsert in
# nce/vertical_modules/vendors/registry.py.  After gating, identity is
# confirmed by exact suffix match on the label, so the gate can be loose.
_VENDOR_CANDIDATE_GATE: float = 0.2

# Sentinel used in flags when the GL is unavailable (Economy engine not built).
_GL_UNAVAILABLE: str = "gl_unavailable"


# ---------------------------------------------------------------------------
# A2A seam — Economy engine GL accessor (injectable for tests)
# ---------------------------------------------------------------------------


async def _read_economy_gl_rows(
    engine: Any,
    namespace_id: UUID,
    *,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """Return GL rows for the namespace from the Economy engine.

    This is the **loose-coupling seam** between Agreements and Economy.
    At runtime the call is resolved via the generic A2A transport
    (tool name ``economy_get_gl_records``).  In tests this function is
    replaced via ``unittest.mock.patch``.

    Each returned dict must have at minimum::

        {
            "supplier_name": str,       # raw supplier name from GL
            "supplier_id":  str | None, # optional supplier identifier
            "amount_nok":   float,      # spend amount in NOK
            "gl_date":      str,        # ISO-8601 date (YYYY-MM-DD)
        }

    Returns ``[]`` when there are no rows for the namespace.

    Economy engine (Module 8) is NOT built yet.  Tests MUST patch this.

    A2A tool name (resolved at runtime): ``economy_get_gl_records``

    Raises
    ------
    NotImplementedError
        Always (until Economy engine Module 8 ships).  Tests must patch this.
    """
    raise NotImplementedError(
        "_read_economy_gl_rows: Economy engine (Module 8) is not built yet. "
        "Mock this function in integration tests: "
        "patch('nce.vertical_modules.agreements.coverage._read_economy_gl_rows', "
        "AsyncMock(return_value=[...]))"
    )


# ---------------------------------------------------------------------------
# Pure domain helpers — zero DB, zero HTTP
# ---------------------------------------------------------------------------


def _parse_date_or_none(raw: Any) -> date | None:
    """Coerce a raw DB/JSON value to a ``date``; returns ``None`` on bad input."""
    if raw is None:
        return None
    if isinstance(raw, date):
        # datetime is a subclass of date; normalize to date only.
        return raw.date() if isinstance(raw, datetime) else raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _is_expired(valid_to_raw: Any) -> bool:
    """Return True when the validTo date is in the past (relative to UTC today)."""
    valid_to = _parse_date_or_none(valid_to_raw)
    if valid_to is None:
        return False
    return valid_to < datetime.now(timezone.utc).date()


def _unwrap_field(extracted: dict[str, Any], field: str) -> Any:
    """Return the scalar value for *field* from an extracted JSONB dict.

    Supports both flat (``{"validTo": "2025-01-01"}``) and nested
    (``{"validTo": {"value": "2025-01-01"}}``) shapes.
    """
    v = extracted.get(field)
    if isinstance(v, dict):
        return v.get("value")
    return v


# ---------------------------------------------------------------------------
# DB read helpers — one job each, all namespace-scoped
# ---------------------------------------------------------------------------


async def _fetch_agreements(
    conn: Any,
    ns_uuid: UUID,
) -> list[dict[str, Any]]:
    """Fetch all agreement terms from the review queue for the namespace.

    Queries ``agreement_review_queue`` directly — **no JOIN to kg_nodes**.
    This is critical: AGREEMENT kg_nodes are only written on ``auto_green`` or
    ``confirm`` decisions (see ``nce/admin_handlers/agreements.py:287,360``),
    whereas a review-queue row is inserted for EVERY extraction status
    (``auto_green``, ``needs_review_yellow``, ``manual_red``).  An INNER JOIN
    would silently drop all ``needs_review_yellow``/``manual_red`` rows —
    exactly the rows the ``review`` flag targets.

    Returns a list of dicts, one per row in ``agreement_review_queue``:
    ``{agreement_id, supplier_id, valid_to, volume_commitment, review_status,
    extracted}``
    """
    rows = await conn.fetch(
        """
        SELECT
            arq.agreement_id,
            arq.extracted,
            arq.review_status
        FROM   agreement_review_queue arq
        WHERE  arq.namespace_id = $1::uuid
        """,
        ns_uuid,
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        extracted = row["extracted"]
        if isinstance(extracted, str):
            extracted = json.loads(extracted)
        elif extracted is None:
            extracted = {}

        results.append(
            {
                "agreement_id": row["agreement_id"],
                "supplier_id": _unwrap_field(extracted, "supplierId"),
                "valid_to": _unwrap_field(extracted, "validTo"),
                "volume_commitment": _unwrap_field(extracted, "volumeCommitment"),
                "review_status": row["review_status"],
                "extracted": extracted,
            }
        )
    return results


async def _resolve_vendor_node_id(
    conn: Any,
    ns_uuid: UUID,
    *,
    raw_id: str | None,
) -> UUID | None:
    """Resolve a raw supplier identifier to a canonical VENDOR node_id via C1.

    Adopts the established project pattern from
    ``nce/vertical_modules/vendors/registry.py:78-93``:

    1. Call ``resolve()`` with a permissive score gate
       (``_VENDOR_CANDIDATE_GATE = 0.2``).
    2. For each gated candidate fetch its ``label`` from ``kg_nodes``.
    3. Confirm identity by exact-matching ``label.split(":")[-1]`` against
       ``raw_id`` (both stripped and casefolded).

    This two-step approach avoids the false-negative caused by the ``Vendor:``
    prefix deflating pg_trgm similarity below any useful threshold when the
    raw identifier is compared directly against the full label.

    Returns ``None`` when:
    - ``raw_id`` is absent or empty (cannot confirm identity without it).
    - No candidate's label suffix matches ``raw_id`` exactly.
    """
    if not raw_id:
        return None

    raw_id_norm = raw_id.strip().lower()
    if not raw_id_norm:
        return None

    matches = await resolve(
        conn,
        namespace_id=ns_uuid,
        candidate={"orgnr": raw_id_norm},
        keys=["orgnr"],
        node_type="VENDOR",
    )

    for match in matches:
        if match.score < _VENDOR_CANDIDATE_GATE:
            # Results are ordered by score descending; once below gate, stop.
            break
        row = await conn.fetchrow(
            "SELECT label FROM kg_nodes WHERE id = $1 AND namespace_id = $2",
            match.node_id,
            ns_uuid,
        )
        if not row:
            continue
        label_suffix = row["label"].split(":")[-1].strip().lower()
        if label_suffix == raw_id_norm:
            return match.node_id

    return None


# ---------------------------------------------------------------------------
# Single-abstraction-level helpers for do_coverage_matrix
# ---------------------------------------------------------------------------


def _expiry_review_flags(
    agreements: list[dict[str, Any]],
    agreement_vendor_nodes: dict[str, UUID | None],
) -> list[dict[str, Any]]:
    """Emit expiry and review flags for each agreement term."""
    flags: list[dict[str, Any]] = []
    for ag in agreements:
        ag_id_str = str(ag["agreement_id"])
        ag_vendor_node = agreement_vendor_nodes[ag_id_str]

        if _is_expired(ag["valid_to"]):
            flags.append(
                {
                    "agreement_id": ag_id_str,
                    "flag_type": "expiry",
                    "detail": f"Agreement validTo={ag['valid_to']} is in the past",
                    "gl_supplier_node_id": None,
                    "agreement_supplier_node_id": str(ag_vendor_node) if ag_vendor_node else None,
                }
            )

        if ag["review_status"] in ("needs_review_yellow", "manual_red"):
            flags.append(
                {
                    "agreement_id": ag_id_str,
                    "flag_type": "review",
                    "detail": f"Agreement review_status={ag['review_status']}",
                    "gl_supplier_node_id": None,
                    "agreement_supplier_node_id": str(ag_vendor_node) if ag_vendor_node else None,
                }
            )
    return flags


async def _leakage_flags(
    conn: Any,
    ns_uuid: UUID,
    agreements: list[dict[str, Any]],
    agreement_vendor_nodes: dict[str, UUID | None],
    gl_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Emit leakage flags by cross-joining GL rows against agreement terms."""
    flags: list[dict[str, Any]] = []

    for gl_row in gl_rows:
        gl_node_id = await _resolve_vendor_node_id(
            conn,
            ns_uuid,
            raw_id=gl_row.get("supplier_id"),
        )

        # Find all agreements whose resolved vendor node matches the GL row's node.
        # Identity comparison is on UUID — never on raw strings.
        matching_agreements = [
            ag
            for ag in agreements
            if gl_node_id is not None
            and agreement_vendor_nodes[str(ag["agreement_id"])] == gl_node_id
        ]

        if not matching_agreements:
            # GL spend for a vendor with no reconciled agreement → leakage.
            # Only emit when GL supplier resolved to a node (prevents noise
            # from unregistered suppliers that have no VENDOR node at all).
            if gl_node_id is not None:
                flags.append(
                    {
                        "agreement_id": None,
                        "flag_type": "leakage",
                        "detail": (
                            f"GL spend {gl_row.get('amount_nok')} NOK on "
                            f"{gl_row.get('gl_date')} has no covering agreement "
                            f"for resolved vendor node {gl_node_id}"
                        ),
                        "gl_supplier_node_id": str(gl_node_id),
                        "agreement_supplier_node_id": None,
                    }
                )
            continue

        # Check each matching agreement for volume cap violations.
        amount = float(gl_row.get("amount_nok") or 0.0)
        for ag in matching_agreements:
            cap = ag.get("volume_commitment")
            if cap is not None:
                try:
                    cap_f = float(cap)
                except (ValueError, TypeError):
                    cap_f = None
                if cap_f is not None and amount > cap_f:
                    ag_id_str = str(ag["agreement_id"])
                    flags.append(
                        {
                            "agreement_id": ag_id_str,
                            "flag_type": "leakage",
                            "detail": (
                                f"GL spend {amount} NOK exceeds agreement "
                                f"volumeCommitment cap {cap_f} NOK on "
                                f"{gl_row.get('gl_date')}"
                            ),
                            "gl_supplier_node_id": str(gl_node_id),
                            "agreement_supplier_node_id": str(agreement_vendor_nodes[ag_id_str])
                            if agreement_vendor_nodes[ag_id_str]
                            else None,
                        }
                    )
    return flags


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def do_coverage_matrix(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compute the agreement coverage matrix for a namespace.

    Cross-joins Economy GL rows against Agreement terms.  Vendor identity is
    resolved via the C1 primitive so that matches are made by canonical
    VENDOR node identity, never by raw string comparison.  This is the
    guard that prevents false-leakage flags.

    Parameters
    ----------
    engine:
        NCEEngine instance (passed to the A2A seam; may be a test stub).
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "since_iso":    str | None,   # optional ISO date for GL lookback
        }``

    Returns
    -------
    dict
        ``{
            "status": "ok" | "gl_unavailable",
            "agreements_scanned": int,
            "gl_rows_processed": int,
            "flags": [
                {
                    "agreement_id":  str,
                    "flag_type":     "leakage" | "expiry" | "review",
                    "detail":        str,
                    "gl_supplier_node_id": str | None,
                    "agreement_supplier_node_id": str | None,
                }
            ],
        }``
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = UUID(str(namespace_id))
    since_iso: str | None = params.get("since_iso")

    # Step 1: fetch GL rows via A2A seam (graceful degrade when not built).
    gl_rows: list[dict[str, Any]] = []
    gl_available: bool = True
    try:
        gl_rows = await _read_economy_gl_rows(engine, ns_uuid, since_iso=since_iso)
    except NotImplementedError:
        log.info(
            "do_coverage_matrix: Economy engine not available (NotImplementedError) "
            "ns=%s — leakage detection skipped, expiry+review still computed",
            ns_uuid,
        )
        gl_available = False
    except Exception:
        log.warning(
            "do_coverage_matrix: A2A GL read failed ns=%s — leakage detection skipped",
            ns_uuid,
            exc_info=True,
        )
        gl_available = False

    # Step 2: fetch agreements + resolve identities inside a single scoped session.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        agreements = await _fetch_agreements(conn, ns_uuid)

        # Build a map: agreement_id -> resolved VENDOR node_id.
        # Resolves each agreement's supplierId to a canonical node once.
        agreement_vendor_nodes: dict[str, UUID | None] = {}
        for ag in agreements:
            ag_id_str = str(ag["agreement_id"])
            node_id = await _resolve_vendor_node_id(
                conn,
                ns_uuid,
                raw_id=str(ag["supplier_id"]) if ag["supplier_id"] else None,
            )
            agreement_vendor_nodes[ag_id_str] = node_id

        flags: list[dict[str, Any]] = []

        # Expiry + review flags (do not require GL).
        flags.extend(_expiry_review_flags(agreements, agreement_vendor_nodes))

        # Leakage flags (require GL rows).
        if gl_available:
            flags.extend(
                await _leakage_flags(conn, ns_uuid, agreements, agreement_vendor_nodes, gl_rows)
            )

    return {
        "status": "ok" if gl_available else _GL_UNAVAILABLE,
        "agreements_scanned": len(agreements),
        "gl_rows_processed": len(gl_rows),
        "flags": flags,
    }
