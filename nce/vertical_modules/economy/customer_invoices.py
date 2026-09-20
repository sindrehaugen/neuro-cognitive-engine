"""
nce/vertical_modules/economy/customer_invoices.py
====================================================
B-13 customer-invoice proposal generation (Lane G, dispatched by ML-orch --
owner engine: economy). Follows directly on B-12 (billing_runs.py):
migration 104 (economy_customer_invoices, CUSTOMER_INVOICE) turns one
BILLING_CANDIDATE into one invoice, priced and ready for a human to review.

Scope: the 'proposal' transition only
---------------------------------------
Charter F13 (host_parity.md) names a four-state lifecycle: proposal ->
approved -> exported -> paid. This wave ships only the first transition
(``do_propose_customer_invoice``) -- the migration's CHECK constraint
already declares all four states so a later wave adding approve/export/paid
needs no further schema change, but this module writes no code for them.
Deliberate, not an oversight, for the same reason B-12 stopped at
"generate" and left billing-run approval/export unbuilt: each remaining
transition is its own real decision (who approves; what "export" produces
-- a PEPPOL/EHF send via peppol.py's do_generate_ehf, an accounting-API
call, or a manual file; what "paid" means -- bank reconciliation or a
manual mark) that deserves its own wave rather than being guessed at here.

Why proposals never assign invoice_number or kid
---------------------------------------------------
The charter's own boundary is explicit: "accounting system stays the legal
system of record." A Norwegian invoice number must be part of an unbroken
legal sequence; minting one at proposal time -- before a human has even
reviewed the amount -- would let NCE invent numbers a real accounting
system never issued. So ``invoice_number`` and ``kid`` are nullable and
left NULL by this module; a future export-transition wave assigns them
(``kid`` via the already-existing ``peppol.py::do_generate_kid``, which is
a pure MOD10 checksum function with no dependency on this module) once
there is a real number to attach one to.

Why subtotal_amount is read from the candidate unchanged (ex-VAT)
---------------------------------------------------------------------
``agreements/price_rules.py``'s ``rates_per_month`` figures carry no VAT of
any kind (checked: no vat/mva reference anywhere in price-rules.json or
price_rules.py) -- the same convention ``sales_quotes.total_ex_vat``
already encodes elsewhere in this schema. ``vat_rate_pct`` defaults to
25.00, reusing the estate's one existing VAT source of truth
(``finago-account-mapping.json``'s ``mva_codes["3"]``, "Utgaende MVA, hoy
sats" -- outbound revenue-side VAT) rather than inventing a new constant.

Why a defaulted VAT rate is never silent (``vat_rate_assumed``)
---------------------------------------------------------------------
25% is right most of the time, but not provably always: checked directly
(not assumed safe) -- ``sales_customers`` has no country, export, or
VAT-exemption field of any kind (``billing_address`` is free-form JSONB
with no guaranteed schema), so a customer legitimately due 0% (an export
sale) or a different rate cannot currently be distinguished from a
standard-rate one anywhere in this data chain. A confidently-wrong VAT
line on a money document is the same failure class
``room_category_pricing.py``'s refuse-and-name design exists to avoid for
room categories -- so rather than silently default, this module sets
``vat_rate_assumed=True`` whenever the caller did not explicitly supply
``vat_rate_pct`` (``False`` when they did -- an explicit override means a
human or a future caller already determined the correct rate). "A human
reviews the proposal" is only a real safeguard if the human can tell which
number was assumed; this column is that visibility.

Why UNIQUE(namespace_id, billing_candidate_id) exists at the DB level
---------------------------------------------------------------------
``@governed``'s idempotency key only catches an exact-same-call replay
(same key). A second, differently-keyed proposal call against the same
candidate is a distinct business error this module should refuse, not
silently double-invoice -- the UNIQUE constraint (migration 104) is the
defense-in-depth backstop, the same shape ``economy_postings``' sum=0
trigger backstops ``do_emit_financial_event``'s own Python-level guard.
"""

