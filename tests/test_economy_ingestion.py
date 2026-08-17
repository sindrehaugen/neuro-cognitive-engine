"""
tests/test_economy_ingestion.py
================================
Tests for ``nce.vertical_modules.economy.ingestion``.

Pure-logic tests (no DB) verify the money-parsing helpers, the EHF header stub, and --
most importantly -- that :func:`_requires_review` structurally enforces Section 9.3's
money/legal-field hard rule: an OCR-derived money figure is NEVER auto-eligible, regardless
of confidence.

Integration tests (``@pytest.mark.integration``, require a live Postgres) verify:
- Invoices (EHF and OCR) ingest to a ``memories`` row with embedding + ``content_fts``.
- OCR-derived money figures are ALWAYS review-gated -- proven with a mocked OCR seam that
  returns a clean, warning-free ("high confidence") read, so a regression that made the
  gate depend on OCR confidence/warnings would fail this test.
- The incremental watermark (derived, no table -- see ``ingestion.py`` module docstring)
  advances as invoices are ingested.
- RLS: a namespace cannot see another namespace's ingested invoice.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.vertical_modules.economy.ingestion import (
    _WATERMARK_OVERLAP_SECONDS,
    ET,
    _extract_ocr_money_figure,
    _parse_ehf_document,
    _parse_money_string,
    _requires_review,
    do_get_invoice_watermark,
    do_ingest_invoice,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ns_a(make_namespace) -> uuid.UUID:
    """Fresh namespace for the primary test tenant."""
    return await make_namespace()


@pytest_asyncio.fixture
async def ns_b(make_namespace) -> uuid.UUID:
    """Second namespace -- used to verify RLS isolation."""
    return await make_namespace()


# ---------------------------------------------------------------------------
# EHF/UBL fixtures
# ---------------------------------------------------------------------------

_EHF_FULL = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>INV-2026-001</cbc:ID>
  <cbc:IssueDate>2026-08-01</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>NOK</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyLegalEntity>
        <cbc:CompanyID>987654321</cbc:CompanyID>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal>
    <cbc:PayableAmount>12345.67</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""

_EHF_MISSING_TOTAL = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>INV-2026-002</cbc:ID>
  <cbc:IssueDate>2026-08-02</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>NOK</cbc:DocumentCurrencyCode>
</Invoice>
"""

_EHF_MALFORMED = b"<Invoice><cbc:ID>oops, not closed properly"

# Round-3 Defect 1 fixtures: defusedxml's OWN forbidden-construct exceptions
# (`EntitiesForbidden` / `DTDForbidden` / `ExternalReferenceForbidden` -- MRO runs through
# `DefusedXmlException` -> `ValueError`) are a DIFFERENT hierarchy from `ET.ParseError` (MRO
# runs through `SyntaxError`). Before the fix, `_parse_ehf_document` caught only
# `ET.ParseError`, so either payload below raised uncaught out of `_parse_ehf_document` --
# and out of `do_ingest_invoice` entirely, proven by calling it directly with `pg_pool=None`
# (the crash precedes any DB I/O).
_EHF_BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">&lol2;</Invoice>
"""

_EHF_EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE Invoice [
 <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">&xxe;</Invoice>
"""

# Fix 4 fixture: ID + Currency + PayableAmount all present, but NO <cbc:IssueDate> element
# at all. Per the module docstring, `cbc:IssueDate` is one of the four required header
# fields -- this must NOT parse_complete=True (see TestParseEhfDocument below).
_EHF_MISSING_ISSUE_DATE = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>INV-2026-003</cbc:ID>
  <cbc:DocumentCurrencyCode>NOK</cbc:DocumentCurrencyCode>
  <cac:LegalMonetaryTotal>
    <cbc:PayableAmount>50000.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""


# ---------------------------------------------------------------------------
# Pure-logic tests: _parse_money_string
# ---------------------------------------------------------------------------


