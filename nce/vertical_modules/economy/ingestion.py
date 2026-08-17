"""
nce/vertical_modules/economy/ingestion.py
==========================================
Semantic Track: invoice ingest (EHF parse OR OCR) -> NCE memory + cognitive-recall ledger.

``do_ingest_invoice`` is the single public entry-point. Mirrors
``nce/vertical_modules/product/ingestion.py::do_ingest_spec`` (same shape, same repo
conventions): it embeds the ingested document text, writes one ``memories`` row
(embedding + ``content_fts``) and one ``v3_cognitive_ledger`` row, namespace-scoped via
``scoped_pg_session``. Per ``docs/vertical_engines/08-economy-engine.md`` build phase B3
("invoice ingest (EHF parser OR Claude Vision OCR) -> memories; incremental watermark") and
``docs/vertical_engines/00-ENGINES-ROADMAP.md`` Section 9.3.

Money/legal-field hard rule (Section 9.3) -- the governing design point
------------------------------------------------------------------------
Section 9.3 ("Money/legal-field hard rule") states that an AI-extracted field driving
money **never auto-promotes to authoritative on self-reported confidence, no matter how
high; a human signs off before it posts** -- and names Economy invoices as one of the
three escalating OCR surfaces. This module enforces that structurally, not by
instruction:

* Every OCR-sourced money figure (``document_format in {"ocr_pdf", "ocr_image"}``) is
  **always** ``requires_review=True``. :func:`_requires_review` takes **no confidence
  argument at all** for that branch -- there is no parameter, config key, or threshold
  anywhere in this module that can flip it to ``False``. That is the structural proof: the
  code path to auto-eligibility simply does not exist.
* An EHF figure is gated **unless** the (stub -- see below) parser found every required
  header field. EHF extraction is deterministic XML field-reading, not an AI guess, so a
  *fully* parsed EHF total is not subject to the "AI-extracted... on self-reported
  confidence" rule the same way OCR is; a *partially* parsed one is exactly as much of a
  guess as a scanned figure, so it is gated identically to OCR.
* :func:`_requires_review` is the single place this decision is made; both the returned
  dict and the persisted ``memories.metadata`` read the *same* value it returns (see the
  module-level note on the "normalise once" rule below) -- there is no second, disagreeing
  computation anywhere in this file.

EHF parser -- explicitly a ~95% stub (flagged, not built out)
-----------------------------------------------------------------
Per ``08-economy-engine.md``'s hardening notes, the EHF/PEPPOL parser is a stub pending the
PEPPOL-provider decision (Tickstar/Pagero); EHF B2B becomes mandatory Jan 2027 but the
provider is not yet selected. :func:`_parse_ehf_document` reads exactly four UBL header
fields (``cbc:ID``, ``cbc:IssueDate``, ``cbc:DocumentCurrencyCode``,
``cac:LegalMonetaryTotal/cbc:PayableAmount``) plus a best-effort supplier org number. It
does **not** parse invoice lines, VAT/tax-subtotal breakdown, credit notes/corrections,
multiple UBL versions or namespace variants, embedded attachments, or validate against the
PEPPOL BIS Billing 3.0 schema -- all of that remains future work. Parses via
``defusedxml.ElementTree`` (already a direct dependency, ``requirements.txt``; same
precedent as ``nce/extractors/adobe_ext.py``, ``diagrams.py``, ``office_word.py``) rather
than stdlib ``xml.etree.ElementTree`` -- defence-in-depth against XXE/billion-laughs on
attacker-supplied invoice bytes; no new dependency is added.

OCR -- reuses the existing extractor, no new dependency, no external Vision API
------------------------------------------------------------------------------
The wave's "Claude Vision OCR" wording is satisfied by the existing tesseract-backed
extractor (``nce/extractors/ocr.py``: ``ocr_pdf_to_sections`` / ``ocr_image_bytes``) via
the injectable seam :func:`_run_ocr`, module-level so ``unittest.mock.patch`` replaces it
cleanly in tests (the established pattern -- see
``nce/vertical_modules/system_design/from_quote.py::_read_quote_lines``). No new OCR
dependency is added and no external Claude Vision HTTP API is called here.

Incremental watermark -- derived, not stored (zero-DDL)
--------------------------------------------------------
This wave adds no migration and no table. The watermark is **derived** as
``MAX(created_at) - _WATERMARK_OVERLAP_SECONDS`` over the ``memories`` rows this module has
written for a namespace, identified by a stable marker (``_INGEST_MARKER_KEY``/
``_INGEST_MARKER_VALUE``) plus ``agent_id`` in ``metadata`` -- a
``JSONB NOT NULL DEFAULT '{}'`` column that already exists (``nce/schema.sql:95``).
:func:`do_get_invoice_watermark` computes it inside a namespace-scoped session (RLS), so it
is naturally per-tenant with no schema change. The overlap subtraction guards against
Postgres ``now()`` being transaction-start time, not commit time (see
``_WATERMARK_OVERLAP_SECONDS``'s module-level comment); this yields **at-least-once**
delivery -- callers must dedupe by ``id``.

Constraints respected:
  - All writes are inside ``scoped_pg_session`` (RLS enforced); every read/write keeps its
    ``namespace_id`` filter.
  - No UPDATE/DELETE on ``event_log``. ``confidence`` never appears on ``kg_nodes`` (this
    module writes no graph rows at all -- only ``memories`` + ``v3_cognitive_ledger``).
  - No external I/O (embedding call, OCR call) inside the pg transaction (per the
    ``db_utils`` warning) -- both happen before ``scoped_pg_session`` opens.
  - Money is never round-tripped through ``float``: parsed amounts stay ``Decimal`` end to
    end and are serialised with ``str()``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

log = logging.getLogger("nce.vertical_modules.economy.ingestion")

# Agent label written to memories.agent_id for invoice ingest rows.
_AGENT_ID = "economy-invoice-ingest"

# Recognised values for `document_format`. Anything else raises -- silently defaulting to
# one branch would misfile a document as the wrong kind of guess.
_OCR_DOCUMENT_FORMATS = frozenset({"ocr_pdf", "ocr_image"})
_VALID_DOCUMENT_FORMATS = frozenset({"ehf"}) | _OCR_DOCUMENT_FORMATS

# Stable marker written into memories.metadata for every row this module writes, so the
# watermark query can identify "this namespace's invoice-ingest rows" without a table of
# its own (Rule 6: no migrations -- see module docstring).
_INGEST_MARKER_KEY = "economy_ingest_marker"
_INGEST_MARKER_VALUE = "invoice"

# Transaction-start-vs-commit race guard for the watermark query below.
# Postgres `now()` (the `created_at` default, `nce/schema.sql`) is evaluated at
# TRANSACTION START, not at commit. A transaction that opens first but commits second
# therefore lands a row whose `created_at` is EARLIER than a watermark already handed out
# to a consumer polling `WHERE created_at > watermark` -- that consumer would never see the
# row again. Mirrors `nce/vertical_modules/dynamics365/client.py::CURSOR_OVERLAP_SECONDS`
# ("records updated just before the previous tick but indexed slightly late are not
# missed") for the identical hazard. See :func:`do_get_invoice_watermark` for the
# at-least-once semantics this produces.
_WATERMARK_OVERLAP_SECONDS: int = 300  # 5 minutes

# UBL namespaces used by EHF/PEPPOL BIS Billing invoices.
_UBL_NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}

# Heuristic money-token shapes: Norwegian comma-decimal with optional space/dot/NBSP
# thousands separators (e.g. "12 345,67", "1.234,56", "12\xa0345,67" -- NBSP/narrow-NBSP
# are what Norwegian locales and many PDF extractors actually emit, not ASCII space), or a
# plain dot-decimal fallback ("1234.56"). Both alternatives accept an optional leading ASCII
# hyphen or Unicode minus sign (credit notes / corrections print negative amounts) -- the
# sign is captured here so it is not stripped before `_parse_money_string` ever sees it.
#
# BOTH alternatives are boundary-anchored on every side: `(?<![\d-])` before
# the optional sign, `(?!\d)` after the decimal digits -- so an ambiguous token like
# "1.234" (3 digits after the dot -- not a valid 2-decimal amount under either
# thousands-separator or decimal-point reading) does not match at all and falls through to
# `None`, rather than silently truncating to "1.23". Without the trailing
# anchor, a longer digit run like "1234,567" or "123456,78" matches a truncated inner
# fragment ("234,56"/"456,78") instead of failing outright. Without the leading anchor
# covering both a preceding digit AND a preceding literal "-", a reference/date/range
# hyphen right before a digit run (e.g. "1-234,56", "4471-2296,00") gets misread as a
# minus sign, or is not blocked at all -- `(?<!\d)` placed after the sign only
# ever sees the sign character it just consumed, never what came before it.
# `(?<![\d-])`, checked BEFORE the sign, fixes both in one assertion: it rejects
# a preceding digit (mid-run continuation) and a preceding hyphen (not a sign).
#
# Invariant for the next reader (this heuristic has now produced four separate
# defects): this is a "last money-looking token wins" scan over unstructured OCR text,
# not a parser -- used ONLY for `document_format in {ocr_pdf, ocr_image}`, which
# `_requires_review` unconditionally gates to `True` regardless of how clean the match
# looks. When a token is ambiguous, this must resolve to `None` -- never guess by
# truncating or misreading a sign.
#
# The comma-decimal integer part accepts EITHER a grouped-thousands run
# (`\d{1,3}(?:[ .  ]\d{3})+`, i.e. at least one separator+group) OR a bare
# digit run (`\d+`) -- a fourth defect: requiring grouping unconditionally meant a
# perfectly ordinary ungrouped amount like "1234,56" (the most common way a Norwegian
# amount is written, and also what a grouped amount becomes once OCR drops a thin/NBSP
# separator, e.g. "12 498,75" -> "12498,75") matched nothing at all rather than
# something wrong -- fail-safe, but useless: no figure ever reached the reviewer.
# The grouped alternative's quantifier is `+` (not `*`) now that a bare `\d+`
# alternative exists alongside it: with `*` the two alternatives would overlap
# (an ungrouped run also satisfies "zero separator groups"), which is harmless for
# matching here but is the kind of ambiguity that invites subtle backtracking bugs
# later -- keep the two shapes disjoint.
_MONEY_TOKEN_RE = re.compile(
    r"(?<![\d-])[-−]?(?:\d{1,3}(?:[ .  ]\d{3})+|\d+),\d{2}(?!\d)"
    r"|(?<![\d-])[-−]?\d+\.\d{2}(?!\d)"
)


# ---------------------------------------------------------------------------
# Money parsing -- untyped text is hostile; never routes through float()
# ---------------------------------------------------------------------------


def _parse_money_string(raw: str) -> Decimal | None:
    """Parse a UBL/OCR amount TEXT into an exact ``Decimal``, or ``None`` if unusable.

    UBL amounts are already plain decimal text with a ``.`` decimal point (e.g.
    ``"1234.56"``) -- no locale formatting. OCR text is the hostile case: Norwegian
    invoices print ``,`` as the decimal separator and ``.``/space as thousands separators
    (e.g. ``"12 345,67"``). Both shapes are handled; anything else returns ``None`` rather
    than guessing further -- a figure this function cannot read cleanly must not enter the
    ledger at all, gated or not.

    Never goes through ``float()``: a binary float cannot represent money exactly, and
    ``Decimal(a_float)`` would import that same drift. This builds an explicit digit
    string and hands it to ``Decimal(...)`` directly. ``Decimal("nan")``/``Decimal("inf")``
    are guarded by the trailing ``is_finite()`` check -- ``float('nan')`` is truthy in
    Python, so a truthiness check alone would not have caught it.

    A leading ``-`` or Unicode minus (``−``) is honoured (credit notes / corrections
    are negative amounts) -- it is stripped up front and re-applied to the parsed
    ``Decimal`` at the end, so the comma-decimal branch's ``integer_part.isdigit()`` check
    below (which would otherwise reject a negative sign outright) never sees it.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    negative = False
    if cleaned[0] in ("-", "−"):
        negative = True
        cleaned = cleaned[1:]
    if "," in cleaned:
        integer_part, _sep, decimal_part = cleaned.rpartition(",")
        if not (decimal_part.isdigit() and len(decimal_part) == 2):
            return None
        # Strip every thousands-separator shape this module recognises -- ASCII space,
        # dot, NBSP (`` ``) and narrow NBSP (`` ``); the last two are what
        # Norwegian locales and many PDF extractors actually emit, not ASCII space.
        integer_part = (
            integer_part.replace(" ", "").replace(".", "").replace(" ", "").replace(" ", "")
        )
        if not integer_part.isdigit():
            return None
        cleaned = f"{integer_part}.{decimal_part}"
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not value.is_finite():  # NaN, sNaN, +/-Infinity
        return None
    return -value if negative else value


