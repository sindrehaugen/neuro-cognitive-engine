"""
nce/vertical_modules/economy/graph.py
======================================
Cognitive-graph upserts + ledger persistence for the Economy vertical module
(Module 8, Wave 6 — graph-postings).

Per ``docs/vertical_engines/08-economy-engine.md`` (B2: "Graph upserts
(INVOICE/POSTING/PERIOD/MARGIN, consume the Procurement boundary edge,
``economy_source_id``). ``economy_postings`` table (RLS, sum=0 guard)") and
``00-ENGINES-ROADMAP.md`` §9.1/§9.2.

Responsibilities (one module, two cooperating halves):
  1. **Graph upserts** — INVOICE / POSTING / PERIOD / MARGIN ``kg_nodes`` rows,
     the ``INVOICE -[recognized_in]-> PERIOD`` and ``PROJECT -[has]-> MARGIN``
     edges, and a READ-ONLY consumer of Procurement's
     ``PO -[posted_to]-> INVOICE`` boundary edge (:func:`find_posted_to_po`).
     Economy never writes ``PO`` or the ``posted_to`` edge — Procurement owns
     both (§9.1 Contract A).
  2. **Ledger persistence** — :func:`persist_financial_event` is the single
     write-path for ``economy_postings``, the balanced double-entry table
     behind the ``POSTING`` node. It takes the *normalised* event
     ``nce.vertical_modules.economy.events.do_emit_financial_event`` already
     validated (postings sum to zero within epsilon, amounts are exact
     ``Decimal``) — that Python-level guard is necessary but not, per this
     wave's brief, sufficient on its own: ``economy_postings`` carries its own
     storage-level sum=0 trigger (migration 048) as a backstop against a
     direct-SQL write or a future bug that bypasses ``do_emit_financial_event``.

Design decisions (uncle-bob-craft: name them)
----------------------------------------------
1. **POSTING node identity = the event's own content hash.** ``do_emit_financial_event``
   already produces a deterministic ``hash`` over every field of the event
   (accounts, signed amounts, approval/invoice/quote ids, …) — see that
   module's docstring. Reusing it as ``event_id`` (and the ``POSTING:{event_id}``
   node label) means a replay of the identical event maps to the identical
   node and the identical ``economy_postings`` rows, with no separate id-
   minting scheme to keep in sync. Two events that are business-distinct
   (different approval, different invoice, …) always hash differently because
   every field is inside the hash body — a collision here is not a modelling
   risk, it is a cryptographic one.
2. **``amount`` is a single signed column, never a debit/credit column pair.**
   The engine's own convention — spelled out in ``ngaap.py``'s
   ``do_compute_bucket_targets`` docstring — is: "a leg's debit/credit
   direction follows the SIGN of its amount". Splitting into two columns
   invites exactly the class of bug this wave's brief calls out by name
   ("Batch 117 shipped a sign error"): a debit recorded in the credit column,
   or vice versa, still "balances" by accident while being financially wrong.
   One signed column makes that mistake a type error, not a silent swap.
3. **The boundary edge is consumed by reading, never by re-deriving it.**
   :func:`find_posted_to_po` is a plain ``SELECT`` against ``kg_edges`` for
   Procurement's ``posted_to`` predicate — mirrors the read-only helper
   pattern already established by ``cascade.py``'s ``_read_signed_baseline``
   / ``_assert_bom_line_exists``. Absence (no PO has posted to this invoice
   yet) returns ``None`` — never fabricated, matching this engine's existing
   "never fabricate a missing baseline" rule (see ``cascade.py``'s
   ``_compute_margin_snapshot``).
4. **Storage-level tolerance matches the application-level epsilon (0.01 NOK),
   not bit-exact zero.** ``persist_financial_event`` quantises each posting
   leg to øre before it reaches ``economy_postings`` — the same reason
   ``cascade.py``'s ``_quantise`` exists: Postgres's ``NUMERIC(18,2)`` column
   would otherwise silently round an unquantised amount on write (migration
   047's fixed bug). Quantising each leg *independently* is a real
   transformation, not a no-op, and can in principle move an
   already-epsilon-approved raw sum by a fraction of an øre. Re-validating
   with the SAME 0.01 tolerance the application guard already uses — rather
   than a stricter exact zero — means the storage backstop still catches a
   genuine break without rejecting a legitimately balanced, already-quantised
   entry. See migration 048's header comment for the DB-trigger half of this.
5. **Every write is namespace-scoped explicitly** (rule 8) — no query here
   relies on RLS alone; ``namespace_id`` is a literal predicate everywhere.
6. **Dependencies point inward.** Only ``asyncpg``, this engine's own
   ``events`` core, and the shared ``nce.entity_resolution.ownership`` /
   ``nce.events.emit`` primitives are imported. No web/HTTP/admin imports —
   this module registers no MCP tool and mounts no REST route (Wave 6 scope).
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, DecimalException, Inexact, localcontext
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write
from nce.vertical_modules.economy.events import UnbalancedPostingsError

log = logging.getLogger("nce.vertical_modules.economy.graph")

# ---------------------------------------------------------------------------
# Engine identifier and node types — must match node-ownership.json entries
# ---------------------------------------------------------------------------
_ECONOMY_ENGINE: str = "economy"

_NODE_TYPE_INVOICE: str = "INVOICE"
_NODE_TYPE_POSTING: str = "POSTING"
_NODE_TYPE_PERIOD: str = "PERIOD"
_NODE_TYPE_MARGIN: str = "MARGIN"

# MARGIN is a PER-DIMENSION node (00-ENGINES-ROADMAP.md §9.1, the margin-trinity
# worked example): 'signed' = Sales-frozen (immutable), 'estimated' = Project,
# 'actual' = Economy cascade. Economy registers and writes ONLY this dimension
# — node-ownership.json has no transition:null row for MARGIN, so a write with
# no transition (or a write by any other engine, including for 'signed'/
# 'estimated') is denied by assert_owner's deny-by-default rule. Never widen
# this to a bare node-type-wide claim; that would silently deny Sales/Project
# their own dimensions.
_MARGIN_TRANSITION_ACTUAL: str = "actual"

# Edge predicates this module WRITES.
_PRED_RECOGNIZED_IN: str = "recognized_in"
_PRED_HAS: str = "has"

# Edge predicate this module only READS — owned and written by Procurement.
_PRED_POSTED_TO: str = "posted_to"

# Money scale for economy_postings.amount (migration 048: NUMERIC(18,2)) — øre,
# 2 dp. Mirrors cascade.py's _quantise / _ORE for the same reason: the code,
# not Postgres's column, decides a third-decimal amount (see module docstring
# point 4).
_ORE: Decimal = Decimal("0.01")

# Working precision for the storage-level re-check sum. Mirrors events.py's
# _SUM_PRECISION — generous enough that any realistic journal adds exactly.
_SUM_PRECISION = 1000

# Same literal 0.01 NOK default as cascade.py's _BALANCE_EPSILON_DEFAULT /
# events.py's documented default tolerance. Never caller-adjustable — the B119
# orchestrator ruling ("a caller must never be able to loosen its own balance
# guard") applies here too.
_BALANCE_EPSILON: Decimal = Decimal("0.01")


# ---------------------------------------------------------------------------
# Label helpers — deterministic, so idempotency holds across re-runs
# ---------------------------------------------------------------------------


def _invoice_label(invoice_id: str) -> str:
    """Canonical kg_nodes label for an INVOICE node."""
    return f"INVOICE:{invoice_id.upper()}"


def _posting_label(event_id: str) -> str:
    """Canonical kg_nodes label for a POSTING node.

    *event_id* is the event's own content hash (lowercase hex) — never
    upper-cased, unlike the other label helpers, because a hash is
    case-sensitive content, not a human identifier.
    """
    return f"POSTING:{event_id}"


def _period_label(period_id: str) -> str:
    """Canonical kg_nodes label for a PERIOD node."""
    return f"PERIOD:{period_id.upper()}"


def _margin_label(quote_id: str) -> str:
    """Canonical kg_nodes label for a MARGIN node.

    Keyed by ``quote_id`` — the same aggregation root ``cascade.py``'s
    margin-trinity snapshot already uses (see that module's
    ``_read_actual_cost_total``), so the MARGIN node for a quote and the
    cascade's own margin computation always agree on which quote they mean.
    """
    return f"MARGIN:{quote_id.upper()}"


def _project_label(quote_id: str) -> str:
    """Canonical label for the PROJECT_PROJECT node this module links to.

    Mirrors ``project/convert.py``'s ``_project_label`` exactly
    (``PROJECT:{quote_id}``) — reimplemented locally rather than imported:
    dependencies point inward, and this is the same four-line-helper
    reimplementation choice ``cascade.py`` already makes for ``ngaap.py``'s
    ``_quantise``. PROJECT_PROJECT is owned by the Project engine; this
    module writes the edge by label only (kg_edges has no FK to kg_nodes),
    never the node itself.
    """
    return f"PROJECT:{quote_id.upper()}"


# ---------------------------------------------------------------------------
# Money coercion — mirrors cascade.py's _quantise / events.py's _sum_amounts
# ---------------------------------------------------------------------------


def _quantise_ore(value: Decimal, where: str) -> Decimal:
    """Round *value* to øre (2 dp), ties away from zero.

    Mirrors ``cascade.py``'s ``_quantise`` (same target scale, same
    ``ROUND_HALF_UP`` convention, same ``DecimalException`` -> ``ValueError``
    translation). Reimplemented locally rather than imported — this module's
    dependencies point inward (see the module docstring), the same reason
    ``cascade.py`` itself gives for not reaching into ``ngaap.py`` for a
    four-line helper.
    """
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: amount is too large to express in øre: {value!r}") from exc


def _sum_exact(amounts: list[Decimal]) -> Decimal:
    """Sum already-quantised øre amounts exactly, or raise.

    Mirrors ``events.py``'s ``_sum_amounts``: raised precision with the
    ``Inexact`` trap armed, so a rounded (i.e. wrong) sum can never be
    silently compared against the tolerance.
    """
    with localcontext() as ctx:
        ctx.prec = _SUM_PRECISION
        ctx.traps[Inexact] = True
        try:
            total = Decimal(0)
            for amount in amounts:
                total += amount
        except DecimalException as exc:
            raise ValueError(
                "persist_financial_event: posting amounts span too many digits to sum "
                "exactly; refusing to compare a rounded sum against the balance tolerance"
            ) from exc
    return total


# ---------------------------------------------------------------------------
# Private upsert helpers — one responsibility each
# ---------------------------------------------------------------------------


async def _upsert_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
    entity_type: str,
    source_id: str | None,
    *,
    transition: str | None = None,
) -> None:
    """Upsert a single kg_nodes row and emit a transactional outbox event.

    Guarded by ``assert_owner`` (deny-by-default when no registry row
    exists — Contract A). ``economy_source_id`` is COALESCEd on conflict so a
    later untagged write never clears an existing tag.

    ``transition`` is ``None`` for the node-type-wide node types (INVOICE /
    POSTING / PERIOD) and ``_MARGIN_TRANSITION_ACTUAL`` for MARGIN — MARGIN is
    registered per-dimension in node-ownership.json (see that constant's
    comment), so its ownership check must name the dimension it writes.
    """
    await assert_owner(conn, namespace_id, entity_type, _ECONOMY_ENGINE, transition=transition)

    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin, economy_source_id)
        VALUES ($1, $2, $3::uuid, 'agent', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type       = EXCLUDED.entity_type,
                change_origin     = 'agent',
                economy_source_id = COALESCE(EXCLUDED.economy_source_id, kg_nodes.economy_source_id),
                updated_at        = NOW()
        """,
        label,
        entity_type,
        str(namespace_id),
        source_id,
    )

    await emit_graph_write(
        conn,
        namespace_id=namespace_id,
        node_type=entity_type,
        op="upserted",
        node_id=label,
    )