class TestParseMoneyString:
    def test_norwegian_comma_decimal_with_thousands_separator(self) -> None:
        assert _parse_money_string("12 345,67") == Decimal("12345.67")

    def test_norwegian_comma_decimal_with_dot_thousands_separator(self) -> None:
        assert _parse_money_string("1.234,56") == Decimal("1234.56")

    def test_plain_dot_decimal(self) -> None:
        assert _parse_money_string("1234.56") == Decimal("1234.56")

    def test_non_numeric_returns_none(self) -> None:
        assert _parse_money_string("total due") is None

    def test_wrong_decimal_length_returns_none(self) -> None:
        assert _parse_money_string("123,4") is None

    def test_nan_string_rejected(self) -> None:
        """`Decimal("nan")` does not raise -- it produces a NaN Decimal. The trailing
        `is_finite()` check must catch it (a truthiness check alone would not, since
        `float('nan')` is truthy in Python)."""
        assert _parse_money_string("nan") is None

    def test_infinity_string_rejected(self) -> None:
        assert _parse_money_string("Infinity") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_money_string("   ") is None

    # -- Fix 1: sign is no longer dropped on negative / credit-note amounts -------------

    def test_negative_comma_decimal_with_thousands_separator(self) -> None:
        """Direct regression for the comma-branch bug: `"-12345".isdigit()` was `False`,
        so a negative Norwegian-format string passed straight to this function used to
        return `None` instead of a negative `Decimal` -- a credit note silently vanished
        rather than merely losing its sign."""
        assert _parse_money_string("-12345,67") == Decimal("-12345.67")

    def test_negative_dot_thousands_separator(self) -> None:
        assert _parse_money_string("-1.234,56") == Decimal("-1234.56")

    def test_negative_plain_dot_decimal(self) -> None:
        assert _parse_money_string("-1234.56") == Decimal("-1234.56")

    def test_unicode_minus_sign_honoured(self) -> None:
        assert _parse_money_string("−12345,67") == Decimal("-12345.67")

    # -- Fix 3: NBSP/narrow-NBSP thousands separators -----------------------------------

    def test_nbsp_thousands_separator(self) -> None:
        """Norwegian locales and many PDF extractors emit NBSP (`\\u00a0`), not ASCII
        space, as the thousands separator. `integer_part.replace(" ", "")` alone does not
        strip it."""
        assert _parse_money_string("12 345,67") == Decimal("12345.67")

    def test_narrow_nbsp_thousands_separator(self) -> None:
        assert _parse_money_string("12 345,67") == Decimal("12345.67")


# ---------------------------------------------------------------------------
# Pure-logic tests: _extract_ocr_money_figure
# ---------------------------------------------------------------------------