from __future__ import annotations

import datetime
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

from nce.autonomy.governor import governed
from nce.entity_resolution.ownership import assert_owner

log = logging.getLogger("nce.vertical_modules.economy.customer_invoices")

_NODE_TYPE_CUSTOMER_INVOICE = "CUSTOMER_INVOICE"
_NODE_TYPE_BILLING_CANDIDATE = "BILLING_CANDIDATE"
_OWNER_ENGINE = "economy"
_PRED_HAS_INVOICE = "has_invoice"
# finago-account-mapping.json's mva_codes["3"] ("Utgaende MVA, hoy sats") --
# the estate's one existing VAT source of truth, not a new invention.
_DEFAULT_VAT_RATE_PCT = Decimal("25.00")
_DEFAULT_DUE_DATE_DAYS = 14
_SCALE_2DP = Decimal("0.01")


def _quantise_money(val: Decimal) -> Decimal:
    return val.quantize(_SCALE_2DP, rounding=ROUND_HALF_UP)


class BillingCandidateNotFoundError(ValueError, KeyError):
    """Raised when the referenced billing candidate does not exist in this namespace."""


class CustomerInvoiceAlreadyExistsError(ValueError):
    """Raised when the referenced billing candidate already has an invoice.

    Distinct from @governed's own idempotency-key replay dedup: this fires
    for a second, differently-keyed proposal attempt against the same
    candidate, caught via the UNIQUE(namespace_id, billing_candidate_id)
    constraint (migration 104) rather than assumed away.
    """

    def __init__(self, billing_candidate_id: Any) -> None:
        self.billing_candidate_id = billing_candidate_id
        super().__init__(f"BILLING_CANDIDATE {billing_candidate_id} already has a customer invoice")


def _parse_uuid(val: Any, field_name: str) -> UUID:
    if not val:
        raise ValueError(f"{field_name} is required")
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} UUID: {val!r}") from exc


