"""
tests/unit/test_product_source_adapter.py
==========================================
Unit tests for the ``SourceAdapter`` base contract and the ``ManufacturerApiAdapter``
(Wave 11 acceptance gate).

All tests are pure-logic / no-network: the ``_mock_items`` seam is used for the
manufacturer adapter; nettailer conformance is tested via ``NettailerAdapter``'s
``_mock_lines`` seam.

Covers
------
* ``SourceAdapter`` is an ABC that cannot be instantiated directly.
* ``ManufacturerApiAdapter`` is a concrete subclass of ``SourceAdapter``.
* ``NettailerAdapter`` is a concrete subclass of ``SourceAdapter``.
* Manufacturer adapter normalises a sample JSON payload to the same canonical
  field names as the nettailer adapter (identity + descriptive fields; no cost).
* Manufacturer adapter is a no-op (yields nothing) when ``NCE_PRODUCT_MFR_API_KEY``
  or ``NCE_PRODUCT_MFR_API_URL`` is absent.
* Manufacturer adapter never logs the API key or URL.
* ``base.py`` imports nothing from web/admin/DB (pure boundary check).
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest

from nce.vertical_modules.product.sources.base import SourceAdapter
from nce.vertical_modules.product.sources.manufacturer_api import (
    _INTERNAL_FIELDS,
    ManufacturerApiAdapter,
    _normalise_item,
    _public_row,
)
from nce.vertical_modules.product.sources.nettailer import NettailerAdapter

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# A sample manufacturer JSON item with the native field names produced by the
# manufacturer API.  Includes internal cost fields to confirm they are stripped.
_SAMPLE_MFR_ITEM: dict[str, Any] = {
    "part_number": "AMP-1000",
    "brand": "Biamp",
    "name": "TesiraFORTE AVB VT4",
    "description": "DSP conferencing unit",
    "long_description": "Full-featured DSP for conferencing",
    "category": "DSP",
    "sub_category": "Conferencing",
    "status": "active",
    "stock": "5",
    "currency": "NOK",
    "ean": "1234567890123",
    "product_url": "https://www.biamp.com/tesiraforte",
    "image_url": "https://cdn.biamp.com/tesiraforte.jpg",
    "weight_kg": "2.5",
    # Cost / margin — must be stripped before yield
    "cost": "15000",
    "bid_price": "12000",
    "supplier_price": "13000",
}

# The expected canonical public shape for the sample item above.
# Must match the field names produced by the nettailer adapter for the same
# logical product (see _DATA_LINE_1 in test_product_nettailer.py).
_EXPECTED_PUBLIC: dict[str, str] = {
    "mfr_part_no": "AMP-1000",
    "manufacturer": "Biamp",
    "product_name": "TesiraFORTE AVB VT4",
    "short_description": "DSP conferencing unit",
    "long_description": "Full-featured DSP for conferencing",
    "category": "DSP",
    "sub_category": "Conferencing",
    "lifecycle_status": "active",
    "stock_qty": "5",
    "currency": "NOK",
    "gtin": "1234567890123",
    "product_url": "https://www.biamp.com/tesiraforte",
    "image_url": "https://cdn.biamp.com/tesiraforte.jpg",
    "weight_kg": "2.5",
}

_SECRET_API_KEY = "super-secret-mfr-api-key-xyz"
_SECRET_API_URL = "https://api.mfr.example.com/v1/products?token=abc123"

# ---------------------------------------------------------------------------
# SourceAdapter base contract
# ---------------------------------------------------------------------------


def test_source_adapter_is_abstract() -> None:
    """``SourceAdapter`` must be an ABC and cannot be instantiated directly."""
    assert inspect.isabstract(SourceAdapter)
    with pytest.raises(TypeError):
        SourceAdapter()  # type: ignore[abstract]


def test_manufacturer_adapter_is_source_adapter_subclass() -> None:
    """``ManufacturerApiAdapter`` must be a concrete ``SourceAdapter`` subclass."""
    assert issubclass(ManufacturerApiAdapter, SourceAdapter)
    assert not inspect.isabstract(ManufacturerApiAdapter)


def test_nettailer_adapter_is_source_adapter_subclass() -> None:
    """``NettailerAdapter`` must be a concrete ``SourceAdapter`` subclass."""
    assert issubclass(NettailerAdapter, SourceAdapter)
    assert not inspect.isabstract(NettailerAdapter)


# ---------------------------------------------------------------------------
# Pure boundary: base.py must not import web/admin/DB
# ---------------------------------------------------------------------------


def test_base_module_has_no_web_or_db_imports() -> None:
    """``base.py`` must only import from stdlib + typing — pure boundary."""
    import nce.vertical_modules.product.sources.base as base_mod

    forbidden_prefixes = (
        "httpx",
        "sqlalchemy",
        "asyncpg",
        "fastapi",
        "starlette",
        "django",
        "nce.db",
        "nce.config",
        "nce.http_resilience",
        "nce.providers",
    )
    for name, obj in vars(base_mod).items():
        if isinstance(obj, type(base_mod)):  # sub-module
            mod_name = getattr(obj, "__name__", "")
            for prefix in forbidden_prefixes:
                assert not mod_name.startswith(prefix), (
                    f"base.py imports forbidden module: {mod_name}"
                )


# ---------------------------------------------------------------------------
# Normalisation: manufacturer adapter → canonical shape
# ---------------------------------------------------------------------------


def test_normalise_item_maps_to_canonical_fields() -> None:
    """``_normalise_item`` must map the manufacturer's native keys to canonical names."""
    internal = _normalise_item(_SAMPLE_MFR_ITEM)

    assert internal["mfr_part_no"] == "AMP-1000"
    assert internal["manufacturer"] == "Biamp"
    assert internal["product_name"] == "TesiraFORTE AVB VT4"
    assert internal["short_description"] == "DSP conferencing unit"
    assert internal["category"] == "DSP"
    assert internal["gtin"] == "1234567890123"
    assert internal["lifecycle_status"] == "active"
    assert internal["stock_qty"] == "5"
    # Internal cost fields present in internal dict
    assert internal["unit_cost"] == "15000"
    assert internal["bid_price"] == "12000"
    assert internal["supplier_price"] == "13000"