class TestExtractOcrMoneyFigure:
    def test_finds_last_amount_in_text(self) -> None:
        text = "Line 1: 100,00\nLine 2: 250,50\nTotal a betale: 12 345,67"
        assert _extract_ocr_money_figure(text) == Decimal("12345.67")

    def test_no_money_token_returns_none(self) -> None:
        assert _extract_ocr_money_figure("no amounts here at all") is None

    def test_empty_text_returns_none(self) -> None:
        assert _extract_ocr_money_figure("") is None

    # -- Fix 1: sign survives tokenization + parsing end to end -------------------------

    def test_negative_amount_keeps_its_sign(self) -> None:
        """Before the fix, `_MONEY_TOKEN_RE` had no minus in its character class, so the
        sign was stripped during tokenization itself -- BEFORE `_parse_money_string` ever
        ran. A credit note was recorded as a positive charge."""
        assert _extract_ocr_money_figure("-12 345,67") == Decimal("-12345.67")

    def test_negative_amount_at_end_of_text(self) -> None:
        text = "Kreditnota\nLine 1: 500,00\nTotal a betale: -9 999,00"
        assert _extract_ocr_money_figure(text) == Decimal("-9999.00")

    # -- Fix 2: ambiguous thousands-separator token refuses to guess ---------------------

    def test_ambiguous_dot_token_returns_none_not_truncated(self) -> None:
        """`\\d+\\.\\d{2}` used to match greedily without a boundary anchor, so
        "Total: 1.234" silently matched "1.23" and dropped the trailing "4" -- wrong under
        either reading of "1.234" (1234 or 1.234). The fixed regex must not match this
        token at all, so the figure falls through to `None` per the module's own
        "return nothing rather than guess" contract."""
        assert _extract_ocr_money_figure("Total: 1.234") is None

    def test_unambiguous_plain_dot_decimal_still_matches(self) -> None:
        """The boundary anchor must not break the legitimate, unambiguous case."""
        assert _extract_ocr_money_figure("Total: 1234.56") == Decimal("1234.56")

    # -- Fix 3: NBSP thousands separator in real OCR-shaped text -------------------------

    def test_nbsp_separator_extracts_full_amount(self) -> None:
        """Before the fix this under-extracted by ~35x: `[ .]` matched ASCII space only,
        so the regex found just the trailing "345,67" fragment after the NBSP."""
        text = "Total a betale: 12 345,67"
        assert _extract_ocr_money_figure(text) == Decimal("12345.67")

    # -- Round-3 Defect 2: round-2 anchored only the dot alternative; the comma alternative --
    # -- (the primary Norwegian format) had NO boundary anchors at all, and the optional -----
    # -- sign class matched a bare hyphen that was not a sign. Every case below is pinned by --
    # -- exact value; each goes red if `(?<![\d-])` / the comma-side `(?!\d)` is reverted. ----

    def test_multi_digit_comma_run_without_separator_returns_none(self) -> None:
        """Before the fix: `"Total 1234,567"` matched the truncated inner fragment
        "234,56", dropping the leading "1" AND the trailing "7". `.567` is 3 digits
        either way it's read -- not a valid 2-decimal amount -- so the whole token must
        fail to match, not silently truncate."""
        assert _extract_ocr_money_figure("Total 1234,567") is None

    def test_long_digit_run_without_thousands_separators_now_extracts_full_value(
        self,
    ) -> None:
        """Round-3 fix (this test's old name/body): `"123456,78"` matched the truncated
        inner fragment "456,78" -- fixed by requiring the integer part to be grouped
        thousands, so the ambiguous bare run fell through to `None`.

        Round-4 fix (this test, corrected): requiring grouping *unconditionally* was
        itself a defect, not a feature -- it also rejected a perfectly ordinary ungrouped
        amount, and OCR frequently drops thin/NBSP separators, so a real grouped amount
        like "12 498,75" can arrive as "12498,75". A bare digit run before the comma is
        not inherently ambiguous the way a bare-then-truncated-fragment match was: unlike
        round-2's bug, the WHOLE run is now consumed (`\\d+` as a fallback alternative
        alongside the grouped-thousands alternative), never a truncated inner fragment.
        So this must now extract the full value, not `None`."""
        assert _extract_ocr_money_figure("123456,78") == Decimal("123456.78")

    def test_reference_number_hyphen_not_read_as_minus_sign_comma(self) -> None:
        """Before the fix: `"Ref 1-234,56"` matched with a spurious leading minus,
        returning `Decimal("-234.56")` -- the hyphen in a reference number like "1-234"
        is not a sign. `(?<![\\d-])`, checked BEFORE the optional sign, blocks both the
        sign-consuming match (hyphen preceded by digit "1") and the sign-less match at
        the same position (digit preceded by the hyphen itself)."""
        assert _extract_ocr_money_figure("Ref 1-234,56") is None

    def test_reference_number_hyphen_not_read_as_minus_sign_dot(self) -> None:
        """Before the fix: `"5-1234.56"` matched `Decimal("-1234.56")` -- the old
        `(?<!\\d)`, placed AFTER the optional sign, only ever inspected the character
        right before the digit run (the sign itself when present, never a digit), so it
        never blocked this. `(?<![\\d-])`, checked BEFORE the sign, blocks it: the
        character immediately before the sign position ("5") is a digit."""
        assert _extract_ocr_money_figure("5-1234.56") is None

    def test_realistic_invoice_kid_footer_returns_true_total_not_reference_fragment(
        self,
    ) -> None:
        """The single most damaging round-2 case: a genuine 12 498,75 NOK total followed
        by an ordinary Norwegian KID-reference footer used to return `Decimal("296.00")`
        -- a truncated fragment of the reference number, not any real money figure at
        all. The fixed regex must return the true total, or `None` -- never `296.00`."""
        text = "Faktura totalt: 12 498,75 NOK\nKID-referanse: 4471-2296,00"
        result = _extract_ocr_money_figure(text)
        assert result != Decimal("296.00")
        assert result == Decimal("12498.75") or result is None

    # -- Round-4 fix: bare digit run before the comma (no thousands separators at all) -------
    # -- now extracts, instead of matching nothing. Round 3 required the integer part to be --
    # -- grouped thousands; that made "1234,56" -- the single most common way a Norwegian ------
    # -- amount is written -- match nothing at all. It failed *safe* (OCR is unconditionally --
    # -- review-gated), but it made the OCR path nearly useless: no figure ever reached the ----
    # -- reviewer. The fix adds a `\d+` fallback alongside the grouped-thousands alternative. --

    def test_bare_digit_run_before_comma_now_extracts(self) -> None:
        """`"1234,56"` has no thousands separator at all -- round 3's grouped-thousands-only
        integer part matched nothing here. Must now extract the full value."""
        assert _extract_ocr_money_figure("1234,56") == Decimal("1234.56")

    def test_long_bare_digit_run_before_comma_now_extracts(self) -> None:
        """`"123456,78"` -- a 6-digit ungrouped run -- is exactly what a grouped amount
        becomes once OCR drops the thin/NBSP separator (e.g. "12 498,75" -> "123456,78"
        is the shape, not the same value, but the same class of degraded input). Must now
        extract the full value, not `None`."""
        assert _extract_ocr_money_figure("123456,78") == Decimal("123456.78")

    def test_bare_digit_run_before_comma_with_surrounding_text_now_extracts(self) -> None:
        assert _extract_ocr_money_figure("Total 1234,56") == Decimal("1234.56")

    # -- Legitimate-value regression set: the new anchors must not reject real amounts ------

    def test_dot_thousands_separator_still_extracts_standalone(self) -> None:
        assert _extract_ocr_money_figure("1.234,56") == Decimal("1234.56")

    def test_space_thousands_separator_still_extracts_standalone(self) -> None:
        assert _extract_ocr_money_figure("1 234,56") == Decimal("1234.56")

    def test_nbsp_thousands_separator_still_extracts_standalone(self) -> None:
        assert _extract_ocr_money_figure("12 345,67") == Decimal("12345.67")

    def test_plain_dot_decimal_still_extracts_standalone(self) -> None:
        assert _extract_ocr_money_figure("1234.56") == Decimal("1234.56")

    def test_negative_space_separator_still_extracts_standalone(self) -> None:
        assert _extract_ocr_money_figure("-12 345,67") == Decimal("-12345.67")

    def test_amount_at_string_start_still_matches(self) -> None:
        assert _extract_ocr_money_figure("1.234,56 er totalen") == Decimal("1234.56")

    def test_amount_at_string_end_still_matches(self) -> None:
        assert _extract_ocr_money_figure("Totalen er 1234.56") == Decimal("1234.56")


