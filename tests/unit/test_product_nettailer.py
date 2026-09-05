"""
tests/unit/test_product_nettailer.py
=====================================
Unit tests for the Nettailer source adapter (W1 acceptance gate).

All tests are pure-logic / no-network: HTTP is mocked via the ``_mock_lines``
seam in :func:`stream_nettailer_rows`.

Covers
------
* Alias-map normalisation: a sample CSV row maps to the correct canonical shape.
* Streaming in batches: rows are yielded in configurable batch sizes.
* Idempotency: re-parsing the same bytes yields the same logical rows.
* Duplicate dedup within a single feed run.
* Secret GUID never appears in any log output or return value.
* Cost/margin fields are excluded from the public row.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from nce.config import DeploymentConfigurationError
from nce.vertical_modules.product.sources.nettailer import (
    _INTERNAL_FIELDS,
    FIELD_ALIAS_MAP,
    _csv_headers,
    _dedup_key,
    normalise_row,
    parse_csv_row,
    public_row,
    stream_nettailer_rows,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADER_LINE = (
    "artnr;manufacturer;produktnavn;description;category;ean;lifecycle;"
    "stock;pris;bid_price;supplier_price;currency"
)

_DATA_LINE_1 = (
    '"AMP-1000";"Biamp";"TesiraFORTE AVB VT4";"DSP conferencing unit";'
    '"DSP";"1234567890123";"aktiv";"5";"15000";"12000";"13000";"NOK"'
)

_DATA_LINE_2 = (
    '"QSC-500";"QSC";"Q-SYS Core 110f";"Q-SYS Core processor";'
    '"DSP";"9876543210987";"aktiv";"2";"85000";"75000";"80000";"NOK"'
)

# Duplicate of line 1 — same (manufacturer, mfr_part_no)
_DATA_LINE_1_DUP = (
    '"AMP-1000";"Biamp";"TesiraFORTE AVB VT4 (dup)";"DSP conferencing unit dup";'
    '"DSP";"1234567890123";"aktiv";"10";"14000";"11000";"12000";"NOK"'
)

_SECRET_GUID = "super-secret-guid-abc123"
_SECRET_URL = f"https://netset.example.com/export/{_SECRET_GUID}/products.csv"


def _feed_lines(*extra_data: str) -> list[str]:
    return [_HEADER_LINE, *extra_data]


# ---------------------------------------------------------------------------
# Alias-map normalisation
# ---------------------------------------------------------------------------


def test_field_alias_map_contains_key_columns() -> None:
    """FIELD_ALIAS_MAP must map all critical column aliases."""
    for alias in ("artnr", "manufacturer", "ean", "pris", "bid_price"):
        assert alias in FIELD_ALIAS_MAP, f"alias '{alias}' missing from FIELD_ALIAS_MAP"


def test_parse_csv_row_handles_quoted_semicolons() -> None:
    headers = ["a", "b", "c"]
    row = parse_csv_row(headers, '"hello; world";"foo";"bar"')
    assert row["a"] == "hello; world"
    assert row["b"] == "foo"
    assert row["c"] == "bar"


def test_normalise_row_maps_to_canonical_fields() -> None:
    """A standard Nettailer row must map to the expected canonical field names."""
    # Use the production header parser to exercise strip + lowercase
    headers = _csv_headers(_HEADER_LINE)
    internal = normalise_row(headers, _DATA_LINE_1)

    assert internal["mfr_part_no"] == "AMP-1000"
    assert internal["manufacturer"] == "Biamp"
    assert internal["product_name"] == "TesiraFORTE AVB VT4"
    assert internal["short_description"] == "DSP conferencing unit"
    assert internal["category"] == "DSP"
    assert internal["gtin"] == "1234567890123"
    assert internal["lifecycle_status"] == "aktiv"
    assert internal["stock_qty"] == "5"
    # Internal cost fields must be present in internal dict
    assert internal["unit_cost"] == "15000"
    assert internal["bid_price"] == "12000"
    assert internal["supplier_price"] == "13000"


def test_public_row_strips_cost_and_margin_fields() -> None:
    """Cost/margin fields must never appear in the public canonical row."""
    headers = [h.lower() for h in _HEADER_LINE.split(";")]
    internal = normalise_row(headers, _DATA_LINE_1)
    pub = public_row(internal)

    for field in _INTERNAL_FIELDS:
        assert field not in pub, f"internal field '{field}' leaked into public_row"

    # Canonical identity fields must still be present
    assert "mfr_part_no" in pub
    assert "manufacturer" in pub


def test_dedup_key_is_lowercase_normalised() -> None:
    row: dict[str, Any] = {"manufacturer": "  Biamp  ", "mfr_part_no": " AMP-1000 "}
    key = _dedup_key(row)
    assert key == ("biamp", "amp-1000")


def test_normalise_row_preserves_norwegian_characters() -> None:
    """Norwegian product names with ø/æ/å must round-trip correctly."""
    headers = _csv_headers(_HEADER_LINE)
    # Data line with Norwegian characters in product name
    norwegian_line = (
        '"NORD-1000";"Nordisk Co.";"Høykvalitets Pådriver Med Æbleskap";'
        '"Rødt utstyr";'
        '"Elektronikk";"9999999999999";"aktiv";"3";"25000";"20000";"22000";"NOK"'
    )
    internal = normalise_row(headers, norwegian_line)

    # Verify that Norwegian characters are preserved, not corrupted
    assert internal["product_name"] == "Høykvalitets Pådriver Med Æbleskap"
    assert internal["short_description"] == "Rødt utstyr"
    assert internal["manufacturer"] == "Nordisk Co."
    pub = public_row(internal)
    assert pub["product_name"] == "Høykvalitets Pådriver Med Æbleskap"


# ---------------------------------------------------------------------------
# Streaming batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_rows_in_configured_batch_size() -> None:
    """With batch_size=1, each batch contains exactly one row."""
    lines = _feed_lines(_DATA_LINE_1, _DATA_LINE_2)
    batches: list[list[dict[str, Any]]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines, batch_size=1):
        batches.append(batch)

    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert len(batches[1]) == 1


@pytest.mark.asyncio
async def test_stream_yields_single_batch_when_batch_size_large() -> None:
    """With batch_size=1000, both rows land in a single batch."""
    lines = _feed_lines(_DATA_LINE_1, _DATA_LINE_2)
    batches: list[list[dict[str, Any]]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines, batch_size=1000):
        batches.append(batch)

    assert len(batches) == 1
    assert len(batches[0]) == 2


@pytest.mark.asyncio
async def test_stream_row_canonical_shape() -> None:
    """Each yielded row must have the expected canonical fields."""
    lines = _feed_lines(_DATA_LINE_1)
    rows: list[dict[str, Any]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines):
        rows.extend(batch)

    assert len(rows) == 1
    row = rows[0]
    assert row["mfr_part_no"] == "AMP-1000"
    assert row["manufacturer"] == "Biamp"
    assert row["product_name"] == "TesiraFORTE AVB VT4"
    # Cost fields must not be present in yielded rows
    for field in _INTERNAL_FIELDS:
        assert field not in row


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_idempotent_same_bytes_same_logical_rows() -> None:
    """Two runs over the same feed bytes must yield identical logical rows."""
    lines = _feed_lines(_DATA_LINE_1, _DATA_LINE_2)

    run1: list[dict[str, Any]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines):
        run1.extend(batch)

    run2: list[dict[str, Any]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines):
        run2.extend(batch)

    assert len(run1) == len(run2)
    for r1, r2 in zip(run1, run2):
        assert r1 == r2


@pytest.mark.asyncio
async def test_stream_dedup_drops_duplicate_keys_in_same_feed() -> None:
    """When the same (manufacturer, mfr_part_no) appears twice, only first is kept."""
    lines = _feed_lines(_DATA_LINE_1, _DATA_LINE_1_DUP, _DATA_LINE_2)
    rows: list[dict[str, Any]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines):
        rows.extend(batch)

    # Line 1 dup must be dropped; total = 2
    assert len(rows) == 2
    product_names = {r["product_name"] for r in rows}
    # The original (first) product_name must be present, not the dup
    assert "TesiraFORTE AVB VT4" in product_names
    assert "TesiraFORTE AVB VT4 (dup)" not in product_names


@pytest.mark.asyncio
async def test_stream_empty_feed_yields_nothing() -> None:
    """A feed with header only yields no batches."""
    lines = [_HEADER_LINE]
    batches: list[list[dict[str, Any]]] = []
    async for batch in stream_nettailer_rows(_mock_lines=lines):
        batches.append(batch)
    assert batches == []


# ---------------------------------------------------------------------------
# Secret GUID never leaked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_guid_not_in_yielded_rows(caplog: pytest.LogCaptureFixture) -> None:
    """The GUID-bearing URL must never appear in log output or row data."""
    lines = _feed_lines(_DATA_LINE_1)
    rows: list[dict[str, Any]] = []

    with caplog.at_level(logging.DEBUG, logger="nce"):
        async for batch in stream_nettailer_rows(
            url=_SECRET_URL,
            _mock_lines=lines,
        ):
            rows.extend(batch)

    # No row value may contain the secret GUID
    for row in rows:
        for val in row.values():
            assert _SECRET_GUID not in str(val), f"Secret GUID leaked into row field: {val!r}"

    # No log record may contain the secret GUID
    for record in caplog.records:
        assert _SECRET_GUID not in record.getMessage(), (
            f"Secret GUID leaked into log: {record.getMessage()!r}"
        )


def test_secret_guid_not_in_field_alias_map_values() -> None:
    """FIELD_ALIAS_MAP values must not reference any URL or token patterns."""
    for val in FIELD_ALIAS_MAP.values():
        assert "http" not in val
        assert "guid" not in val.lower()
        assert "secret" not in val.lower()


# ---------------------------------------------------------------------------
# ValueError when URL is absent and no mock provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_raises_value_error_when_no_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_nettailer_rows must raise ValueError when no URL is configured."""
    monkeypatch.delenv("NCE_PRODUCT_NETTAILER_PRODUCT_URL", raising=False)

    with pytest.raises(DeploymentConfigurationError, match="NCE_PRODUCT_NETTAILER_PRODUCT_URL"):
        async for _ in stream_nettailer_rows(url=""):
            pass
