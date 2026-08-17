"""
nce/vertical_modules/product/sources/manufacturer_api.py
=========================================================
Manufacturer JSON-API adapter for the Product Engine.

Fetches products from a manufacturer's REST API and normalises their native
JSON format to the same canonical ``PRODUCT`` row shape produced by the
Nettailer adapter — so manufacturer-API products and Nettailer-CSV products
land as identical node shapes in the Product Engine.

Config keys (``NCE_PRODUCT_<MFR>_*``)
---------------------------------------
``NCE_PRODUCT_MFR_API_KEY``
    Bearer-token API key for the manufacturer endpoint.  Read via
    ``resolve_secret``; must **never** be logged or echoed.
``NCE_PRODUCT_MFR_API_URL``
    Base URL for the manufacturer product catalogue endpoint.  Read via
    ``resolve_secret``; must **never** be logged (may contain auth tokens).
``NCE_PRODUCT_MFR_BATCH_SIZE``
    Rows per yielded batch.  Default 200.
``NCE_PRODUCT_MFR_HTTP_TIMEOUT``
    Per-request timeout seconds.  Default 30.

No-op behaviour
---------------
When ``NCE_PRODUCT_MFR_API_KEY`` **or** ``NCE_PRODUCT_MFR_API_URL`` is unset,
:meth:`ManufacturerApiAdapter.stream` yields nothing and logs a single
``INFO`` message (without revealing why — no key name in the value position).

Canonical row shape
-------------------
The manufacturer API returns JSON objects.  This adapter normalises the
following native fields to the shared canonical names:

    part_number       → mfr_part_no
    brand             → manufacturer
    name              → product_name
    description       → short_description
    long_description  → long_description
    category          → category
    sub_category      → sub_category
    status            → lifecycle_status
    stock             → stock_qty
    currency          → currency
    ean               → gtin
    product_url       → product_url
    image_url         → image_url
    weight_kg         → weight_kg

Cost/margin fields (``cost``, ``bid_price``, ``supplier_price``) are parsed
internally but **never** included in yielded rows (ADR-0017).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nce.config import resolve_secret
from nce.http_resilience import (
    ExternalAPITransientError,
    classify_httpx_response,
)
from nce.vertical_modules.product.sources.base import SourceAdapter

log = logging.getLogger("nce.vertical_modules.product.sources.manufacturer_api")

# ---------------------------------------------------------------------------
# Internal-only fields — stripped before yielding (ADR-0017)
# ---------------------------------------------------------------------------
_INTERNAL_FIELDS: frozenset[str] = frozenset({"unit_cost", "bid_price", "supplier_price"})

# ---------------------------------------------------------------------------
# Native-field → canonical-field map
# ---------------------------------------------------------------------------
_FIELD_MAP: dict[str, str] = {
    "part_number": "mfr_part_no",
    "brand": "manufacturer",
    "name": "product_name",
    "description": "short_description",
    "long_description": "long_description",
    "category": "category",
    "sub_category": "sub_category",
    "status": "lifecycle_status",
    "stock": "stock_qty",
    "currency": "currency",
    "ean": "gtin",
    "product_url": "product_url",
    "image_url": "image_url",
    "weight_kg": "weight_kg",
    # Internal cost / margin — parsed but stripped before yield
    "cost": "unit_cost",
    "bid_price": "bid_price",
    "supplier_price": "supplier_price",
}


# ---------------------------------------------------------------------------
# Config helpers (lazy / call-time so monkeypatch works in tests)
# ---------------------------------------------------------------------------


def _api_key() -> str:
    """Return the manufacturer API key from the secret store.

    Returns an empty string when the env-var is unset.
    The key must never be logged.
    """
    return resolve_secret("NCE_PRODUCT_MFR_API_KEY") or ""


def _api_url() -> str:
    """Return the manufacturer API base URL from the secret store.

    Returns an empty string when the env-var is unset.
    The URL must never be logged.
    """
    return resolve_secret("NCE_PRODUCT_MFR_API_URL") or ""


def _batch_size() -> int:
    raw = os.getenv("NCE_PRODUCT_MFR_BATCH_SIZE", "200").strip()
    return max(1, int(raw) if raw.isdigit() else 200)


def _http_timeout() -> float:
    raw = os.getenv("NCE_PRODUCT_MFR_HTTP_TIMEOUT", "30").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 30.0


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


def _normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map a manufacturer JSON item to the canonical internal field set.

    Returns the **internal** dict (may include cost/margin).
    Use :func:`_public_row` to obtain the safe public projection.
    """
    out: dict[str, Any] = {}
    for native_key, value in item.items():
        canonical = _FIELD_MAP.get(native_key)
        if canonical and canonical not in out:
            out[canonical] = str(value).strip() if value is not None else ""
    return out