async def _upsert_customer_invoice_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
    candidate_label: str,
) -> None:
    await assert_owner(conn, namespace_id, _NODE_TYPE_CUSTOMER_INVOICE, _OWNER_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3::uuid, 'agent')
        ON CONFLICT (label, namespace_id) DO NOTHING
        """,
        label,
        _NODE_TYPE_CUSTOMER_INVOICE,
        namespace_id,
    )
    await conn.execute(
        """
        INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id, change_origin)
        VALUES ($1, $2, $3, 1.0, $4::uuid, 'agent')
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
        """,
        candidate_label,
        _PRED_HAS_INVOICE,
        label,
        namespace_id,
    )


@governed(action_type="propose_customer_invoice")
async def do_propose_customer_invoice(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    engine: Any = None,
    billing_candidate_id: Any,
    vat_rate_pct: Any = None,
    due_date_days: int = _DEFAULT_DUE_DATE_DAYS,
) -> dict[str, Any]:
    """Governed, confirm-first creation of one 'proposal' customer invoice (B-13).

    One BILLING_CANDIDATE (B-12) maps to exactly one CUSTOMER_INVOICE --
    unlike ``do_generate_billing_run``, there is no batch/refuse-at-run-level
    shape here because there is no analogous cross-candidate failure mode:
    the candidate's pricing is already locked in, so this is a single-entity
    action, modelled on ``field_tech/time_entry.py::do_approve_time_entry``
    rather than the billing-run batch shape.

    Parameters
    ----------
    conn:
        asyncpg connection inside an active transaction (``scoped_pg_session``),
        opened by the caller -- ``@governed`` never opens one itself.
    namespace_id:
        Tenant UUID.
    idempotency_key:
        Stable hash of the call inputs (caller's responsibility to derive).
    confirm:
        ``False`` (default) -- ``@governed`` returns ``pending_approval`` and
        this body never runs. ``True`` -- executes once.
    billing_candidate_id:
        The BILLING_CANDIDATE (B-12) to invoice. Must exist in this
        namespace and must not already have an invoice (UNIQUE constraint,
        migration 104 -- raises ``CustomerInvoiceAlreadyExistsError``).
    vat_rate_pct:
        Override for the default 25% outbound VAT rate. ``None`` (default)
        uses 25% and sets ``vat_rate_assumed=True`` on the row -- module
        docstring explains why this is surfaced rather than silent. Passing
        an explicit value means the caller determined the correct rate;
        that row gets ``vat_rate_assumed=False``.
    due_date_days:
        Days from today's issue_date to due_date. Defaults to 14.

    Returns
    -------
    dict with ``status`` ('proposal'), ``customer_invoice_id``,
    ``customer_invoice_label``, ``billing_candidate_id``, ``customer_id``,
    ``issue_date``, ``due_date``, ``currency``, ``subtotal_amount``,
    ``vat_rate_pct``, ``vat_rate_assumed``, ``vat_amount``, ``total_amount``.

    Raises
    ------
    BillingCandidateNotFoundError
        No such candidate in this namespace.
    CustomerInvoiceAlreadyExistsError
        The candidate already has an invoice.
    """
    ns_uuid = _parse_uuid(namespace_id, "namespace_id")
    candidate_uuid = _parse_uuid(billing_candidate_id, "billing_candidate_id")

    candidate = await conn.fetchrow(
        """
        SELECT id, node_label, customer_id, currency, total_amount
        FROM   economy_billing_candidates
        WHERE  id = $1 AND namespace_id = $2::uuid
        """,
        candidate_uuid,
        ns_uuid,
    )
    if candidate is None:
        raise BillingCandidateNotFoundError(
            f"BILLING_CANDIDATE {billing_candidate_id} not found in namespace {namespace_id}"
        )

    rate_assumed = vat_rate_pct is None
    rate = Decimal(str(vat_rate_pct)) if vat_rate_pct is not None else _DEFAULT_VAT_RATE_PCT
    subtotal = Decimal(str(candidate["total_amount"]))
    vat_amount = _quantise_money(subtotal * rate / Decimal("100"))
    total_amount = subtotal + vat_amount

    issue_date = datetime.date.today()
    due_date = issue_date + datetime.timedelta(days=due_date_days)

    invoice_id = uuid4()
    invoice_label = f"{_NODE_TYPE_CUSTOMER_INVOICE}:{invoice_id}"

    await _upsert_customer_invoice_node(conn, ns_uuid, invoice_label, candidate["node_label"])

    try:
        await conn.execute(
            """
            INSERT INTO economy_customer_invoices
                (id, namespace_id, node_label, billing_candidate_id, customer_id,
                 issue_date, due_date, currency, subtotal_amount, vat_rate_pct,
                 vat_rate_assumed, vat_amount, total_amount, status)
            VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'proposal')
            """,
            invoice_id,
            ns_uuid,
            invoice_label,
            candidate_uuid,
            candidate["customer_id"],
            issue_date,
            due_date,
            candidate["currency"],
            subtotal,
            rate,
            rate_assumed,
            vat_amount,
            total_amount,
        )
    except asyncpg.UniqueViolationError as exc:
        raise CustomerInvoiceAlreadyExistsError(billing_candidate_id) from exc

    log.info(
        "propose_customer_invoice: invoice=%s ns=%s candidate=%s total=%s %s",
        invoice_label,
        ns_uuid,
        candidate_uuid,
        total_amount,
        candidate["currency"],
    )

    return {
        "status": "proposal",
        "customer_invoice_id": str(invoice_id),
        "customer_invoice_label": invoice_label,
        "billing_candidate_id": str(candidate_uuid),
        "customer_id": str(candidate["customer_id"]) if candidate["customer_id"] else None,
        "issue_date": str(issue_date),
        "due_date": str(due_date),
        "currency": candidate["currency"],
        "subtotal_amount": float(subtotal),
        "vat_rate_pct": float(rate),
        "vat_rate_assumed": rate_assumed,
        "vat_amount": float(vat_amount),
        "total_amount": float(total_amount),
    }