def _extract_ocr_money_figure(text: str) -> Decimal | None:
    """Best-effort guess at a total-amount figure from raw OCR text.

    Deliberately a heuristic, not a parser: OCR text has no structure, so this scans from
    the END of the text for the first money-shaped token that parses cleanly (invoice
    totals conventionally appear near the bottom of the document). It exists only to give
    the mandatory review gate below something to attach to -- a human always confirms it
    before it can post (Section 9.3), regardless of how clean this guess looks.
    """
    matches = _MONEY_TOKEN_RE.findall(text)
    for candidate in reversed(matches):
        parsed = _parse_money_string(candidate)
        if parsed is not None:
            return parsed
    return None


# ---------------------------------------------------------------------------
# EHF (UBL) header parse -- ~95% stub, flagged (see module docstring)
# ---------------------------------------------------------------------------


def _parse_ehf_document(document_bytes: bytes) -> dict[str, Any]:
    """Best-effort EHF (Norwegian UBL/PEPPOL BIS Billing) header parse. A ~95% stub.

    Reads exactly four header fields from a UBL ``Invoice`` root (``cbc:ID``,
    ``cbc:IssueDate``, ``cbc:DocumentCurrencyCode``,
    ``cac:LegalMonetaryTotal/cbc:PayableAmount``) plus a best-effort supplier org number.
    Does not parse invoice lines, VAT breakdown, credit notes, multiple UBL
    versions/namespaces, or attachments, and does not validate against the PEPPOL schema
    (see module docstring). Any of those gaps -- or malformed/non-UBL XML -- degrades this
    function to ``parse_complete=False`` rather than raising; the caller then forces the
    money review gate for the same reason it forces it for OCR: an incompletely-parsed
    total is also a guess.

    Returns
    -------
    dict with ``text`` (best-effort decoded document text, for embedding/FTS),
    ``invoice_number``, ``supplier_orgnr``, ``currency``, ``issue_date`` (``str | None``
    each), ``money_amount`` (``Decimal | None``), ``parse_complete`` (``bool``) and
    ``warnings`` (``list[str]``).
    """
    text = document_bytes.decode("utf-8", errors="replace")
    result: dict[str, Any] = {
        "text": text,
        "invoice_number": None,
        "supplier_orgnr": None,
        "currency": None,
        "issue_date": None,
        "money_amount": None,
        "parse_complete": False,
        "warnings": [],
    }

    try:
        root = ET.fromstring(document_bytes)
    except (ET.ParseError, DefusedXmlException) as exc:
        # `ET.ParseError` covers genuinely malformed/non-UBL XML (its MRO runs through
        # `SyntaxError`). `DefusedXmlException` is the common base defusedxml itself uses
        # for every forbidden-construct it raises instead of parsing on -- `EntitiesForbidden`,
        # `DTDForbidden`, `ExternalReferenceForbidden` (MRO runs through `ValueError`, an
        # entirely different hierarchy from `ParseError`). Both must degrade identically:
        # a hostile or merely legacy-custom-entity invoice must never crash this function,
        # only ever gate its money figure for review (see module docstring).
        result["warnings"].append(f"ehf_parse_failed: {exc}")
        return result

    def _find_text(path: str) -> str | None:
        el = root.find(path, _UBL_NS)
        if el is None or el.text is None:
            return None
        stripped = el.text.strip()
        return stripped or None

    result["invoice_number"] = _find_text("cbc:ID")
    result["issue_date"] = _find_text("cbc:IssueDate")
    result["currency"] = _find_text("cbc:DocumentCurrencyCode")
    result["supplier_orgnr"] = _find_text(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID"
    )

    total_text = _find_text("cac:LegalMonetaryTotal/cbc:PayableAmount")
    if total_text is not None:
        result["money_amount"] = _parse_money_string(total_text)

    # All four required header fields named in the module docstring must be present --
    # `issue_date` included -- or the figure is treated as a partial parse and gated
    # identically to OCR (fail toward review, never weaken the gate to match a narrower
    # check).
    result["parse_complete"] = bool(
        result["invoice_number"]
        and result["issue_date"]
        and result["currency"]
        and result["money_amount"] is not None
    )
    if not result["parse_complete"]:
        result["warnings"].append("ehf_parse_incomplete: one or more header fields missing")
    return result