# ---------------------------------------------------------------------------
# Pure-logic tests: _parse_ehf_document (the ~95% stub)
# ---------------------------------------------------------------------------


class TestParseEhfDocument:
    def test_full_header_parses_all_fields(self) -> None:
        result = _parse_ehf_document(_EHF_FULL)
        assert result["invoice_number"] == "INV-2026-001"
        assert result["currency"] == "NOK"
        assert result["supplier_orgnr"] == "987654321"
        assert result["issue_date"] == "2026-08-01"
        assert result["money_amount"] == Decimal("12345.67")
        assert result["parse_complete"] is True

    def test_missing_total_marks_incomplete(self) -> None:
        result = _parse_ehf_document(_EHF_MISSING_TOTAL)
        assert result["invoice_number"] == "INV-2026-002"
        assert result["money_amount"] is None
        assert result["parse_complete"] is False

    def test_malformed_xml_falls_back_without_raising(self) -> None:
        result = _parse_ehf_document(_EHF_MALFORMED)
        assert result["parse_complete"] is False
        assert result["money_amount"] is None
        # Raw text is still captured for embedding/FTS even when XML parsing fails.
        assert "oops" in result["text"]
        assert any("ehf_parse_failed" in w for w in result["warnings"])

    def test_missing_issue_date_marks_incomplete_and_gates_review(self) -> None:
        """Fix 4: the module docstring names `cbc:IssueDate` as one of four required
        header fields, but the old `parse_complete` check tested only `invoice_number`,
        `currency`, and `money_amount is not None` -- `issue_date` was excluded. A UBL
        invoice with ID + Currency + a 50,000 NOK PayableAmount and NO `<cbc:IssueDate>`
        element at all used to get `parse_complete=True` -> `requires_review=False`,
        letting a 50,000 NOK figure pass ungated. This pins the fixed, fail-toward-review
        behaviour end to end (`_parse_ehf_document` -> `_requires_review`)."""
        result = _parse_ehf_document(_EHF_MISSING_ISSUE_DATE)
        assert result["invoice_number"] == "INV-2026-003"
        assert result["currency"] == "NOK"
        assert result["money_amount"] == Decimal("50000.00")
        assert result["issue_date"] is None
        assert result["parse_complete"] is False, (
            "a UBL document missing cbc:IssueDate must NOT be treated as a complete parse"
        )
        assert _requires_review("ehf", result["parse_complete"]) is True, (
            "a 50,000 NOK figure with no IssueDate must be gated for human review"
        )

    # -- Round-3 Defect 1: defusedxml's forbidden-construct exceptions are a DIFFERENT ------
    # -- MRO from ET.ParseError (ValueError vs SyntaxError) and must degrade identically, ----
    # -- not raise. Goes red if `except (ET.ParseError, DefusedXmlException)` is narrowed ----
    # -- back to `except ET.ParseError` alone. -----------------------------------------------

    def test_billion_laughs_entity_payload_degrades_without_raising(self) -> None:
        """Before the fix: `defusedxml.common.EntitiesForbidden` is NOT an instance of
        `ET.ParseError`, so the old `except ET.ParseError` handler did not catch it -- this
        payload raised straight out of `_parse_ehf_document` (and, verified separately, out
        of `do_ingest_invoice` called directly with `pg_pool=None`, proving the crash
        precedes any DB I/O). The module's own documented contract (module docstring) is
        that malformed/hostile XML degrades to `parse_complete=False` rather than raising."""
        result = _parse_ehf_document(_EHF_BILLION_LAUGHS)
        assert result["parse_complete"] is False
        assert result["money_amount"] is None
        assert any("ehf_parse_failed" in w for w in result["warnings"])
        assert _requires_review("ehf", result["parse_complete"]) is True

    def test_external_entity_payload_degrades_without_raising(self) -> None:
        """Same defect, XXE shape (`SYSTEM "file:///etc/passwd"`) instead of billion-laughs
        -- defusedxml raises `EntitiesForbidden` for this shape too (the entity declaration
        itself is forbidden before external resolution is ever attempted). Must degrade the
        same way, not raise."""
        result = _parse_ehf_document(_EHF_EXTERNAL_ENTITY)
        assert result["parse_complete"] is False
        assert result["money_amount"] is None
        assert any("ehf_parse_failed" in w for w in result["warnings"])
        assert _requires_review("ehf", result["parse_complete"]) is True


