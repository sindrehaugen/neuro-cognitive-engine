"""
nce/vertical_modules/sales/lines.py
=====================================
The MANUAL-PICK origination path for ``BOM_LINE`` (Module 5, Wave 15 --
Batch 132d).

A human picks an article and it becomes exactly one ``BOM_LINE`` row. This is
**origination path 1 of 5**; the design flow, the external adapter, package
expansion and the dealroom cutover are separate waves and are NOT written
here. Provenance is per LINE, not per quote -- a mixed-origin quote is normal,
so nothing in this module may assume it authored every line of a quote.

Why this module exists at all
-------------------------------
``nce/bom_lines.py`` (Batch 132a) is the guarded STORE. Until this wave nothing
in the repo called ``create_bom_line``: downstream integration tests seeded
``BOM_LINE`` rows by raw SQL, which bypasses ``assert_owner`` entirely and so
proved the trigger logic while never proving that a real guarded write
survives. This module is the first real caller.

Trust boundary -- READ BEFORE ADDING A PARAMETER
--------------------------------------------------
``origin_kind`` is set by THIS module's own flow-to-origin mapping and never
from a caller-supplied argument. There is deliberately no ``origin_kind``
parameter and no ``**kwargs`` on ``do_add_quote_line``: a caller that passes
one gets a ``TypeError``, not a forged provenance. The MCP handler likewise
never reads such an argument -- an ``origin_kind`` key in the tool arguments is
inert. ``flow`` is not a parameter either; it is the module constant ``FLOW``,
because a caller that can choose the flow can choose the transition, and a
manually entered line could then claim to be design-generated.

This module is a CALLER of ``nce/bom_lines.py``, never an author of it.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.bom_lines import CreateFlow, create_bom_line, list_bom_lines_for_quote
from nce.db_utils import scoped_pg_session

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

log = logging.getLogger("nce.vertical_modules.sales.lines")

# The engine identity this module writes as. Must match the ``owner_engine``
# of the BOM_LINE rows in ``nce/config_data/node-ownership.json`` verbatim --
# any other value is denied by ``assert_owner``, and a value that were somehow
# accepted would record a false provenance.
WRITER_ENGINE: str = "sales"

# The one flow this module originates. ``bom_lines.CreateFlow`` is a closed
# literal, so this is the whole of the mapping: FLOW is the transition suffix
# AND the stored ``origin_kind``, and neither is reachable from a caller.
FLOW: CreateFlow = "manual"

# Recorded for the tests and for readers; ``create_bom_line`` composes the same
# string itself from ``FLOW``. Kept in sync by
# ``tests/test_sales_lines_manual.py``, which asserts both against
# node-ownership.json rather than against each other.
TRANSITION: str = f"content:create:{FLOW}"

_MAX_REF_LEN = 128


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    text = value.strip()
    if len(text) > _MAX_REF_LEN:
        raise ValueError(f"{field} exceeds {_MAX_REF_LEN} characters")
    return text


def _require_decimal(value: Any, field: str) -> Decimal:
    """Coerce a JSON scalar to ``Decimal`` without going through ``float``.

    Money and quantity are ``NUMERIC`` in Postgres; a decimal string on the
    wire must survive exactly, so ``str`` is accepted and preferred. ``bool``
    is rejected explicitly -- it is an ``int`` subclass and would otherwise
    silently become 0 or 1.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is required and must be a number")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int | float | str):
        try:
            candidate = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field} is not a valid number: {value!r}") from exc
    else:
        raise ValueError(f"{field} is required and must be a number")
    if not candidate.is_finite():
        raise ValueError(f"{field} must be finite")
    if candidate < 0:
        raise ValueError(f"{field} must not be negative")
    return candidate