# ---------------------------------------------------------------------------
# OCR seam -- injectable for tests (mirrors from_quote.py's `_read_quote_lines` pattern)
# ---------------------------------------------------------------------------


async def _run_ocr(document_bytes: bytes, document_format: str) -> tuple[str, list[str]]:
    """Run OCR over an invoice document and return ``(text, warnings)``.

    Delegates to the existing extractor pipeline (``nce.extractors.ocr``) -- this is the
    "Claude Vision OCR" step in wave terms; see the module docstring for why no new OCR
    dependency or external Vision API is introduced. Module-level so tests replace it with
    ``unittest.mock.patch`` and never need a live tesseract/poppler installation.
    """
    from nce.extractors import ocr as _ocr

    if document_format == "ocr_pdf":
        text, _sections, warnings = await _ocr.ocr_pdf_to_sections(document_bytes)
        return text, warnings
    if document_format == "ocr_image":
        return await _ocr.ocr_image_bytes(document_bytes)
    raise ValueError(f"_run_ocr: unsupported document_format {document_format!r}")


# ---------------------------------------------------------------------------
# The review gate -- Section 9.3, structurally enforced
# ---------------------------------------------------------------------------


def _requires_review(document_format: str, parse_complete: bool) -> bool:
    """Whether an ingested money figure must be human-reviewed before it can post.

    **OCR-derived money is ALWAYS ``True``** -- this branch reads only ``document_format``
    and ignores everything else; there is no confidence argument here at all, by design
    (Section 9.3, "no auto-promote, regardless of confidence"). An EHF figure is gated too
    UNLESS the (stub) parser found every required header field -- see the module docstring
    for why a partial EHF parse is treated the same as OCR.
    """
    if document_format in _OCR_DOCUMENT_FORMATS:
        return True
    return not parse_complete


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def do_ingest_invoice(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    invoice_id: str,
    document_bytes: bytes,
    document_format: str,
    source: str = "invoice_ingest",
    trigger: str = "manual",
) -> dict[str, Any]:
    """Ingest an invoice document (EHF parse or OCR) into the cognitive-recall substrate.

    Parameters
    ----------
    pg_pool:
        asyncpg connection pool. RLS context is set inside ``scoped_pg_session``.
    namespace_id:
        Tenant namespace UUID -- all writes are scoped to this namespace.
    invoice_id:
        Caller-supplied identifier for the invoice (stored in ``memories.metadata`` and in
        the ledger metadata).
    document_bytes:
        Raw invoice document bytes -- EHF/UBL XML for ``document_format="ehf"``, or a
        PDF/image blob for ``"ocr_pdf"``/``"ocr_image"``.
    document_format:
        One of ``"ehf"``, ``"ocr_pdf"``, ``"ocr_image"``. Anything else raises
        ``ValueError``.
    source, trigger:
        Provenance recorded in ``v3_cognitive_ledger.tlx_scores`` for audit (mirrors
        ``product/ingestion.py``'s ``do_ingest_spec``).

    Returns
    -------
    dict with ``memory_id``, ``degraded`` (bool), ``invoice_id``, ``document_format``,
    ``requires_review`` (bool -- see :func:`_requires_review`), ``money_amount``
    (``str | None`` -- the parsed amount, serialised, never a ``float``) and
    ``watermark`` (ISO-8601 string -- this row's own ``created_at``).
    """
    if document_format not in _VALID_DOCUMENT_FORMATS:
        raise ValueError(
            f"do_ingest_invoice: document_format must be one of "
            f"{sorted(_VALID_DOCUMENT_FORMATS)}, got {document_format!r}"
        )

    # ------------------------------------------------------------------
    # 1. Extract text + a (possibly absent) money figure. Format-specific, no DB or
    #    embedding I/O yet.
    # ------------------------------------------------------------------
    if document_format == "ehf":
        parsed = _parse_ehf_document(document_bytes)
    else:
        ocr_text, ocr_warnings = await _run_ocr(document_bytes, document_format)
        parsed = {
            "text": ocr_text,
            "invoice_number": None,
            "supplier_orgnr": None,
            "currency": None,
            "issue_date": None,
            "money_amount": _extract_ocr_money_figure(ocr_text),
            "parse_complete": False,
            "warnings": ocr_warnings,
        }

    document_text: str = parsed["text"]
    if not document_text or not document_text.strip():
        log.warning(
            "[ECONOMY-INGEST] skipped empty document text invoice_id=%s format=%s",
            invoice_id,
            document_format,
        )
        return {"skipped": "empty document text"}

    # Computed ONCE, read by both the return dict and the persisted metadata below --
    # never re-derived from a second, potentially-disagreeing source (Batch 116's worst
    # defect was normalising for validation and then reading the raw value at the write).
    requires_review: bool = _requires_review(document_format, parsed["parse_complete"])
    money_amount: Decimal | None = parsed["money_amount"]

    # ------------------------------------------------------------------
    # 2. Embed -- outside the pg transaction (slow I/O must not hold a lock)
    # ------------------------------------------------------------------
    from nce import embeddings as _embeddings

    vectors = await _embeddings.embed_batch([document_text])
    vector: list[float] = vectors[0] if vectors else []
    degraded: bool = _embeddings.degraded_embedding_flag.get()

    # ------------------------------------------------------------------
    # 3. INSERT memories + 4. INSERT v3_cognitive_ledger, in one scoped transaction.
    # ------------------------------------------------------------------
    from nce.db_utils import scoped_pg_session

    memory_id = uuid.uuid4()
    vector_str = f"[{','.join(str(v) for v in vector)}]" if vector else None

    # payload_ref has a CHECK constraint requiring a 24-char hex Mongo ObjectId shape.
    # Invoice ingest has no Mongo document; derive a stable 24-char ref from the memory
    # UUID so the constraint is satisfied and the value is traceable (matches
    # product/ingestion.py::do_ingest_spec).
    payload_ref = memory_id.hex[:24]

    row_metadata: dict[str, Any] = {
        "invoice_id": invoice_id,
        "source": source,
        "trigger": trigger,
        "document_format": document_format,
        "requires_review": requires_review,
        "invoice_number": parsed["invoice_number"],
        "supplier_orgnr": parsed["supplier_orgnr"],
        "currency": parsed["currency"],
        "issue_date": parsed["issue_date"],
        _INGEST_MARKER_KEY: _INGEST_MARKER_VALUE,
    }
    if money_amount is not None:
        # Never float(): str() on a Decimal keeps exact precision end to end.
        row_metadata["money_amount"] = str(money_amount)
    if degraded:
        row_metadata["degraded_embedding"] = True

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        created_at = await conn.fetchval(
            """
            INSERT INTO memories (
                id, namespace_id, agent_id, content_fts,
                payload_ref, memory_type, assertion_type,
                embedding, pii_redacted, metadata
            ) VALUES (
                $1::uuid, $2::uuid, $3, to_tsvector('english', $4),
                $5, $6, $7, $8::vector, $9, $10::jsonb
            )
            RETURNING created_at
            """,
            str(memory_id),
            str(namespace_id),
            _AGENT_ID,
            document_text[:4000],
            payload_ref,
            "episodic",
            "observation",
            vector_str,
            False,
            json.dumps(row_metadata),
        )

        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (
                memory_id, namespace_id, empathic_tensor,
                tlx_scores, vad_scores, model_version
            ) VALUES (
                $1::uuid, $2::uuid, $3::float[], $4::jsonb, $5::jsonb, $6
            )
            """,
            str(memory_id),
            str(namespace_id),
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            json.dumps(
                {
                    "source": source,
                    "trigger": trigger,
                    "invoice_id": invoice_id,
                    "document_format": document_format,
                    "requires_review": requires_review,
                    "degraded_embedding": degraded,
                }
            ),
            json.dumps({}),
            "1.0",
        )

    log.info(
        "[ECONOMY-INGEST] invoice ingested invoice_id=%s memory_id=%s format=%s "
        "requires_review=%s degraded=%s",
        invoice_id,
        memory_id,
        document_format,
        requires_review,
        degraded,
    )

    return {
        "memory_id": str(memory_id),
        "degraded": degraded,
        "invoice_id": invoice_id,
        "document_format": document_format,
        "requires_review": requires_review,
        "money_amount": str(money_amount) if money_amount is not None else None,
        "watermark": created_at.isoformat(),
    }


async def do_get_invoice_watermark(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> str | None:
    """Return the incremental invoice-ingest watermark for a namespace, or ``None``.

    Zero-DDL by design (Rule 6: no migrations) -- there is no watermark table. The value
    is DERIVED as ``MAX(created_at)`` over the ``memories`` rows this module has written
    for the namespace, identified by the stable ``_INGEST_MARKER_KEY``/
    ``_INGEST_MARKER_VALUE`` marker in ``metadata``, AND ``agent_id = _AGENT_ID`` (defence
    in depth -- a future copy-paste of this ingest pattern under a different agent writing
    the same metadata marker must not corrupt this module's watermark). Namespace-scoped
    inside ``scoped_pg_session`` like every other read/write in this module (RLS).

    **At-least-once semantics, by design.** The returned value is
    ``MAX(created_at) - _WATERMARK_OVERLAP_SECONDS``, not the raw max -- see the constant's
    module-level comment for the transaction-start-vs-commit race this guards against. A
    consumer polling with ``WHERE created_at > watermark`` will therefore sometimes see an
    invoice it already processed; callers MUST dedupe by ``id`` (or ``invoice_id`` in
    ``metadata``). For invoices, re-processing on the next poll is safe; silently skipping
    one because it landed just behind an already-issued watermark is not.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        watermark = await conn.fetchval(
            """
            SELECT MAX(created_at) - $4::interval FROM memories
            WHERE namespace_id = $1::uuid AND metadata->>$2 = $3 AND agent_id = $5
            """,
            str(namespace_id),
            _INGEST_MARKER_KEY,
            _INGEST_MARKER_VALUE,
            timedelta(seconds=_WATERMARK_OVERLAP_SECONDS),
            _AGENT_ID,
        )
    return watermark.isoformat() if watermark is not None else None
