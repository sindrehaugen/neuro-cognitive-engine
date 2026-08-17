"""
nce/vertical_modules/economy/cascade.py
=========================================
``do_cascade_on_approval`` — the 7-effect Stage-2 approval cascade (B2).

Lifted as a **pattern** (not a transliteration) from Andreas's
``lib/finance/cascade/supplier-invoice-approved.ts:cascadeOnApproval`` (546 LOC;
tests: ``tests/finance/supplier-invoice-cascade.test.ts``). Per
``docs/vertical_engines/08-economy-engine.md`` (core function
``do_cascade_on_approval``, Build phase B2, Review round-2 #2/#7) and
``00-ENGINES-ROADMAP.md`` §9.1 (the ``BOM_LINE`` lifecycle state-machine).

Depends on Wave 3's balance guarantee: every posting routes through
``nce.vertical_modules.economy.events.do_emit_financial_event`` (sum=0 ±epsilon
at write time, or ``UnbalancedPostingsError`` — see that module for the full
Decimal/NaN/bool boundary discipline this cascade inherits for free).

§9.1 CONTRACTS — binding, a violation here is a failed wave even with green tests
------------------------------------------------------------------------------
1. **This cascade is the ONLY writer of ``BOM_LINE.actual_cost`` and writes
   NOTHING else on the line.** It must NOT advance ``BOM_LINE.status``
   (Warehouse owns ``DELIVERED``; Field Tech owns ``INSTALLED``/``TESTED``).
   Andreas's reference *does* advance ``bom.orderStatusEnum`` in this cascade
   (see ``advanceToAtLeast`` / the ``bom.line.status.advanced`` effect in the
   reference) — the wave names that a **known audit bug** and this port does
   not copy it. ``BOM_LINE`` has no ``status`` column at all in this schema;
   status is represented as a ``BOM_LINE -[has_status]-> STATUS:*`` kg_edge
   (see ``project/tasks.py``), so "does not advance status" here means: this
   module writes **zero** ``kg_nodes``/``kg_edges`` rows of any kind. The only
   DB writes in this whole module are against the Economy-owned
   ``economy_bom_actual_costs`` table (migration 047) — because ``kg_nodes``
   has no payload column to hold a numeric ``actual_cost`` field (the same
   reason ``system_design_device_capabilities`` and ``procurement_bid_prices``
   exist as their own tables rather than as kg_nodes columns).
2. **``marginSignedPct`` (``sales_signed_baselines.signed_margin_pct``) is
   Sales-frozen and immutable.** This cascade READS the signed baseline row
   (both ``signed_margin_pct`` and ``signed_total_nok`` — the margin-trinity
   snapshot needs a revenue basis, not just the frozen percentage) and never
   writes ``sales_signed_baselines`` in any way — no ``UPDATE``, no
   ``INSERT``, no ``DELETE``.
3. **One transaction, all 7 effects.** ``scoped_pg_session`` (``nce.db_utils``)
   wraps the whole yielded block in ``conn.transaction()``; an exception
   raised anywhere inside it — in particular ``UnbalancedPostingsError`` from
   any effect's posting — rolls back every write already issued in this call,
   including ``actual_cost`` writes for BOM lines processed earlier in the
   same ``lines`` list. No partial write.
4. **Idempotent — by constraint, not by a guard that has to stay correct.**
   ``economy_bom_actual_costs`` is keyed ``UNIQUE (namespace_id,
   bom_line_label, source_approval_id)`` — ONE ROW PER (line, approval).
   :func:`_upsert_actual_cost` issues ``INSERT ... ON CONFLICT ... DO
   NOTHING``, so a replay of the SAME approval against the SAME line cannot
   produce a second row: the constraint refuses it outright. A DIFFERENT
   approval against the same line is a DIFFERENT conflict target, so it lands
   as its own row instead of colliding — partial delivery and split invoicing
   against one BOM line are ordinary, not an error (the roadmap's own
   Inventory language, "partial-GR vs ``BOM_LINE.DELIVERED``", assumes
   exactly this). The line's actual cost is therefore never a single scalar:
   it is ``SUM(actual_cost)`` grouped by ``(namespace_id, bom_line_label)``
   (see :func:`_read_actual_cost_total`). A replay under the same key with a
   DIFFERENT ``actual_cost`` is refused (``ValueError``) rather than silently
   applied — "fail toward review, never toward looseness" (money-module
   briefing); see :func:`do_cascade_on_approval`'s post-conflict read-back.

   **Round-1 history (do not repeat):** the first cut of this table used the
   natural key ``(namespace_id, bom_line_label)`` with ``ON CONFLICT ... DO
   UPDATE SET actual_cost = EXCLUDED.actual_cost`` — a plain replace. A
   second approval against the same line silently overwrote the first
   instead of adding to it, because the replay guard only compared against
   ``source_approval_id`` of the *last* writer. Reproduced live: approval A
   (60 000,00) then approval B (40 000,00) left the row at 40 000,00, not
   100 000,00 — understating incurred cost and inflating reported margin.
   **Do NOT "fix" this by accumulating (``SET actual_cost = existing +
   EXCLUDED``) with the same single-last-id guard** — ``source_approval_id``
   can only remember the LAST writer, so approvals A, B, then a replay of A
   would find ``existing.source_approval_id == B``, fall through the guard,
   and add A's amount a second time. Accumulate-plus-single-last-id-guard is
   not idempotent under interleaving. One row per (line, approval) is.
5. **Single BOM write-path.** :func:`_upsert_actual_cost` is the only
   function in this module — and, by the §9.1 field-ownership rule, in the
   whole codebase — that writes ``economy_bom_actual_costs``.
6. **GREEN = auto-ELIGIBLE, not auto-POSTED.** This function still only runs
   on explicit Stage-2 approval; it performs no Stage-2 gating decision itself
   (that lives in the caller — REST/MCP surface, out of this wave's scope).

The 7 effects (docs/08-economy-engine.md B2 + the reference's own numbering)
-----------------------------------------------------------------------------
1. ``economy.invoice.approved`` — the invoice-level GL postings (COGS debit +
   VAT-input debit + AP credit, caller-supplied via ``invoice_postings``).
   This is the only effect whose postings are provided at the *whole-call*
   level; it is validated **before any DB write**, so a malformed invoice-level
   posting never reaches the ledger and no BOM line gets touched.
2. ``economy.bom_line.cost_updated`` — one per line in ``params["lines"]``;
   this is the actual ``actual_cost`` write (:func:`_upsert_actual_cost`). A
   line MAY carry its own ``postings`` (e.g. a per-line COGS split); an
   unbalanced per-line posting raises and rolls back the whole transaction,
   including writes already issued for earlier lines in the same call — this
   is what makes contract 3 ("no partial write") a real, testable guarantee
   rather than a trivial "validate-before-any-write" case.
3–7. ``economy.project.margin_recalculated`` / ``..supplier_scorecard_updated``
   / ``..kickback_accrued`` / ``..delivery_recalculated`` / ``..cashflow_reprojected``
   — portal-internal recalculation snapshots, one each per call. None of the
   5 carries ``postings`` (events.py's documented "no postings = no balance
   obligation" case — the same category as the reference's own margin/
   scorecard/kickback events), so they always pass the balance guard
   trivially. But — unlike a bare ``{"postings": None}`` — each now carries
   the SAME computed content the reference computes inline for the same
   5 events (margin before/after, supplier/invoice/trigger fields; see
   :func:`_compute_margin_snapshot` and the per-effect blocks in
   :func:`do_cascade_on_approval`). This is *event payload*, so it needs no
   projection table: the reference's own v1 note ("events emitted with
   computed payloads but projection writes land in Phase 1c") describes
   payload-plus-no-table, and that is exactly what this port now delivers —
   materialising a persisted margin/cashflow *table* remains Wave 6's scope,
   untouched here.

Deliberate simplification vs. the reference (uncle-bob-craft: name it)
--------------------------------------------------------------------------
The reference loops over every ``affectedProjectId`` derived from the
invoice's matched lines and fires effects 3–7 **once per project**. This port
takes a single ``project_id`` per call instead of deriving a set of affected
projects from the lines. This is a scope choice, not an oversight: deriving
"which project owns this BOM line" would mean querying
``PROJECT -[contains]-> BOM_LINE`` edges (introducing a new read dependency on
Project's graph shape) for a value this wave's acceptance test does not
exercise. A multi-project invoice is handled by calling this function once per
affected project — a natural extension, not a design change. For the same
reason, the margin-trinity snapshot aggregates ``actual_cost`` by
**quote_id** (the ``BOM_LINE:{QUOTE}:`` label-prefix lookup already
established by ``sales/dealroom.py`` and ``project/convert.py`` — BOM_LINE
labels encode quote_id, not project_id — but matched here via a literal
``starts_with`` prefix test, not ``LIKE``; see :func:`_read_actual_cost_total`)
rather than by project.

Design invariants (uncle-bob-craft)
-------------------------------------
- SRP per effect/helper: one job each (``_as_lines`` parses, ``_upsert_actual_cost``
  writes, ``_read_actual_cost_total``/``_read_signed_baseline`` read,
  ``_compute_margin_snapshot`` computes, ``do_cascade_on_approval`` orchestrates).
- Dependencies point inward: only ``asyncpg``, ``nce.db_utils``, and this
  engine's own ``events`` core. No web/HTTP/admin imports.
- Centralised BOM-cost write: exactly one function issues the write: no
  second write path exists anywhere else in this module or this engine.
- ``confidence`` lives on ``kg_edges`` only — moot here since this module
  writes no kg rows at all.
- Money never crosses a ``float`` boundary: :func:`_as_actual_cost` mirrors
  ``events.py``'s ``_as_decimal`` boundary (bool-before-int, NaN/inf rejected,
  ``str`` never parsed, ``float`` via ``Decimal(str(x))``), then quantises to
  øre via :func:`_quantise` (mirrors ``ngaap.py``'s own ``_quantise`` — same
  scale, same ``ROUND_HALF_UP`` rounding) so the code, not Postgres's
  ``NUMERIC(18,2)`` column, decides a third-decimal amount. Negative amounts
  are legal — see :func:`_as_actual_cost`'s sign-convention note.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.economy.events import do_emit_financial_event

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.economy.cascade")

# Balance tolerance (NOK). Same call-site literal convention already used by
# mcp_handlers.py / admin_handlers/economy.py: the B119 orchestrator ruling
# established that this engine adds no ``NCE_ECONOMY_BALANCE_EPSILON`` config
# key yet — epsilon is never caller-supplied (money-module briefing: a caller
# must never be able to loosen its own balance guard).
_BALANCE_EPSILON_DEFAULT: float = 0.01

_BOM_LINE_ENTITY_TYPE: str = "BOM_LINE"

# Money scale for this table's ``actual_cost`` column (migration 047:
# ``NUMERIC(18,2)``) — øre, 2 dp. See :func:`_quantise`.
_ORE: Decimal = Decimal("0.01")

# The 7 effect event types.
_EFFECT_INVOICE_APPROVED: str = "economy.invoice.approved"
_EFFECT_BOM_LINE_COST_UPDATED: str = "economy.bom_line.cost_updated"
_EFFECT_MARGIN_RECALCULATED: str = "economy.project.margin_recalculated"
_EFFECT_SCORECARD_UPDATED: str = "economy.project.supplier_scorecard_updated"
_EFFECT_KICKBACK_ACCRUED: str = "economy.project.kickback_accrued"
_EFFECT_DELIVERY_RECALCULATED: str = "economy.project.delivery_recalculated"
_EFFECT_CASHFLOW_REPROJECTED: str = "economy.project.cashflow_reprojected"


# ---------------------------------------------------------------------------
# Coercion boundary — mirrors events.py's discipline (bool-before-int,
# NaN/inf rejected, str never parsed).
# ---------------------------------------------------------------------------


def _as_ns_uuid(raw: Any, field: str) -> UUID:
    if not raw:
        raise ValueError(f"do_cascade_on_approval: '{field}' is required")
    return UUID(str(raw)) if not isinstance(raw, UUID) else raw


def _quantise(value: Decimal, where: str) -> Decimal:
    """Round *value* to øre (2 dp), ties away from zero.

    Mirrors ``ngaap.py``'s ``_quantise`` — same target scale (``_ORE`` /
    ``Decimal("0.01")``), same rounding mode (``ROUND_HALF_UP`` — the
    Norwegian accounting convention this engine already committed to for
    periodisering), same ``DecimalException`` -> ``ValueError`` translation.
    Reimplemented locally rather than imported: this module's dependencies
    point inward (``asyncpg``, ``nce.db_utils``, this engine's own
    ``events`` core — see the module docstring), so it does not reach across
    to ``ngaap.py`` for a four-line helper; ``forecast.py`` makes the same
    choice for the same reason.

    Round 2 shipped migration 047's ``actual_cost NUMERIC(18,2)`` column
    with nothing quantising in code, so a third-decimal input (e.g.
    ``100.005``) reached Postgres un-rounded and Postgres rounded it
    silently on write — confirmed live: ``100.005::numeric(18,2) ->
    100.01``, ``100.004::numeric(18,2) -> 100.00``, both with no error and
    no signal to the caller that anything happened. This module now
    quantises to øre explicitly, at this coercion boundary, exactly once —
    deliberately "quantise", not "reject anything finer than øre": øre
    precision is this table's chosen scale (like every other quantised
    money amount in this engine), a third-decimal input is an ordinary
    rounding case, not a caller error, and rounding it the same way
    ``ngaap.py`` already rounds money keeps one rounding convention across
    the whole Economy engine. The DB column's scale therefore agrees with
    the code by construction: every ``Decimal`` that reaches
    ``economy_bom_actual_costs.actual_cost`` is already at 2 dp, so
    Postgres's own rounding is never actually invoked.
    """
    try:
        return value.quantize(_ORE, rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"{where}: amount is too large to express in øre: {value!r}") from exc


def _as_actual_cost(value: Any, where: str) -> Decimal:
    """Coerce one line's ``actual_cost`` to an exact ``Decimal``, quantised to
    øre, or raise.

    Same boundary rules as ``events.py``'s ``_as_decimal``: ``bool`` rejected
    before ``int`` (``isinstance(True, int)`` is ``True`` in Python), NaN/inf
    rejected (a NaN cost must not silently enter the ledger as zero-ish), and
    a ``str`` amount is never parsed here — an upstream ingest bug must not be
    guessed at inside the cascade that writes the number. The resulting
    ``Decimal`` is then quantised to øre via :func:`_quantise` — see that
    function's docstring for why this now rounds (rather than rejects) a
    finer-than-øre input, and for the live proof of the silent-rounding bug
    this closes.

    Sign convention: **negative is legal and meaningful.** A supplier credit
    note against a BOM line is a legitimate negative ``actual_cost`` row (one
    more row under the line's ``(namespace_id, bom_line_label)`` group,
    summed like any other — see :func:`_read_actual_cost_total`). There is no
    ``>= 0`` check here and none should be added: rejecting negative amounts
    would make crediting a mischarged line impossible through this cascade.
    Only non-finite (NaN/±inf) and non-numeric values are refused.
    """
    if isinstance(value, bool):
        raise ValueError(f"{where}: bool is not a cost amount (got {value!r})")
    if isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / +-Inf
            raise ValueError(f"{where}: amount must be finite (got {value!r})")
        decimal_value = Decimal(str(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{where}: amount must be finite (got {value!r})")
        decimal_value = value
    else:
        raise ValueError(f"{where}: expected int/float/Decimal, got {type(value).__name__}")
    return _quantise(decimal_value, where)


def _as_optional_amount(value: Any, where: str) -> Decimal | None:
    """Same boundary as :func:`_as_actual_cost`, but absent/``None`` is legal —
    an optional caller-supplied figure (e.g. ``invoice_amount``), not a
    required line cost."""
    if value is None:
        return None
    return _as_actual_cost(value, where)


def _as_lines(raw: Any) -> list[dict[str, Any]]:
    """Normalise ``params['lines']``. Absent/``None`` -> no lines (an invoice
    approval that matched zero BOM lines is a legal, if unusual, call — it
    mirrors the reference's own empty-``affectedProjectIds`` case). Anything
    present must be a list of well-formed line objects; a malformed entry
    raises rather than being silently skipped (a lost line is a lost cost)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("do_cascade_on_approval: 'lines' must be a list")

    lines: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"do_cascade_on_approval: lines[{index}] must be an object")
        label = str(entry.get("bom_line_label") or "").strip()
        if not label:
            raise ValueError(f"do_cascade_on_approval: lines[{index}].bom_line_label is required")
        if "actual_cost" not in entry:
            raise ValueError(f"do_cascade_on_approval: lines[{index}].actual_cost is required")
        actual_cost = _as_actual_cost(entry["actual_cost"], f"lines[{index}].actual_cost")
        lines.append(
            {
                "bom_line_label": label,
                "actual_cost": actual_cost,
                "postings": entry.get("postings"),
            }
        )
    return lines