# ---------------------------------------------------------------------------
# Pure-logic test: Fix 5 -- defusedxml, not stdlib xml.etree, parses invoice bytes
# ---------------------------------------------------------------------------


class TestUsesDefusedXml:
    def test_ehf_parser_imports_defusedxml_elementtree(self) -> None:
        """`ingestion.py` must parse attacker-supplied invoice bytes via
        `defusedxml.ElementTree` (already a direct dependency, matching the precedent in
        `nce/extractors/adobe_ext.py` / `diagrams.py` / `office_word.py`), not stdlib
        `xml.etree.ElementTree` -- defence-in-depth against XXE/billion-laughs, even
        though neither is currently exploitable on this stack's expat version."""
        assert ET.__name__.startswith("defusedxml"), (
            f"expected the defusedxml.ElementTree module, got {ET.__name__!r}"
        )


# ---------------------------------------------------------------------------
# Pure-logic tests: _requires_review -- the Section 9.3 gate, proven structurally
# ---------------------------------------------------------------------------


class TestRequiresReview:
    """The money/legal-field hard rule (Section 9.3): an OCR-derived money figure is
    NEVER auto-eligible, regardless of confidence. These tests prove the gate cannot be
    bypassed -- not merely that it happens to return True for one sample input."""

    def test_ocr_pdf_always_requires_review_regardless_of_parse_state(self) -> None:
        assert _requires_review("ocr_pdf", parse_complete=True) is True
        assert _requires_review("ocr_pdf", parse_complete=False) is True

    def test_ocr_image_always_requires_review_regardless_of_parse_state(self) -> None:
        assert _requires_review("ocr_image", parse_complete=True) is True
        assert _requires_review("ocr_image", parse_complete=False) is True

    def test_ehf_full_parse_does_not_require_review(self) -> None:
        assert _requires_review("ehf", parse_complete=True) is False

    def test_ehf_partial_parse_requires_review(self) -> None:
        assert _requires_review("ehf", parse_complete=False) is True

    def test_no_confidence_parameter_exists_on_the_gate(self) -> None:
        """Structural proof: the function that decides review-eligibility for OCR money
        has no confidence/threshold parameter at all, so no caller can pass one in."""
        params = set(inspect.signature(_requires_review).parameters)
        assert params == {"document_format", "parse_complete"}