def test_public_row_strips_cost_fields() -> None:
    """``_public_row`` must strip all cost/margin fields from the internal dict."""
    internal = _normalise_item(_SAMPLE_MFR_ITEM)
    pub = _public_row(internal)

    for field in _INTERNAL_FIELDS:
        assert field not in pub, f"internal field '{field}' leaked into public row"

    # Identity fields must still be present
    assert "mfr_part_no" in pub
    assert "manufacturer" in pub


def test_public_row_matches_expected_canonical_shape() -> None:
    """The manufacturer public row must match the expected canonical shape exactly."""
    internal = _normalise_item(_SAMPLE_MFR_ITEM)
    pub = _public_row(internal)

    for field, expected_value in _EXPECTED_PUBLIC.items():
        assert field in pub, f"canonical field '{field}' missing from public row"
        assert pub[field] == expected_value, (
            f"field '{field}': got {pub[field]!r}, expected {expected_value!r}"
        )


def test_canonical_shape_matches_nettailer_field_names() -> None:
    """Manufacturer canonical field names must be a subset of nettailer's canonical names."""
    from nce.vertical_modules.product.sources.nettailer import FIELD_ALIAS_MAP

    nettailer_canonical = set(FIELD_ALIAS_MAP.values())
    # Cost/margin are valid nettailer canonicals too (internal only)
    for field in _EXPECTED_PUBLIC:
        assert field in nettailer_canonical, (
            f"manufacturer canonical field '{field}' is not in nettailer's canonical set"
        )


# ---------------------------------------------------------------------------
# Streaming via adapter.stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manufacturer_adapter_streams_canonical_rows() -> None:
    """``ManufacturerApiAdapter.stream`` must yield canonical public rows."""
    adapter = ManufacturerApiAdapter()
    rows: list[dict[str, Any]] = []
    async for batch in adapter.stream(_mock_items=[_SAMPLE_MFR_ITEM]):
        rows.extend(batch)

    assert len(rows) == 1
    row = rows[0]
    assert row["mfr_part_no"] == "AMP-1000"
    assert row["manufacturer"] == "Biamp"
    # No cost/margin in yielded rows
    for field in _INTERNAL_FIELDS:
        assert field not in row, f"internal field '{field}' leaked into streamed row"


@pytest.mark.asyncio
async def test_manufacturer_adapter_batches_rows() -> None:
    """With batch_size=1, each batch contains exactly one item."""
    items = [_SAMPLE_MFR_ITEM, {**_SAMPLE_MFR_ITEM, "part_number": "AMP-2000"}]
    adapter = ManufacturerApiAdapter()
    batches: list[list[dict[str, Any]]] = []
    async for batch in adapter.stream(_mock_items=items, batch_size=1):
        batches.append(batch)

    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert len(batches[1]) == 1


@pytest.mark.asyncio
async def test_manufacturer_adapter_empty_mock_yields_nothing() -> None:
    """An empty ``_mock_items`` list must produce no batches."""
    adapter = ManufacturerApiAdapter()
    batches: list[list[dict[str, Any]]] = []
    async for batch in adapter.stream(_mock_items=[]):
        batches.append(batch)
    assert batches == []


