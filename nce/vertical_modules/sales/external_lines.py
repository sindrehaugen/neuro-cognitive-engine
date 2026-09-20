"""
nce/vertical_modules/sales/external_lines.py
==============================================
The EXTERNAL-IMPORT origination path for ``BOM_LINE`` (charter Wave B-5).

``lines.py``'s own module docstring names this explicitly as future work: "the
design flow, the external adapter, package expansion and the dealroom cutover
are separate waves and are NOT written here." This is that adapter --
origination path 2 of 5, batched (a caller imports N lines from an external
source in one call, not one at a time like the manual pick path).

Same trust boundary as ``lines.py``, restated because it is load-bearing here
too: ``flow`` is NOT a parameter and never will be. It is the module constant
``FLOW`` below, because a caller that could choose the flow could choose the
transition -- an externally-imported line claiming to be a manual pick (or
vice versa) is a false-provenance bug, not a feature. The MCP handler and this
module's own function accept no ``flow``/``origin_kind`` argument; either key
appearing in a caller's payload is silently inert, matching ``lines.py``'s own
convention exactly.

This module is a CALLER of ``nce/bom_lines.py``, never an author of it --
``create_bom_line`` already enforces ``content:create:external`` ownership via
``assert_owner``; this module adds no guard of its own.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.bom_lines import CreateFlow, create_bom_line

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

log = logging.getLogger("nce.vertical_modules.sales.external_lines")

# Must match the owner_engine of BOM_LINE's content:create:external row in
# nce/config_data/node-ownership.json verbatim -- see lines.py's identical
# comment for why a mismatch here is a false-provenance bug, not just an error.
WRITER_ENGINE: str = "sales"

# The one flow this module originates. See the module docstring: this is
# deliberately not reachable from a caller argument.
FLOW: CreateFlow = "external"

TRANSITION: str = f"content:create:{FLOW}"

_MAX_REF_LEN = 128
_MAX_LINES_PER_CALL = 500


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    text = value.strip()
    if len(text) > _MAX_REF_LEN:
        raise ValueError(f"{field} exceeds {_MAX_REF_LEN} characters")
    return text


def _require_decimal(value: Any, field: str) -> Decimal:
    """Coerce a JSON scalar to ``Decimal`` without going through ``float``.

    Identical contract to ``lines.py``'s helper of the same name (money and
    quantity are ``NUMERIC`` in Postgres; a decimal string on the wire must
    survive exactly) -- duplicated rather than imported because each
    origination-path module is self-contained by this codebase's own
    convention (``to_quote.py``, the other real caller, does the same).
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


async def do_import_quote_lines(
    conn: asyncpg.Connection,  # type: ignore[type-arg,name-defined]
    namespace_id: str | UUID,
    *,
    quote_id: Any,
    lines: Any,
) -> dict[str, Any]:
    """Import a batch of externally-sourced lines onto a quote.

    Writes each line through ``content:create:external`` as
    ``writer_engine="sales"`` -- the only transition this module is
    registered to own. Runs inside the CALLER's transaction
    (``scoped_pg_session``); issues no BEGIN/COMMIT, matching
    ``do_add_quote_line``'s contract exactly so a caller may batch this with
    other writes.

    Idempotent per line by ``bom_line_label(quote_id, line_ref)`` (inherited
    from ``create_bom_line``'s own natural key): re-importing the same
    ``line_ref`` returns the existing row rather than creating a second one.

    All-or-nothing: the FIRST invalid line raises immediately (before any
    write for that line), and the caller's own transaction determines whether
    already-written earlier lines in the same batch are kept or rolled back --
    this function makes no rollback decision of its own, matching every other
    caller of ``create_bom_line`` in this codebase.

    Parameters
    ----------
    quote_id:
        The Sales QUOTE identifier every line in this call is added to.
    lines:
        A list of dicts, each shaped like ``do_add_quote_line``'s arguments:
        ``line_ref`` (required), ``qty`` (required), ``unit_price``
        (required), ``line_total`` (optional, defaults to ``qty * unit_price``),
        ``currency`` (optional, defaults to NOK), ``origin_ref`` (optional).

    Returns
    -------
    ``{"quote_id": str, "imported": int, "lines": [row, ...]}`` -- ``lines``
    in the same order as the input, each exactly as
    ``nce.bom_lines.create_bom_line`` returns it.
    """
    quote = _require_text(quote_id, "quote_id")

    if not isinstance(lines, list) or not lines:
        raise ValueError("lines is required and must be a non-empty list")
    if len(lines) > _MAX_LINES_PER_CALL:
        raise ValueError(f"lines exceeds the {_MAX_LINES_PER_CALL}-line limit per call")

    written: list[dict[str, Any]] = []
    for idx, entry in enumerate(lines):
        if not isinstance(entry, dict):
            raise ValueError(f"lines[{idx}] must be an object")

        ref = _require_text(entry.get("line_ref"), f"lines[{idx}].line_ref")
        quantity = _require_decimal(entry.get("qty"), f"lines[{idx}].qty")
        price = _require_decimal(entry.get("unit_price"), f"lines[{idx}].unit_price")
        raw_total = entry.get("line_total")
        total = (
            quantity * price
            if raw_total is None
            else _require_decimal(raw_total, f"lines[{idx}].line_total")
        )

        raw_currency = entry.get("currency")
        code = (
            "NOK"
            if raw_currency is None
            else _require_text(raw_currency, f"lines[{idx}].currency").upper()
        )
        if len(code) != 3:
            raise ValueError(f"lines[{idx}].currency must be a 3-letter ISO-4217 code")

        raw_origin_ref = entry.get("origin_ref")
        ref_note = (
            None
            if raw_origin_ref is None
            else _require_text(raw_origin_ref, f"lines[{idx}].origin_ref")
        )

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
        written.append(row)

    log.info(
        "sales external import wrote %d BOM_LINE row(s) for quote %s (flow=%s, writer=%s)",
        len(written),
        quote,
        FLOW,
        WRITER_ENGINE,
    )

    return {"quote_id": quote, "imported": len(written), "lines": written}