# ---------------------------------------------------------------------------
# Integration tests: do_ingest_invoice -- EHF path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoIngestInvoiceEhf:
    async def test_full_parse_writes_memories_row_not_review_required(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        invoice_id = f"inv-{uuid.uuid4().hex[:8]}"

        result = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id=invoice_id,
            document_bytes=_EHF_FULL,
            document_format="ehf",
        )

        assert "memory_id" in result, f"Expected memory_id in result, got: {result}"
        assert result["requires_review"] is False
        assert result["money_amount"] == "12345.67"
        memory_id = result["memory_id"]

        row = await pg_admin_conn.fetchrow(
            "SELECT id, namespace_id, embedding, content_fts, payload_ref, agent_id, metadata "
            "FROM memories WHERE id = $1::uuid",
            memory_id,
        )
        assert row is not None, "memories row not found"
        assert str(row["namespace_id"]) == str(ns_a)
        assert row["embedding"] is not None
        assert row["content_fts"] is not None
        assert len(row["payload_ref"]) == 24
        assert row["agent_id"] == "economy-invoice-ingest"

        meta = json.loads(row["metadata"])
        assert meta["requires_review"] is False
        assert meta["money_amount"] == "12345.67"
        assert meta["invoice_number"] == "INV-2026-001"
        assert meta["economy_ingest_marker"] == "invoice"

    async def test_partial_parse_requires_review(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        invoice_id = f"inv-{uuid.uuid4().hex[:8]}"

        result = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id=invoice_id,
            document_bytes=_EHF_MISSING_TOTAL,
            document_format="ehf",
        )

        assert result["requires_review"] is True
        assert result["money_amount"] is None

        row = await pg_admin_conn.fetchrow(
            "SELECT metadata FROM memories WHERE id = $1::uuid", result["memory_id"]
        )
        meta = json.loads(row["metadata"])
        assert meta["requires_review"] is True

    async def test_malformed_xml_still_ingests_and_requires_review(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        invoice_id = f"inv-{uuid.uuid4().hex[:8]}"

        result = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id=invoice_id,
            document_bytes=_EHF_MALFORMED,
            document_format="ehf",
        )

        assert "memory_id" in result
        assert result["requires_review"] is True
        assert result["money_amount"] is None