# ---------------------------------------------------------------------------
# No-op when config key is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_is_noop_when_api_key_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter must yield nothing when ``NCE_PRODUCT_MFR_API_KEY`` is unset."""
    monkeypatch.delenv("NCE_PRODUCT_MFR_API_KEY", raising=False)
    monkeypatch.setenv("NCE_PRODUCT_MFR_API_URL", "https://api.mfr.example.com/products")

    adapter = ManufacturerApiAdapter()
    batches: list[list[dict[str, Any]]] = []
    async for batch in adapter.stream():
        batches.append(batch)

    assert batches == [], "adapter must be a no-op when API key is absent"


@pytest.mark.asyncio
async def test_adapter_is_noop_when_api_url_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter must yield nothing when ``NCE_PRODUCT_MFR_API_URL`` is unset."""
    monkeypatch.setenv("NCE_PRODUCT_MFR_API_KEY", "some-key")
    monkeypatch.delenv("NCE_PRODUCT_MFR_API_URL", raising=False)

    adapter = ManufacturerApiAdapter()
    batches: list[list[dict[str, Any]]] = []
    async for batch in adapter.stream():
        batches.append(batch)

    assert batches == [], "adapter must be a no-op when API URL is absent"


@pytest.mark.asyncio
async def test_adapter_is_noop_when_both_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter must yield nothing when both config keys are absent."""
    monkeypatch.delenv("NCE_PRODUCT_MFR_API_KEY", raising=False)
    monkeypatch.delenv("NCE_PRODUCT_MFR_API_URL", raising=False)

    adapter = ManufacturerApiAdapter()
    batches: list[list[dict[str, Any]]] = []
    async for batch in adapter.stream():
        batches.append(batch)

    assert batches == []


# ---------------------------------------------------------------------------
# Secret never logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The API key must never appear in any log output."""
    adapter = ManufacturerApiAdapter()
    rows: list[dict[str, Any]] = []

    with caplog.at_level(logging.DEBUG, logger="nce"):
        async for batch in adapter.stream(
            api_key=_SECRET_API_KEY,
            api_url=_SECRET_API_URL,
            _mock_items=[_SAMPLE_MFR_ITEM],
        ):
            rows.extend(batch)

    for record in caplog.records:
        assert _SECRET_API_KEY not in record.getMessage(), (
            f"API key leaked into log: {record.getMessage()!r}"
        )


@pytest.mark.asyncio
async def test_api_key_not_in_yielded_rows(caplog: pytest.LogCaptureFixture) -> None:
    """The API key must never appear in any yielded row value."""
    adapter = ManufacturerApiAdapter()
    rows: list[dict[str, Any]] = []

    with caplog.at_level(logging.DEBUG, logger="nce"):
        async for batch in adapter.stream(
            api_key=_SECRET_API_KEY,
            api_url=_SECRET_API_URL,
            _mock_items=[_SAMPLE_MFR_ITEM],
        ):
            rows.extend(batch)

    for row in rows:
        for val in row.values():
            assert _SECRET_API_KEY not in str(val), f"API key leaked into row field: {val!r}"


@pytest.mark.asyncio
async def test_api_url_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The API URL must never appear in any log output."""
    adapter = ManufacturerApiAdapter()

    with caplog.at_level(logging.DEBUG, logger="nce"):
        async for _ in adapter.stream(
            api_key=_SECRET_API_KEY,
            api_url=_SECRET_API_URL,
            _mock_items=[_SAMPLE_MFR_ITEM],
        ):
            pass

    for record in caplog.records:
        # Check that the token in the URL is not leaked
        assert "abc123" not in record.getMessage(), (
            f"API URL token leaked into log: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# NettailerAdapter conformance via base contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nettailer_adapter_stream_via_base_contract() -> None:
    """``NettailerAdapter.stream`` must satisfy the ``SourceAdapter`` base contract."""
    _HEADER = (
        "artnr;manufacturer;produktnavn;description;category;ean;"
        "lifecycle;stock;pris;bid_price;supplier_price;currency"
    )
    _ROW = (
        '"AMP-1000";"Biamp";"TesiraFORTE AVB VT4";"DSP conferencing unit";'
        '"DSP";"1234567890123";"aktiv";"5";"15000";"12000";"13000";"NOK"'
    )

    adapter: SourceAdapter = NettailerAdapter()
    rows: list[dict[str, Any]] = []
    async for batch in adapter.stream(_mock_lines=[_HEADER, _ROW]):
        rows.extend(batch)

    assert len(rows) == 1
    row = rows[0]
    assert row["mfr_part_no"] == "AMP-1000"
    assert row["manufacturer"] == "Biamp"
    # No cost/margin
    for field in {"unit_cost", "bid_price", "supplier_price"}:
        assert field not in row