def _public_row(internal: dict[str, Any]) -> dict[str, Any]:
    """Strip internal cost/margin fields — returns the public canonical row."""
    return {k: v for k, v in internal.items() if k not in _INTERNAL_FIELDS}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ManufacturerApiAdapter(SourceAdapter):
    """Product source adapter for a manufacturer's JSON REST API.

    Gated by ``NCE_PRODUCT_MFR_API_KEY`` and ``NCE_PRODUCT_MFR_API_URL``:
    when either is unset the adapter is a no-op (yields nothing).
    """

    async def stream(  # type: ignore[override]
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        batch_size: int | None = None,
        http_timeout: float | None = None,
        _mock_items: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield batches of canonical product rows from the manufacturer API.

        Parameters
        ----------
        api_key:
            Override the API key (test seam only — never log).
        api_url:
            Override the API URL (test seam only — never log).
        batch_size:
            Rows per yielded batch (default: ``_batch_size()``).
        http_timeout:
            HTTP timeout seconds (default: ``_http_timeout()``).
        _mock_items:
            Internal test seam — when set, skips HTTP entirely and processes
            the supplied list of JSON objects.  Never use in production.

        Yields
        ------
        list[dict[str, Any]]
            Each dict is the public canonical row (cost/margin excluded).
        """
        effective_key = api_key if api_key is not None else _api_key()
        effective_url = api_url if api_url is not None else _api_url()
        effective_batch = batch_size if batch_size is not None else _batch_size()
        effective_timeout = http_timeout if http_timeout is not None else _http_timeout()

        if _mock_items is None and (not effective_key or not effective_url):
            log.info("manufacturer_api: required config absent — adapter is disabled (no-op)")
            return

        items: list[dict[str, Any]]

        if _mock_items is not None:
            items = _mock_items
        else:
            items = await _fetch_items(
                effective_url,
                effective_key,
                timeout=effective_timeout,
            )

        batch: list[dict[str, Any]] = []
        for item in items:
            internal = _normalise_item(item)
            pub = _public_row(internal)
            batch.append(pub)
            if len(batch) >= effective_batch:
                yield batch
                batch = []

        if batch:
            yield batch


async def _fetch_items(
    url: str,
    api_key: str,
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    """Fetch the product JSON array from the manufacturer API.

    The URL and API key are **never logged**.

    Raises
    ------
    nce.http_resilience.ExternalAPITransientError
        On connection failure or 5xx / 429.
    nce.http_resilience.ExternalAPIClientError
        On non-retryable 4xx.
    """
    timeout_config = httpx.Timeout(timeout)
    async with httpx.AsyncClient(timeout=timeout_config) as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise ExternalAPITransientError(
                "manufacturer_api: request timed out",
                operation="manufacturer_api",
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalAPITransientError(
                "manufacturer_api: transport error",
                operation="manufacturer_api",
            ) from exc

        classify_httpx_response(resp, operation="manufacturer_api")
        data = resp.json()

    # Accept either a top-level list or a dict with a "products" key
    if isinstance(data, list):
        return data  # type: ignore[return-value]
    if isinstance(data, dict):
        return data.get("products", [])  # type: ignore[return-value]
    return []
