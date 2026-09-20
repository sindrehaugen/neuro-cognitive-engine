"""
nce/vertical_modules/sales/quote_clone.py
==========================================
``sales_clone_quote`` (charter §13 Wave B-5).

Composite operation, not an origination path: creates a new draft QUOTE and
carries the source quote's BOM_LINE content across, rather than authoring new
content of its own. Deliberately separate from ``lines.py``/``external_lines.py``
(each of those is one closed origination path with its own single ``FLOW``
constant) because a clone is not itself a fifth origination path -- it moves
already-authored content into a new container.

PROVENANCE DECISION -- read before changing FLOW below
--------------------------------------------------------
Cloned lines are written through ``content:create:external`` (the same
transition ``external_lines.py`` owns), not through whatever flow originally
created the source lines (design/manual/package/external). This was a real
design choice, not a default:

Preserving each source line's own original flow was considered and rejected.
``create_bom_line``'s ``assert_owner`` check validates a (node_type,
transition) pair against the registry, not the actual calling module -- so it
is technically POSSIBLE for this module to pass ``flow="design"`` and have it
succeed if System Design is genuinely registered for that transition. But
``lines.py``'s and ``external_lines.py``'s own docstrings both state the same
invariant for the same reason: ``flow`` must never be a caller-choosable value
because "a caller that can choose the flow can choose the transition" -- and a
clone operation choosing to relabel a line as ``flow="design"`` when the
ACTUAL code writing it right now is Sales' own clone logic is exactly that
false-provenance shape, just one level removed (relabeling on behalf of a
past write, not a present caller argument). ``content:create:external`` is
the honest description of what is happening: this new quote's copy of the
line did not originate from a fresh manual pick (or any other origination
performed while authoring THIS quote) -- its content was imported, from the
source quote, exactly as ``external_lines.py`` already models "content
brought in from elsewhere" for any other external source. No new
``CreateFlow`` value or node-ownership transition was added for this --
reusing the already-registered, already-audited ``external`` path.

Each source line's own ``origin_ref`` (if any) is carried forward unchanged;
nothing here overwrites it with a "cloned from" note, so a line double-cloned
across quotes keeps whatever provenance note its original author entered.
The clone linkage between the two QUOTE rows themselves (not the lines) is
recorded structurally instead: the new quote's ``metadata`` JSONB gains a
``cloned_from`` key, alongside the source's existing metadata rather than
replacing it.

Numbers copied, not recomputed: ``total_ex_vat``/``total_inc_vat`` are copied
verbatim from the source row. The clone's lines are byte-identical to the
source's lines at the moment of cloning, so the source's own totals are
already the correct totals for the copy -- recomputing them here would be a
second implementation of whatever pricing logic produced them the first time,
the exact duplication this codebase's other modules avoid (see
``bom_lines.py``'s "has_status ... never reimplement" precedent).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from nce.bom_lines import list_bom_lines_for_quote
from nce.vertical_modules.sales.external_lines import do_import_quote_lines

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

    from nce.orchestrator import NCEEngine

_MAX_TITLE_LEN = 512


class QuoteNotFoundError(KeyError):
    """Raised when the source QUOTE does not exist in the given namespace."""


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


async def do_clone_quote(
    engine: NCEEngine,
    namespace_id: str | UUID,
    *,
    quote_id: Any,
) -> dict[str, Any]:
    """Clone a QUOTE and its BOM_LINE content into a new draft quote.

    Opens its OWN ``scoped_pg_session`` (like ``do_get_quote_lines``, unlike
    ``do_add_quote_line``) because this is a top-level operation composing
    multiple writes (the new QUOTE row, then N cloned lines) that must commit
    or fail together -- ``scoped_pg_session`` wraps its whole block in one
    transaction, so no explicit BEGIN/COMMIT is needed here. Reads the source
    lines via ``list_bom_lines_for_quote`` directly on the SAME connection
    rather than calling ``do_get_quote_lines`` (which would open a second,
    separate ``scoped_pg_session`` -- a second pooled connection and a second
    transaction, breaking the atomicity this function exists to provide).

    The new quote is always ``status='draft'``, ``version=1`` -- a clone is a
    fresh editable starting point, never a continuation of the source's own
    lifecycle/version history. ``quote_number`` is the source's own number
    with a ``-COPY`` suffix; ``sales_quotes.quote_number`` carries no UNIQUE
    constraint (confirmed against schema.sql), so this needs no collision
    handling.

    Parameters
    ----------
    quote_id:
        The source QUOTE identifier to clone.

    Returns
    -------
    ``{"quote_id", "quote_number", "source_quote_id", "lines_cloned"}`` --
    ``quote_id``/``quote_number`` describe the NEW quote.

    Raises
    ------
    ``QuoteNotFoundError`` if no QUOTE with ``quote_id`` exists in this
    namespace. ``ValueError`` for a missing/malformed ``quote_id``.
    """
    from nce.db_utils import scoped_pg_session

    source_id = _require_text(quote_id, "quote_id")
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        source = await _fetch_quote(conn, ns_uuid, source_id)
        if source is None:
            raise QuoteNotFoundError(f"QUOTE {source_id} not found")

        new_id = uuid4()
        new_quote_number = f"{source['quote_number']}-COPY"
        new_title = f"{source['title']} (Copy)"[:_MAX_TITLE_LEN]

        # asyncpg returns jsonb columns as str, not dict, unless a codec is
        # registered -- same read pattern design_versions.py already uses for
        # its own meta column. dict(source["metadata"]) on the raw string
        # would iterate its characters, not its keys, and fail on the first
        # one -- exactly the bug the mock-backed unit tier could not catch
        # (a mock returns a real dict; only a live Postgres returns str).
        raw_metadata = source["metadata"]
        metadata = (
            raw_metadata if isinstance(raw_metadata, dict) else json.loads(raw_metadata or "{}")
        )
        metadata = dict(metadata)
        metadata["cloned_from"] = source_id

        await conn.execute(
            """
            INSERT INTO sales_quotes
                (id, namespace_id, quote_number, deal_id, customer_id, title,
                 version, status, total_ex_vat, total_inc_vat, currency,
                 valid_until, billing_method, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, 1, 'draft', $7, $8, $9, $10, $11, $12::jsonb)
            """,
            new_id,
            ns_uuid,
            new_quote_number,
            source["deal_id"],
            source["customer_id"],
            new_title,
            source["total_ex_vat"],
            source["total_inc_vat"],
            source["currency"],
            source["valid_until"],
            source["billing_method"],
            json.dumps(metadata),
        )

        source_lines = await list_bom_lines_for_quote(conn, ns_uuid, quote_id=source_id)
        lines_cloned = 0
        if source_lines:
            payload = [
                {
                    "line_ref": ln["line_ref"],
                    "qty": ln["qty"],
                    "unit_price": ln["unit_price"],
                    "line_total": ln["line_total"],
                    "currency": ln["currency"],
                    "origin_ref": ln.get("origin_ref"),
                }
                for ln in source_lines
            ]
            result = await do_import_quote_lines(conn, ns_uuid, quote_id=str(new_id), lines=payload)
            lines_cloned = result["imported"]

    return {
        "quote_id": str(new_id),
        "quote_number": new_quote_number,
        "source_quote_id": source_id,
        "lines_cloned": lines_cloned,
    }


async def _fetch_quote(
    conn: asyncpg.Connection,  # type: ignore[type-arg,name-defined]
    namespace_id: UUID,
    quote_id: str,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT * FROM sales_quotes WHERE namespace_id = $1 AND id = $2",
        namespace_id,
        UUID(quote_id),
    )
    return dict(row) if row else None