# ---------------------------------------------------------------------------
# Integration tests: do_ingest_invoice -- OCR path (the mandatory gate)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoIngestInvoiceOcr:
    """The wave's binding requirement: an OCR'd money figure is ALWAYS review-gated,
    never auto-posted -- regardless of confidence. The mocked seam below simulates a
    CLEAN, warning-free ("high confidence") OCR read; a regression that made the gate
    depend on OCR warnings/confidence would incorrectly report `requires_review=False`
    here and this test would fail."""

    async def test_ocr_pdf_money_always_requires_review_even_with_clean_read(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        invoice_id = f"inv-{uuid.uuid4().hex[:8]}"
        clean_text = "Supplier AS\nLine 1: 500,00\nTotal a betale: 9 999,00"

        async def _clean_ocr(_document_bytes: bytes, _document_format: str):
            return clean_text, []  # no warnings -- simulates a "high confidence" read

        with patch("nce.vertical_modules.economy.ingestion._run_ocr", side_effect=_clean_ocr):
            result = await do_ingest_invoice(
                pg_pool,
                ns_a,
                invoice_id=invoice_id,
                document_bytes=b"%PDF-1.4 fake pdf bytes",
                document_format="ocr_pdf",
            )

        assert result["requires_review"] is True, (
            "OCR money must ALWAYS be review-gated, regardless of confidence (Section 9.3)"
        )
        assert result["money_amount"] == "9999.00"

        row = await pg_admin_conn.fetchrow(
            "SELECT metadata FROM memories WHERE id = $1::uuid", result["memory_id"]
        )
        meta = json.loads(row["metadata"])
        assert meta["requires_review"] is True, (
            "the persisted memories row must also carry requires_review=true"
        )

    async def test_ocr_image_money_always_requires_review_even_with_clean_read(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        invoice_id = f"inv-{uuid.uuid4().hex[:8]}"
        clean_text = "Total: 1234.56"

        async def _clean_ocr(_document_bytes: bytes, _document_format: str):
            return clean_text, []

        with patch("nce.vertical_modules.economy.ingestion._run_ocr", side_effect=_clean_ocr):
            result = await do_ingest_invoice(
                pg_pool,
                ns_a,
                invoice_id=invoice_id,
                document_bytes=b"\x89PNG fake image bytes",
                document_format="ocr_image",
            )

        assert result["requires_review"] is True

    async def test_empty_ocr_text_returns_skipped(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        async def _empty_ocr(_document_bytes: bytes, _document_format: str):
            return "", ["ocr_low_confidence: discarded (<30%)"]

        with patch("nce.vertical_modules.economy.ingestion._run_ocr", side_effect=_empty_ocr):
            result = await do_ingest_invoice(
                pg_pool,
                ns_a,
                invoice_id="inv-empty",
                document_bytes=b"blank",
                document_format="ocr_pdf",
            )

        assert result.get("skipped") == "empty document text"


# ---------------------------------------------------------------------------
# Integration tests: common behaviour (format validation, degraded embedding, RLS)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDoIngestInvoiceCommon:
    async def test_invalid_document_format_raises(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        with pytest.raises(ValueError, match="document_format"):
            await do_ingest_invoice(
                pg_pool,
                ns_a,
                invoice_id="inv-bad-format",
                document_bytes=b"whatever",
                document_format="carrier_pigeon",
            )

    async def test_degraded_embedding_still_writes_row(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        stub_vector = [0.0] * 768

        async def _stubbed_embed_batch(texts: list[str]) -> list[list[float]]:
            return [list(stub_vector) for _ in texts]

        with (
            patch("nce.embeddings.embed_batch", side_effect=_stubbed_embed_batch),
            patch("nce.embeddings.degraded_embedding_flag") as mock_flag,
        ):
            mock_flag.get.return_value = True

            result = await do_ingest_invoice(
                pg_pool,
                ns_a,
                invoice_id=f"inv-{uuid.uuid4().hex[:8]}",
                document_bytes=_EHF_FULL,
                document_format="ehf",
            )

        assert result["degraded"] is True
        row = await pg_admin_conn.fetchrow(
            "SELECT embedding, metadata FROM memories WHERE id = $1::uuid", result["memory_id"]
        )
        assert row["embedding"] is not None
        meta = json.loads(row["metadata"])
        assert meta.get("degraded_embedding") is True

        ledger_row = await pg_admin_conn.fetchrow(
            "SELECT tlx_scores FROM v3_cognitive_ledger WHERE memory_id = $1::uuid",
            result["memory_id"],
        )
        assert ledger_row is not None
        tlx = json.loads(ledger_row["tlx_scores"])
        assert tlx.get("degraded_embedding") is True

    async def test_rls_namespace_isolation(
        self,
        pg_pool: asyncpg.Pool,
        pg_app_conn: asyncpg.Connection,
        ns_a: uuid.UUID,
        ns_b: uuid.UUID,
    ) -> None:
        result = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id=f"inv-{uuid.uuid4().hex[:8]}",
            document_bytes=_EHF_FULL,
            document_format="ehf",
        )
        memory_id = result["memory_id"]

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            row = await pg_app_conn.fetchrow(
                "SELECT id FROM memories WHERE id = $1::uuid", memory_id
            )
        assert row is None, "RLS isolation violated: namespace B can see namespace A's invoice"


# ---------------------------------------------------------------------------
# Integration tests: incremental watermark
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestWatermark:
    async def test_watermark_none_before_any_ingest(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        watermark = await do_get_invoice_watermark(pg_pool, ns_a)
        assert watermark is None

    async def test_watermark_advances_after_each_ingest(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """Fix 6: the returned watermark is `MAX(created_at) - _WATERMARK_OVERLAP_SECONDS`,
        not the raw per-row `created_at` returned by `do_ingest_invoice` -- so it is
        pinned as `first["watermark"] - overlap`, not exact equality (see
        `test_watermark_equals_max_created_at_minus_overlap` for the direct proof)."""
        before = await do_get_invoice_watermark(pg_pool, ns_a)
        assert before is None

        first = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id="inv-watermark-1",
            document_bytes=_EHF_FULL,
            document_format="ehf",
        )
        after_first = await do_get_invoice_watermark(pg_pool, ns_a)
        assert after_first is not None
        expected_first = datetime.fromisoformat(first["watermark"]) - timedelta(
            seconds=_WATERMARK_OVERLAP_SECONDS
        )
        assert datetime.fromisoformat(after_first) == expected_first

        second = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id="inv-watermark-2",
            document_bytes=_EHF_MISSING_TOTAL,
            document_format="ehf",
        )
        after_second = await do_get_invoice_watermark(pg_pool, ns_a)
        assert after_second is not None
        assert after_second >= after_first
        expected_second = datetime.fromisoformat(second["watermark"]) - timedelta(
            seconds=_WATERMARK_OVERLAP_SECONDS
        )
        assert datetime.fromisoformat(after_second) == expected_second

    async def test_watermark_equals_max_created_at_minus_overlap(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        """Fix 6, pinned directly against the raw DB row: Postgres `now()` (the
        `created_at` default) is transaction-start time, not commit time, so a raw
        `MAX(created_at)` watermark can permanently skip a row from a slower-committing
        concurrent transaction. `do_get_invoice_watermark` must return
        `MAX(created_at) - _WATERMARK_OVERLAP_SECONDS`, giving at-least-once semantics.
        This test would go red if the overlap subtraction were reverted (it would then
        assert equal to a value `_WATERMARK_OVERLAP_SECONDS` seconds in the future of the
        real watermark)."""
        result = await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id=f"inv-{uuid.uuid4().hex[:8]}",
            document_bytes=_EHF_FULL,
            document_format="ehf",
        )
        raw_created_at = await pg_admin_conn.fetchval(
            "SELECT created_at FROM memories WHERE id = $1::uuid", result["memory_id"]
        )
        assert raw_created_at is not None

        watermark = await do_get_invoice_watermark(pg_pool, ns_a)
        assert watermark is not None
        watermark_dt = datetime.fromisoformat(watermark)
        expected = raw_created_at.astimezone(timezone.utc) - timedelta(
            seconds=_WATERMARK_OVERLAP_SECONDS
        )
        assert watermark_dt.astimezone(timezone.utc) == expected

    async def test_watermark_ignores_marker_match_from_different_agent(
        self, pg_pool: asyncpg.Pool, pg_admin_conn: asyncpg.Connection, ns_a: uuid.UUID
    ) -> None:
        """Fix 7: defence-in-depth -- the watermark predicate must also filter on
        `agent_id`, not just the `metadata` marker. A future copy-paste of this ingest
        pattern under a different `agent_id` that writes the same
        `economy_ingest_marker: "invoice"` key must not be able to pull the watermark
        forward (or, here, into the future) and corrupt it."""
        await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id=f"inv-{uuid.uuid4().hex[:8]}",
            document_bytes=_EHF_FULL,
            document_format="ehf",
        )

        # Simulate a different agent writing a row with the same marker, timestamped
        # far in the future -- if the watermark query only filtered on the marker, this
        # row would pull the watermark forward into the future.
        await pg_admin_conn.execute(
            """
            INSERT INTO memories (
                namespace_id, agent_id, payload_ref, metadata, created_at
            ) VALUES (
                $1::uuid, $2, $3, $4::jsonb, now() + interval '1 day'
            )
            """,
            str(ns_a),
            "some-other-agent-copy-pasted-this-pattern",
            uuid.uuid4().hex[:24],
            json.dumps({"economy_ingest_marker": "invoice"}),
        )

        watermark = await do_get_invoice_watermark(pg_pool, ns_a)
        assert watermark is not None
        watermark_dt = datetime.fromisoformat(watermark)
        assert watermark_dt < datetime.now(timezone.utc), (
            "a row written under a different agent_id must not be able to pull this "
            "module's watermark into the future"
        )

    async def test_watermark_is_namespace_scoped(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, ns_b: uuid.UUID
    ) -> None:
        await do_ingest_invoice(
            pg_pool,
            ns_a,
            invoice_id="inv-ns-a",
            document_bytes=_EHF_FULL,
            document_format="ehf",
        )

        watermark_b = await do_get_invoice_watermark(pg_pool, ns_b)
        assert watermark_b is None, "namespace B must not see namespace A's watermark"