# ---------------------------------------------------------------------------
# DB helpers — one responsibility each
# ---------------------------------------------------------------------------


async def _assert_bom_line_exists(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    bom_line_label: str,
) -> None:
    """Refuse to write an actual-cost row for a label with no BOM_LINE node.

    Read-only (a plain ``SELECT``) — this is a safety check, not a write, and
    does not conflict with "this module writes zero kg_nodes/kg_edges rows".
    Explicit namespace_id filter (never rely on RLS alone for this check).
    """
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM kg_nodes
            WHERE label = $1 AND entity_type = $2 AND namespace_id = $3::uuid
        )
        """,
        bom_line_label,
        _BOM_LINE_ENTITY_TYPE,
        str(ns_uuid),
    )
    if not exists:
        raise ValueError(
            f"do_cascade_on_approval: no BOM_LINE node found for {bom_line_label!r} in "
            f"namespace {ns_uuid} — refusing to write an orphan actual_cost row"
        )


async def _read_existing_cost_row(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    bom_line_label: str,
    approval_id: str,
) -> asyncpg.Record | None:  # type: ignore[type-arg]
    """The row for this EXACT ``(namespace_id, bom_line_label,
    source_approval_id)`` key, or ``None``.

    Filtered on all three natural-key columns. Under the one-row-per-approval
    schema, ``(namespace_id, bom_line_label)`` alone can match more than one
    row (one per approval that has ever posted against this line), so a
    two-column read would return an arbitrary one of them instead of the
    specific approval this call is replaying.
    """
    return await conn.fetchrow(
        """
        SELECT actual_cost, source_approval_id
        FROM economy_bom_actual_costs
        WHERE namespace_id = $1::uuid AND bom_line_label = $2 AND source_approval_id = $3
        """,
        str(ns_uuid),
        bom_line_label,
        approval_id,
    )


async def _upsert_actual_cost(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    bom_line_label: str,
    actual_cost: Decimal,
    approval_id: str,
) -> bool:
    """The SINGLE write-path for ``BOM_LINE.actual_cost`` (§9.1).

    ONE ROW PER (line, approval), never a replace. ``ON CONFLICT
    (namespace_id, bom_line_label, source_approval_id) DO NOTHING`` — a
    replay of the SAME approval against the SAME line is a no-op *by
    construction*: the unique constraint refuses the duplicate row outright,
    so idempotency does not depend on a guard that has to read first and
    decide correctly (that is exactly what the round-1 design got wrong —
    see the module docstring's "Round-1 history" note; accumulating instead
    of replacing does not fix it either, because a single
    ``source_approval_id`` column cannot remember more than the last writer).

    A DIFFERENT approval against the SAME line is a DIFFERENT conflict
    target, so it lands as a brand-new row instead of colliding with the
    first — partial delivery and split invoicing against one BOM line are
    ordinary, not an error. The line's total actual cost is read back as
    ``SUM(actual_cost)`` — see :func:`_read_actual_cost_total` — never as a
    single row's value.

    Returns ``True`` if a new row was actually inserted, ``False`` if ``ON
    CONFLICT ... DO NOTHING`` fired (this exact key already has a row). The
    caller uses the ``False`` case to decide REPLAY-no-op vs REFUSE by
    reading the existing row back and comparing ``actual_cost`` — see
    :func:`do_cascade_on_approval`. Reading back only AFTER the conflict is
    confirmed (rather than checking-then-writing) is what keeps the refusal
    correct even under a concurrent race for the same key: the loser of the
    INSERT race reads the winner's already-committed row, not a stale
    pre-write snapshot.
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO economy_bom_actual_costs
            (namespace_id, bom_line_label, actual_cost, source_approval_id)
        VALUES ($1::uuid, $2, $3, $4)
        ON CONFLICT (namespace_id, bom_line_label, source_approval_id) DO NOTHING
        RETURNING 1
        """,
        str(ns_uuid),
        bom_line_label,
        actual_cost,
        approval_id,
    )
    return inserted is not None


async def _read_actual_cost_total(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    quote_id: str,
) -> Decimal:
    """``SUM(actual_cost)`` across every BOM line under *quote_id*.

    One row per (line, approval) now that :func:`_upsert_actual_cost` inserts
    rather than replaces, so a line's real cost is never a single stored
    scalar — it is this grouped sum. The label prefix ``BOM_LINE:{QUOTE}:``
    is the established quote-to-BOM-lines lookup already used by
    ``sales/dealroom.py`` and ``project/convert.py``, but unlike those call
    sites the aggregation root here is a caller-supplied ``quote_id`` used
    directly as the margin-trinity's cost basis — so this MUST be a literal
    prefix test, never SQL ``LIKE``. ``_`` and ``%`` are ordinary LIKE
    metacharacters (``_`` matches any single character), and a quote id
    containing an underscore is entirely unremarkable; against a raw ``LIKE``
    pattern, ``quote_id='Q_1'`` would ALSO match a completely different quote
    ``QA1`` (confirmed live: ``'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%'`` is
    true), silently summing another quote's costs into this quote's
    ``actual_margin_pct``/``margin_variance_pct`` with no error. ``starts_with``
    is a plain literal-substring test with no pattern semantics at all, so no
    ``quote_id`` — underscore, percent, or otherwise — can ever be crafted to
    widen the match; this closes the bug class rather than relying on every
    future edit remembering to escape it. Read inside the same transaction as
    this call's own writes, so a just-applied invoice is reflected in the
    same-transaction margin snapshot. A credit note (a negative
    ``actual_cost`` row) reduces this sum like any other row — ``SUM`` has no
    sign opinion.
    """
    total = await conn.fetchval(
        """
        SELECT COALESCE(SUM(actual_cost), 0)
        FROM economy_bom_actual_costs
        WHERE namespace_id = $1::uuid AND starts_with(bom_line_label, $2)
        """,
        str(ns_uuid),
        f"BOM_LINE:{quote_id.upper()}:",
    )
    return total if isinstance(total, Decimal) else Decimal(total)


async def _read_signed_baseline(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    quote_id: str,
) -> asyncpg.Record | None:  # type: ignore[type-arg]
    """READ-ONLY: the Sales-frozen signed baseline (§9.1 margin-trinity).

    Returns ``None`` when Sales has no signed baseline for *quote_id* yet
    (graceful degradation — never fabricated). Reads BOTH
    ``signed_margin_pct`` and ``signed_total_nok``: the margin-trinity
    snapshot needs a revenue basis to turn ``actual_cost`` into an actual
    margin, not just the frozen percentage alone. This function issues no
    write of any kind against ``sales_signed_baselines`` — reading two
    columns instead of one does not change that.
    """
    return await conn.fetchrow(
        """
        SELECT signed_margin_pct, signed_total_nok FROM sales_signed_baselines
        WHERE namespace_id = $1::uuid AND quote_id = $2
        """,
        str(ns_uuid),
        quote_id,
    )


def _compute_margin_snapshot(
    signed_margin_pct: Decimal | None,
    signed_total_nok: Decimal | None,
    actual_cost_total: Decimal,
) -> dict[str, Decimal | None] | None:
    """The margin-trinity: signed (Sales-frozen baseline) vs actual (this
    cascade's own newly-summed ``actual_cost``) vs their variance. Pure
    arithmetic — no DB, no write.

    Mirrors the reference's ``computeMarginSnapshot``/``marginPctBefore``
    /``marginPctAfter`` pairing, but where the reference's v1 leaves
    before == after (a documented TODO — "same snapshot, recompute once BOM
    cost projection lands"), this port already has the actual cost (the very
    rows this cascade just wrote/read), so "before" (signed) and "after"
    (actual) are genuinely different numbers here.

    Returns ``None`` when Sales has no signed baseline for this quote yet —
    never a fabricated zero margin (this repo has shipped a
    fabricated-baseline money bug before). ``actual_margin_pct`` and
    ``margin_variance_pct`` are additionally ``None`` when
    ``signed_total_nok`` is exactly zero (no revenue basis to divide by);
    every other field in the trinity is still reported.
    """
    if signed_margin_pct is None or signed_total_nok is None:
        return None

    signed_margin_amount = signed_total_nok * signed_margin_pct
    actual_margin_amount = signed_total_nok - actual_cost_total
    actual_margin_pct = (actual_margin_amount / signed_total_nok) if signed_total_nok != 0 else None
    margin_variance_amount = actual_margin_amount - signed_margin_amount
    margin_variance_pct = (
        (actual_margin_pct - signed_margin_pct) if actual_margin_pct is not None else None
    )
    return {
        "signed_margin_pct": signed_margin_pct,
        "signed_margin_amount": signed_margin_amount,
        "actual_margin_pct": actual_margin_pct,
        "actual_margin_amount": actual_margin_amount,
        "margin_variance_pct": margin_variance_pct,
        "margin_variance_amount": margin_variance_amount,
    }


# ---------------------------------------------------------------------------
# Public: do_cascade_on_approval
# ---------------------------------------------------------------------------


async def do_cascade_on_approval(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """The 7-effect Stage-2 approval cascade — the single BOM-cost write-path.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    params:
        ``{
            "namespace_id":       str | UUID,        # required
            "approval_id":        str,                # required — idempotency key
            "quote_id":           str,                 # required — Sales QUOTE identifier
                                                        # (looks up sales_signed_baselines,
                                                        # and is the actual_cost aggregation
                                                        # root via a literal BOM_LINE:{QUOTE}:
                                                        # prefix match — see
                                                        # _read_actual_cost_total)
            "project_id":         str,                 # optional — echoed into effects 3-7
            "invoice_id":         str,                  # optional — echoed into effect 1
                                                          # and effect 4 (scorecard)
            "supplier_id":        str,                  # optional — echoed into effects
                                                          # 4 (scorecard) and 5 (kickback)
            "invoice_amount":     int|float|Decimal|None,  # optional — echoed into
                                                          # effects 5 (kickback) and 7
                                                          # (cashflow); coerced like a line's
                                                          # actual_cost (finite, never bool)
            "invoice_postings":   list[dict] | None,   # optional — effect 1's GL postings
            "lines": [                                  # optional, default [] — one entry
                {                                        # per matched BOM line
                    "bom_line_label": str,              # required per line
                    "actual_cost":    int|float|Decimal, # required per line — negative is
                                                          # legal (a credit note)
                    "postings":       list[dict] | None,# optional per-line GL postings
                },
                ...
            ],
        }``

    Returns
    -------
    dict
        ``{
            "ok": True,
            "approval_id": str,
            "bom_lines_written":  list[str],  # labels a NEW (line, approval) row was
                                               # inserted for this call
            "bom_lines_replayed": list[str],  # labels skipped as exact-key no-ops
            "effects": list[dict],            # the 7 normalised/hashed events
            "signed_margin_pct": Decimal | None,  # read-only, unchanged by this call
        }``

    Raises
    ------
    ValueError
        Missing/malformed required params, an unresolvable ``bom_line_label``,
        or an idempotency-key reuse with a different ``actual_cost``.
    UnbalancedPostingsError
        Any effect's postings do not sum to zero within ``epsilon`` — the
        whole transaction is rolled back (contract 3), including
        ``actual_cost`` writes already issued earlier in this call.
    """
    ns_uuid = _as_ns_uuid(params.get("namespace_id"), "namespace_id")

    approval_id = str(params.get("approval_id") or "").strip()
    if not approval_id:
        raise ValueError("do_cascade_on_approval: 'approval_id' is required (idempotency key)")

    quote_id = str(params.get("quote_id") or "").strip()
    if not quote_id:
        raise ValueError("do_cascade_on_approval: 'quote_id' is required")

    project_id = params.get("project_id")
    invoice_id = params.get("invoice_id")
    supplier_id = params.get("supplier_id")
    invoice_amount = _as_optional_amount(params.get("invoice_amount"), "invoice_amount")
    lines = _as_lines(params.get("lines"))

    effects: list[dict[str, Any]] = []
    bom_lines_written: list[str] = []
    bom_lines_replayed: list[str] = []
    signed_margin_pct: Decimal | None = None

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # --- Effect 1: economy.invoice.approved (whole-call GL postings) ---
        # Validated FIRST, before any DB write — an unbalanced invoice-level
        # posting raises here and nothing below ever runs.
        effects.append(
            do_emit_financial_event(
                _BALANCE_EPSILON_DEFAULT,
                {
                    "type": _EFFECT_INVOICE_APPROVED,
                    "approval_id": approval_id,
                    "invoice_id": invoice_id,
                    "quote_id": quote_id,
                    "postings": params.get("invoice_postings"),
                },
            )
        )

        # --- Effect 2: economy.bom_line.cost_updated (the single write-path) ---
        for line in lines:
            label = line["bom_line_label"]
            actual_cost: Decimal = line["actual_cost"]

            await _assert_bom_line_exists(conn, ns_uuid, label)

            # Validated before the write for THIS line — an unbalanced
            # per-line posting raises here and, because everything is inside
            # scoped_pg_session's single open transaction, rolls back every
            # actual_cost write already issued for earlier lines in this
            # same call (contract 3 — no partial write).
            effects.append(
                do_emit_financial_event(
                    _BALANCE_EPSILON_DEFAULT,
                    {
                        "type": _EFFECT_BOM_LINE_COST_UPDATED,
                        "approval_id": approval_id,
                        "bom_line_label": label,
                        "actual_cost": actual_cost,
                        "postings": line["postings"],
                    },
                )
            )

            inserted = await _upsert_actual_cost(conn, ns_uuid, label, actual_cost, approval_id)
            if inserted:
                bom_lines_written.append(label)
                continue

            # ON CONFLICT fired: this exact (line, approval) key already has
            # a row. Read it back and decide REPLAY-no-op vs REFUSE — the
            # constraint already guarantees the stored data is safe either
            # way; this is about giving the caller an honest answer.
            existing = await _read_existing_cost_row(conn, ns_uuid, label, approval_id)
            if existing is None:  # pragma: no cover - unreachable, ON CONFLICT guarantees a row
                raise RuntimeError(
                    f"do_cascade_on_approval: ON CONFLICT fired for {label!r}/{approval_id!r} "
                    f"but no row was found on read-back"
                )
            if existing["actual_cost"] != actual_cost:
                raise ValueError(
                    f"do_cascade_on_approval: approval_id {approval_id!r} was already "
                    f"applied to {label!r} with actual_cost={existing['actual_cost']} — "
                    f"refusing to apply a different actual_cost={actual_cost} under the "
                    f"same idempotency key"
                )
            bom_lines_replayed.append(label)

        # --- Margin-trinity inputs: READ the Sales-frozen dimension plus the
        # newly-summed actual cost. Never write sales_signed_baselines. ---
        baseline = await _read_signed_baseline(conn, ns_uuid, quote_id)
        signed_margin_pct = baseline["signed_margin_pct"] if baseline is not None else None
        signed_total_nok = baseline["signed_total_nok"] if baseline is not None else None
        actual_cost_total = await _read_actual_cost_total(conn, ns_uuid, quote_id)
        margin_snapshot = _compute_margin_snapshot(
            signed_margin_pct, signed_total_nok, actual_cost_total
        )

        def _margin_field(key: str) -> Decimal | None:
            return margin_snapshot[key] if margin_snapshot is not None else None

        # --- Effect 3: economy.project.margin_recalculated ---
        effects.append(
            do_emit_financial_event(
                _BALANCE_EPSILON_DEFAULT,
                {
                    "type": _EFFECT_MARGIN_RECALCULATED,
                    "approval_id": approval_id,
                    "project_id": project_id,
                    "postings": None,
                    "quote_id": quote_id,
                    "actual_cost_total": actual_cost_total,
                    "signed_margin_pct": _margin_field("signed_margin_pct"),
                    "signed_margin_amount": _margin_field("signed_margin_amount"),
                    "actual_margin_pct": _margin_field("actual_margin_pct"),
                    "actual_margin_amount": _margin_field("actual_margin_amount"),
                    "margin_variance_pct": _margin_field("margin_variance_pct"),
                    "margin_variance_amount": _margin_field("margin_variance_amount"),
                    "trigger": _EFFECT_INVOICE_APPROVED,
                },
            )
        )

        # --- Effect 4: economy.project.supplier_scorecard_updated ---
        effects.append(
            do_emit_financial_event(
                _BALANCE_EPSILON_DEFAULT,
                {
                    "type": _EFFECT_SCORECARD_UPDATED,
                    "approval_id": approval_id,
                    "project_id": project_id,
                    "postings": None,
                    "supplier_id": supplier_id,
                    "invoice_id": invoice_id,
                },
            )
        )

        # --- Effect 5: economy.project.kickback_accrued ---
        effects.append(
            do_emit_financial_event(
                _BALANCE_EPSILON_DEFAULT,
                {
                    "type": _EFFECT_KICKBACK_ACCRUED,
                    "approval_id": approval_id,
                    "project_id": project_id,
                    "postings": None,
                    "supplier_id": supplier_id,
                    "invoice_amount": invoice_amount,
                },
            )
        )

        # --- Effect 6: economy.project.delivery_recalculated ---
        effects.append(
            do_emit_financial_event(
                _BALANCE_EPSILON_DEFAULT,
                {
                    "type": _EFFECT_DELIVERY_RECALCULATED,
                    "approval_id": approval_id,
                    "project_id": project_id,
                    "postings": None,
                    "trigger": _EFFECT_INVOICE_APPROVED,
                },
            )
        )

        # --- Effect 7: economy.project.cashflow_reprojected ---
        effects.append(
            do_emit_financial_event(
                _BALANCE_EPSILON_DEFAULT,
                {
                    "type": _EFFECT_CASHFLOW_REPROJECTED,
                    "approval_id": approval_id,
                    "project_id": project_id,
                    "postings": None,
                    "trigger": _EFFECT_INVOICE_APPROVED,
                    "invoice_amount": invoice_amount,
                },
            )
        )

    log.info(
        "do_cascade_on_approval: ns=%s quote=%s approval=%s lines_written=%d "
        "lines_replayed=%d effects=%d",
        ns_uuid,
        quote_id,
        approval_id,
        len(bom_lines_written),
        len(bom_lines_replayed),
        len(effects),
    )

    return {
        "ok": True,
        "approval_id": approval_id,
        "bom_lines_written": bom_lines_written,
        "bom_lines_replayed": bom_lines_replayed,
        "effects": effects,
        "signed_margin_pct": signed_margin_pct,
    }