async def do_add_quote_line(
    conn: asyncpg.Connection,  # type: ignore[type-arg,name-defined]
    namespace_id: str | UUID,
    *,
    quote_id: Any,
    line_ref: Any,
    qty: Any,
    unit_price: Any,
    line_total: Any = None,
    currency: Any = None,
    origin_ref: Any = None,
) -> dict[str, Any]:
    """Add ONE manually picked line to a quote.

    Writes through ``content:create:manual`` as ``writer_engine="sales"`` --
    the only transition this module is registered to own. Any other transition
    is denied by ``assert_owner`` inside ``nce.bom_lines.create_bom_line``;
    this function adds no guard of its own and must not, because a second
    implementation of the ownership question is how the rule drifts.

    Runs inside the CALLER's transaction (``scoped_pg_session``); issues no
    BEGIN/COMMIT.

    Idempotent by ``bom_line_label(quote_id, line_ref)``: replaying the same
    pick returns the existing row rather than creating a second one. That
    property is inherited from the store's natural key, not re-implemented as
    a check-then-write that would race.

    ``line_total`` defaults to ``qty * unit_price``. A caller MAY pass one
    (an agreed line discount is not derivable), but it is content, never
    provenance -- unlike ``origin_kind``, which has no parameter at all.
    """
    quote = _require_text(quote_id, "quote_id")
    ref = _require_text(line_ref, "line_ref")
    quantity = _require_decimal(qty, "qty")
    price = _require_decimal(unit_price, "unit_price")
    total = quantity * price if line_total is None else _require_decimal(line_total, "line_total")

    code = "NOK" if currency is None else _require_text(currency, "currency").upper()
    if len(code) != 3:
        raise ValueError("currency must be a 3-letter ISO-4217 code")

    ref_note = None if origin_ref is None else _require_text(origin_ref, "origin_ref")

    row = await create_bom_line(
        conn,
        namespace_id,
        flow=FLOW,
        writer_engine=WRITER_ENGINE,
        quote_id=quote,
        line_ref=ref,
        qty=quantity,
        unit_price=price,
        line_total=total,
        currency=code,
        origin_ref=ref_note,
    )
    log.info(
        "sales manual pick wrote BOM_LINE %s (flow=%s, writer=%s)",
        row.get("bom_line_label"),
        FLOW,
        WRITER_ENGINE,
    )
    return row


async def do_get_quote_lines(
    engine: Any,
    namespace_id: str | UUID,
    quote_id: Any,
) -> list[dict[str, Any]]:
    """Read every ``BOM_LINE`` on one quote. THE READ SEAM (M5.W16, Batch 132f).

    This is the cross-engine read that System Design's
    ``system_design.from_quote._read_quote_lines`` resolves to. It writes
    nothing, takes no ``writer_engine`` from anybody, and adds no guard of its
    own: ``nce.bom_lines.list_bom_lines_for_quote`` is already explicitly
    ``namespace_id``-scoped in SQL and never relies on RLS alone, so this
    function calls it rather than issuing a query of its own.

    Opens its OWN ``scoped_pg_session`` (unlike ``do_add_quote_line``, which
    joins the caller's transaction) because the seam is called from
    ``from_quote.py`` before any transaction is open, deliberately.

    WHAT THIS RETURNS, AND WHAT IT DOES NOT -- defect D37
    -------------------------------------------------------
    Exactly the columns ``bom_line_content`` holds (migration 058):
    ``bom_line_label, quote_id, line_ref, qty, unit_price, line_total,
    currency, origin_kind, origin_ref, writer_engine, status,
    status_changed_at, frozen_at``.

    ``from_quote``'s seam docstring documents six fields; FOUR of them
    (``fl_path``, ``manufacturer``, ``mfr_part_no``, ``confidence``) do not
    exist in the store anywhere -- there is no SKU, product, manufacturer or
    FL reference on a ``BOM_LINE`` today. That is ledger defect **D37**, filed
    separately and NOT fixed here. This function therefore returns what the
    store holds and lets ``do_design_from_quote``'s existing ``.get()``
    defaults supply ``"UNKNOWN"``/``[]``/``1.0``. Inventing any of them here
    would be indistinguishable downstream from an authored value.
    """
    if isinstance(namespace_id, UUID):
        ns_uuid = namespace_id
    else:
        if not isinstance(namespace_id, str) or not namespace_id.strip():
            raise ValueError("namespace_id is required")
        ns_uuid = UUID(namespace_id.strip())

    quote = _require_text(quote_id, "quote_id")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        rows = await list_bom_lines_for_quote(conn, ns_uuid, quote_id=quote)

    log.info("sales read %d BOM_LINE row(s) for quote %s", len(rows), quote)
    return rows