async def _upsert_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    subject_label: str,
    predicate: str,
    object_label: str,
    confidence: float,
    source_id: str | None,
) -> None:
    """Upsert a single kg_edges row. ``confidence`` lives on the edge only (rule 7).

    No ownership check — edges have no FK to kg_nodes, so a cross-engine
    subject/object label (e.g. PROJECT, owned by the Project engine) is
    always a safe write (mirrors procurement/graph.py's ``upsert_offers_edge``).
    """
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id,
             change_origin, economy_source_id)
        VALUES ($1, $2, $3, $4, $5::uuid, 'agent', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence        = EXCLUDED.confidence,
                change_origin     = 'agent',
                economy_source_id = COALESCE(EXCLUDED.economy_source_id, kg_edges.economy_source_id),
                updated_at        = NOW()
        """,
        subject_label,
        predicate,
        object_label,
        float(confidence),
        str(namespace_id),
        source_id,
    )


# ---------------------------------------------------------------------------
# Public: node upserts
# ---------------------------------------------------------------------------


async def upsert_invoice_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    invoice_id: str,
    source_id: str | None = None,
) -> str:
    """Upsert an INVOICE node. Returns its label."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    label = _invoice_label(invoice_id)
    await _upsert_node(conn, ns_uuid, label, _NODE_TYPE_INVOICE, source_id)
    return label


async def upsert_posting_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    event_id: str,
    source_id: str | None = None,
) -> str:
    """Upsert a POSTING node — one per balanced ledger entry.

    ``event_id`` is the balanced event's own content hash (see module
    docstring point 1); the SAME id is the natural key
    ``economy_postings.event_id`` uses, so a graph query on
    ``POSTING:{event_id}`` and a SQL query on ``economy_postings`` address
    the exact same ledger entry.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    label = _posting_label(event_id)
    await _upsert_node(conn, ns_uuid, label, _NODE_TYPE_POSTING, source_id)
    return label


async def upsert_period_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    period_id: str,
    source_id: str | None = None,
) -> str:
    """Upsert a PERIOD node (an accounting period / close). Returns its label."""
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    label = _period_label(period_id)
    await _upsert_node(conn, ns_uuid, label, _NODE_TYPE_PERIOD, source_id)
    return label


async def upsert_margin_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    quote_id: str,
    source_id: str | None = None,
) -> str:
    """Upsert a MARGIN node (the margin-trinity snapshot anchor). Returns its label.

    Writes only the ``actual`` dimension — MARGIN is a per-dimension node
    (``signed`` = Sales, ``estimated`` = Project, ``actual`` = Economy; see
    ``_MARGIN_TRANSITION_ACTUAL``'s comment). ``assert_owner`` is called with
    that transition, so this call is denied unless node-ownership.json
    registers Economy for MARGIN/``actual`` specifically.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    label = _margin_label(quote_id)
    await _upsert_node(
        conn, ns_uuid, label, _NODE_TYPE_MARGIN, source_id, transition=_MARGIN_TRANSITION_ACTUAL
    )
    return label


# ---------------------------------------------------------------------------
# Public: edges this module writes
# ---------------------------------------------------------------------------


async def upsert_recognized_in_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    invoice_id: str,
    period_id: str,
    confidence: float = 1.0,
    source_id: str | None = None,
) -> None:
    """Upsert ``INVOICE -[recognized_in]-> PERIOD`` (the periodisering output — B4).

    ``confidence`` defaults to 1.0 — structural, not predictive (mirrors
    ``project/convert.py``'s ``_CONTAINS_CONFIDENCE`` convention for a
    deterministic assignment rather than a scored match).
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    await _upsert_edge(
        conn,
        ns_uuid,
        _invoice_label(invoice_id),
        _PRED_RECOGNIZED_IN,
        _period_label(period_id),
        confidence,
        source_id,
    )


async def upsert_has_margin_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    quote_id: str,
    confidence: float = 1.0,
    source_id: str | None = None,
) -> None:
    """Upsert ``PROJECT -[has]-> MARGIN`` (the margin-trinity snapshot the cascade updates).

    PROJECT_PROJECT (label ``PROJECT:{quote_id}``) is owned by the Project
    engine — this writes the edge by label only, never the PROJECT node
    itself (kg_edges has no FK to kg_nodes, so this cross-engine write is
    always safe; mirrors procurement/graph.py's ``upsert_offers_edge``).
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    await _upsert_edge(
        conn,
        ns_uuid,
        _project_label(quote_id),
        _PRED_HAS,
        _margin_label(quote_id),
        confidence,
        source_id,
    )


# ---------------------------------------------------------------------------
# Public: consuming Procurement's boundary edge (READ-ONLY)
# ---------------------------------------------------------------------------


async def find_posted_to_po(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    invoice_id: str,
) -> str | None:
    """READ-ONLY: the PO subject label of the ``PO -[posted_to]-> INVOICE``
    boundary edge Procurement wrote for *invoice_id*, or ``None``.

    Procurement owns the ``PO`` node and the ``posted_to`` edge (§9.1
    Contract A / ``docs/vertical_engines/01-procurement-engine.md``); this
    function never writes either — it is the "consume, don't re-derive" half
    of this wave's Step 1. Absence (Procurement has not posted a PO to this
    invoice yet) returns ``None`` rather than raising or fabricating a PO —
    same "never fabricate" rule ``cascade.py``'s margin-trinity already
    follows for a missing signed baseline.

    Namespace-scoped explicitly (rule 8) — never relies on RLS alone.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    invoice_label = _invoice_label(invoice_id)
    return await conn.fetchval(
        """
        SELECT subject_label FROM kg_edges
        WHERE object_label = $1 AND predicate = $2 AND namespace_id = $3::uuid
        """,
        invoice_label,
        _PRED_POSTED_TO,
        str(ns_uuid),
    )


async def upsert_invoice_from_procurement(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    invoice_id: str,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Upsert the INVOICE node and report the PO that posted to it, if any.

    Composes :func:`upsert_invoice_node` (write) with :func:`find_posted_to_po`
    (read) — each does exactly one job (SRP); this is the orchestration point
    where Step 1's "consume the Procurement boundary edge" actually happens.

    Returns
    -------
    dict
        ``{"invoice_label": str, "po_label": str | None}`` — ``po_label`` is
        ``None`` when no ``posted_to`` edge exists yet for this invoice.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    invoice_label = await upsert_invoice_node(
        conn, ns_uuid, invoice_id=invoice_id, source_id=source_id
    )
    po_label = await find_posted_to_po(conn, ns_uuid, invoice_id)
    return {"invoice_label": invoice_label, "po_label": po_label}


# ---------------------------------------------------------------------------
# Public: economy_postings — the single ledger write-path
# ---------------------------------------------------------------------------


async def persist_financial_event(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    event: dict[str, Any],
    *,
    period_id: str | None = None,
    source_id: str | None = None,
) -> int:
    """Persist one balanced financial event's postings into ``economy_postings``.

    *event* MUST be the normalised dict returned by
    ``nce.vertical_modules.economy.events.do_emit_financial_event`` — i.e. it
    already carries a stable content ``hash`` and a ``postings`` list whose
    ``amount`` values are already exact ``Decimal`` (never raw caller input;
    that boundary belongs to ``do_emit_financial_event``, not here).

    No postings (absent/``None``/empty) means no bookkeeping obligation — 0
    rows written, matching ``do_emit_financial_event``'s own "no postings = no
    event" convention all the way to storage.

    Idempotent: natural key ``(namespace_id, event_id, line_no)``; a replay
    of the exact same event (same hash, same posting order — order is never
    normalised, see ``events.py``) is a no-op via ``ON CONFLICT DO NOTHING``,
    never ``DO UPDATE`` — the same "fail toward review, never toward
    looseness" discipline as ``economy_bom_actual_costs`` (migration 047).
    All lines for one event are inserted in a SINGLE statement (via
    ``unnest``) so migration 048's storage-level sum=0 trigger — which fires
    once per statement — validates the complete set at once, not one row at
    a time.

    Also upserts the POSTING node for this event (see module docstring
    point 1) — the graph and the ledger share one identity by construction.

    Parameters
    ----------
    period_id:
        Optional accounting period tag applied to every persisted line.
    source_id:
        Optional ``economy_source_id`` provenance tag.

    Returns
    -------
    int
        Number of NEW rows actually inserted (0 for an empty/absent postings
        list, or for a full replay where every line already exists).

    Raises
    ------
    ValueError
        *event* is missing ``hash``/``type``, or a posting is malformed / its
        ``amount`` is not already an exact ``Decimal`` (a raw, un-normalised
        event was passed by mistake).
    UnbalancedPostingsError
        The postings — after quantising each leg to øre — no longer sum to
        zero within the 0.01 NOK tolerance (see module docstring point 4).
        This is the Python-level half of the storage backstop; migration
        048's DB trigger is the other half.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    postings = event.get("postings")
    if not postings:
        return 0

    event_hash = event.get("hash")
    if not isinstance(event_hash, str) or not event_hash.strip():
        raise ValueError(
            "persist_financial_event: event['hash'] is required — pass the NORMALISED "
            "event returned by do_emit_financial_event, not raw input"
        )
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("persist_financial_event: event['type'] is required")

    line_numbers: list[int] = []
    accounts: list[str] = []
    amounts: list[Decimal] = []
    for index, posting in enumerate(postings):
        if not isinstance(posting, dict):
            raise ValueError(f"persist_financial_event: postings[{index}] must be an object")
        account = posting.get("account")
        if not isinstance(account, str) or not account.strip():
            raise ValueError(
                f"persist_financial_event: postings[{index}].account must be a non-empty string"
            )
        raw_amount = posting.get("amount")
        if not isinstance(raw_amount, Decimal):
            raise ValueError(
                f"persist_financial_event: postings[{index}].amount must already be an "
                f"exact Decimal (got {type(raw_amount).__name__}) — pass the postings list "
                f"do_emit_financial_event already normalised, never raw caller input"
            )
        line_numbers.append(index)
        accounts.append(account)
        amounts.append(_quantise_ore(raw_amount, f"postings[{index}].amount"))

    # Re-validate AFTER quantising to øre — see module docstring point 4:
    # quantising each leg independently is a real transformation and can in
    # principle push an already-epsilon-approved raw sum outside tolerance.
    total = _sum_exact(amounts)
    if total.copy_abs() > _BALANCE_EPSILON:
        raise UnbalancedPostingsError(event_type, total, postings, _BALANCE_EPSILON)

    await upsert_posting_node(conn, ns_uuid, event_id=event_hash, source_id=source_id)

    status = await conn.execute(
        """
        INSERT INTO economy_postings
            (namespace_id, event_id, event_type, line_no, account, amount,
             period_id, economy_source_id, change_origin)
        SELECT $1::uuid, $2, $3, u.line_no, u.account, u.amount, $7, $8, 'agent'
        FROM unnest($4::int[], $5::text[], $6::numeric[]) AS u(line_no, account, amount)
        ON CONFLICT (namespace_id, event_id, line_no) DO NOTHING
        """,
        str(ns_uuid),
        event_hash,
        event_type,
        line_numbers,
        accounts,
        amounts,
        period_id,
        source_id,
    )
    # asyncpg returns "INSERT 0 N" — parse the actual-rows-inserted count.
    try:
        return int(status.split()[-1])
    except (AttributeError, ValueError, IndexError):  # pragma: no cover - defensive
        return 0
