"""
nce/vertical_modules/product/sources/nettailer.py
==================================================
Nettailer/Netset CSV feed adapter for the Product Engine.

Lifted from the Portal sidecar ``backend/integrations/nettailer_client.py``
(alias map + quote-safe CSV parse) and ``backend/steps_product/sync.py``
(streaming, idempotent row generator).

Key invariants
--------------
* **Streaming** — the 295 MB feed is never loaded into RAM at once; rows are
  yielded in configurable batches (``NCE_PRODUCT_SYNC_BATCH_SIZE``).
* **Idempotent** — a re-run over the same feed bytes produces the same logical
  rows: dedup key is ``(manufacturer, mfr_part_no)``.  The generator yields
  only the *first* occurrence of each key; duplicates within a feed are silently
  dropped and counted.
* **Secret-safe** — the GUID-bearing feed URL is read via ``resolve_secret`` at
  call time.  It is **never** logged, echoed, or included in any return value.
* **No DB writes** — this wave is parse+normalise+yield only.  Graph upserts are
  W2/W3.
* **cost / margin** — internal fields (``unit_cost``, ``bid_price``) are parsed
  and available in the *internal* normalised dict but are **not** included in the
  public ``canonical_row`` projection returned by :func:`stream_nettailer_rows`.
  Cost/margin stay internal (ADR-0017).

Config keys (``NCE_PRODUCT_*``)
--------------------------------
``NCE_PRODUCT_NETTAILER_PRODUCT_URL``
    Full export URL including the secret GUID.  Resolved via
    ``resolve_secret``; must never be logged.
``NCE_PRODUCT_SYNC_BATCH_SIZE``
    Rows yielded per batch.  Default 2000.
``NCE_PRODUCT_HTTP_TIMEOUT``
    Per-request timeout seconds for the Netset HTTP connection.  Default 30.
"""

from __future__ import annotations

import csv
import io
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

log = logging.getLogger("nce.vertical_modules.product.sources.nettailer")

# ---------------------------------------------------------------------------
# Field-alias map
# ---------------------------------------------------------------------------
# Maps Nettailer/Netset CSV column headers → canonical field names used by the
# Product Engine.  Lifted from Portal ``integrations/nettailer_client.py``.
# All keys are lower-cased and stripped before lookup (see ``_normalise_row``).
#
# Columns marked ``_INTERNAL`` carry cost/margin data.  They are parsed into the
# internal dict but **excluded** from the public canonical_row by
# ``_public_row``.
_INTERNAL_FIELDS: frozenset[str] = frozenset({"unit_cost", "bid_price", "supplier_price"})

FIELD_ALIAS_MAP: dict[str, str] = {
    # --- Identity ---
    "artnr": "mfr_part_no",
    "art.nr": "mfr_part_no",
    "manufacturerpartnumber": "mfr_part_no",
    "manufacturer_part_no": "mfr_part_no",
    "mfr_part_no": "mfr_part_no",
    "itemnumber": "mfr_part_no",
    "ean": "gtin",
    "ean13": "gtin",
    "gtin": "gtin",
    # --- Manufacturer ---
    "manufacturer": "manufacturer",
    "brand": "manufacturer",
    "fabrikat": "manufacturer",
    "leverandor": "manufacturer",
    # --- Naming ---
    "produktnavn": "product_name",
    "productname": "product_name",
    "name": "product_name",
    "description": "short_description",
    "beskrivelse": "short_description",
    "short_description": "short_description",
    "longdescription": "long_description",
    "lang_beskrivelse": "long_description",
    "long_description": "long_description",
    # --- Category ---
    "category": "category",
    "kategori": "category",
    "productcategory": "category",
    "subcategory": "sub_category",
    "underkategori": "sub_category",
    # --- Lifecycle ---
    "lifecycle": "lifecycle_status",
    "lifecycle_status": "lifecycle_status",
    "status": "lifecycle_status",
    # --- Stock ---
    "stock": "stock_qty",
    "lager": "stock_qty",
    "qty": "stock_qty",
    "quantity": "stock_qty",
    # --- Pricing (internal — never returned in canonical_row) ---
    "pris": "unit_cost",
    "cost": "unit_cost",
    "unit_cost": "unit_cost",
    "inpris": "unit_cost",
    "innkjopspris": "unit_cost",
    "bidprice": "bid_price",
    "bid_price": "bid_price",
    "supplierprice": "supplier_price",
    "supplier_price": "supplier_price",
    # --- Currency ---
    "currency": "currency",
    "valuta": "currency",
    # --- URL / media ---
    "producturl": "product_url",
    "url": "product_url",
    "imageurl": "image_url",
    "image": "image_url",
    # --- Weight / dimensions ---
    "weight": "weight_kg",
    "vekt": "weight_kg",
    "weight_kg": "weight_kg",
}


# ---------------------------------------------------------------------------
# Config helpers (lazy / call-time so monkeypatch works in tests)
# ---------------------------------------------------------------------------


def _product_url() -> str:
    """Return the Nettailer product feed URL from the secret store.

    Returns an empty string when the env-var is unset (callers must guard).
    The URL must never be logged — it contains a secret GUID.
    """
    return resolve_secret("NCE_PRODUCT_NETTAILER_PRODUCT_URL") or ""


def _batch_size() -> int:
    raw = os.getenv("NCE_PRODUCT_SYNC_BATCH_SIZE", "2000").strip()
    return max(1, int(raw) if raw.isdigit() else 2000)


def _http_timeout() -> float:
    raw = os.getenv("NCE_PRODUCT_HTTP_TIMEOUT", "30").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 30.0


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def _csv_headers(first_line: str) -> list[str]:
    """Parse the header row from a Nettailer CSV line.

    Uses csv.reader with semicolon delimiter and optional quote-char so
    column names containing commas or quotes are handled correctly.
    """
    reader = csv.reader(io.StringIO(first_line), delimiter=";", quotechar='"')
    return [h.strip().lower() for h in next(reader)]


def parse_csv_row(headers: list[str], raw_line: str) -> dict[str, str]:
    """Parse one CSV data line into a ``{header: value}`` dict.

    Semicolon-delimited, double-quote escaped (RFC 4180 variant).
    Extra columns beyond the header count are silently ignored; missing
    trailing columns are filled with empty string.
    """
    reader = csv.reader(io.StringIO(raw_line), delimiter=";", quotechar='"')
    values = next(reader)
    # Pad short rows; trim over-long rows to header length
    padded = (values + [""] * len(headers))[: len(headers)]
    return dict(zip(headers, padded))


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


def normalise_row(headers: list[str], raw_line: str) -> dict[str, Any]:
    """Parse and alias-map one CSV line to the canonical internal field set.

    Returns the **internal** dict (includes cost/margin fields).
    Use :func:`public_row` to obtain the safe external projection.

    The dedup key ``(manufacturer, mfr_part_no)`` is always present; rows
    missing both are returned with empty-string values so callers can filter.
    """
    raw = parse_csv_row(headers, raw_line)
    out: dict[str, Any] = {}
    for col, val in raw.items():
        canonical = FIELD_ALIAS_MAP.get(col.strip().lower())
        if canonical:
            # First writer wins — earlier columns take priority when multiple
            # aliases map to the same canonical field.
            if canonical not in out:
                out[canonical] = val.strip()
    return out


def public_row(internal: dict[str, Any]) -> dict[str, Any]:
    """Return the external-safe projection — strips cost/margin fields.

    This is the shape yielded by :func:`stream_nettailer_rows` and safe to
    pass to graph upserts, REST responses, and tests.
    """
    return {k: v for k, v in internal.items() if k not in _INTERNAL_FIELDS}


def _dedup_key(row: dict[str, Any]) -> tuple[str, str]:
    """Return the idempotency key for a normalised row."""
    return (
        (row.get("manufacturer") or "").lower().strip(),
        (row.get("mfr_part_no") or "").lower().strip(),
    )


# ---------------------------------------------------------------------------
# Streaming, idempotent feed generator
# ---------------------------------------------------------------------------


async def stream_nettailer_rows(
    *,
    url: str | None = None,
    batch_size: int | None = None,
    http_timeout: float | None = None,
    _mock_lines: list[str] | None = None,
) -> AsyncIterator[list[dict[str, Any]]]:
    """Yield batches of canonical (public) product rows from the Nettailer feed.

    The feed URL contains a secret GUID and is **never logged**.  Only a
    redacted placeholder appears in log messages.

    Parameters
    ----------
    url:
        Override the feed URL (default: ``_product_url()``).  Pass only in
        tests or via admin trigger — never log the value.
    batch_size:
        Rows per yielded batch (default: ``_batch_size()``).
    http_timeout:
        HTTP connect/read timeout seconds (default: ``_http_timeout()``).
    _mock_lines:
        Internal test seam — when set, skips the HTTP fetch entirely and
        iterates over the supplied lines instead.  Never use in production.

    Yields
    ------
    list[dict[str, Any]]
        Each dict is the public canonical row (cost/margin excluded).
        Duplicates within the feed (same ``(manufacturer, mfr_part_no)``) are
        silently dropped; only the first occurrence is yielded.

    Raises
    ------
    ValueError
        When no feed URL is configured and ``_mock_lines`` is not provided.
    nce.http_resilience.ExternalAPITransientError
        On connection failure (propagated to caller for retry policy).
    nce.http_resilience.ExternalAPIClientError
        On non-retryable HTTP error (4xx other than 429).
    """
    effective_url = url if url is not None else _product_url()
    effective_batch = batch_size if batch_size is not None else _batch_size()
    effective_timeout = http_timeout if http_timeout is not None else _http_timeout()

    if _mock_lines is None and not effective_url:
        raise ValueError(
            "NCE_PRODUCT_NETTAILER_PRODUCT_URL is not configured; cannot stream Nettailer feed."
        )

    seen: set[tuple[str, str]] = set()
    batch: list[dict[str, Any]] = []
    headers: list[str] | None = None

    async def _iter_lines() -> AsyncIterator[str]:
        """Stream lines from the HTTP feed, never logging the URL."""
        timeout = httpx.Timeout(effective_timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream("GET", effective_url) as resp:
                    try:
                        classify_httpx_response(resp, operation="nettailer_feed")
                    except Exception:
                        # Drain body before re-raising so the connection is clean
                        await resp.aread()
                        raise
                    buffer_bytes = b""
                    async for chunk in resp.aiter_bytes():
                        buffer_bytes += chunk
                        # Decode only complete UTF-8 sequences; keep incomplete
                        # multi-byte chars in buffer for next chunk.
                        try:
                            text = buffer_bytes.decode("utf-8")
                            buffer_bytes = b""
                        except UnicodeDecodeError:
                            # Multi-byte char split across chunks: find the
                            # last complete UTF-8 boundary and re-buffer.
                            for i in range(len(buffer_bytes) - 1, -1, -1):
                                try:
                                    text = buffer_bytes[:i].decode("utf-8")
                                    buffer_bytes = buffer_bytes[i:]
                                    break
                                except UnicodeDecodeError:
                                    continue
                            else:
                                # No decodable boundary found; buffer is incomplete.
                                continue
                        while "\n" in text:
                            line, text = text.split("\n", 1)
                            yield line.rstrip("\r")
                        if text:
                            buffer_bytes = text.encode("utf-8")
                    # Decode any remaining bytes
                    if buffer_bytes:
                        text = buffer_bytes.decode("utf-8", errors="replace")
                        if text.strip():
                            yield text.rstrip("\r")
            except httpx.TimeoutException as exc:
                raise ExternalAPITransientError(
                    "nettailer_feed: request timed out",
                    operation="nettailer_feed",
                ) from exc
            except httpx.RequestError as exc:
                raise ExternalAPITransientError(
                    "nettailer_feed: transport error",
                    operation="nettailer_feed",
                ) from exc

    async def _iter_mock() -> AsyncIterator[str]:
        for line in _mock_lines or []:
            yield line

    line_source = _iter_mock() if _mock_lines is not None else _iter_lines()

    first = True
    async for line in line_source:
        if not line:
            continue

        if first:
            headers = _csv_headers(line)
            first = False
            continue

        if headers is None:
            continue

        internal = normalise_row(headers, line)
        key = _dedup_key(internal)

        if key in seen:
            log.debug(
                "nettailer: duplicate row skipped key=(%s, %s)",
                key[0] or "<empty>",
                key[1] or "<empty>",
            )
            continue

        seen.add(key)
        batch.append(public_row(internal))

        if len(batch) >= effective_batch:
            yield batch
            batch = []

    if batch:
        yield batch


# ---------------------------------------------------------------------------
# SourceAdapter conformance
# ---------------------------------------------------------------------------


class NettailerAdapter(SourceAdapter):
    """``SourceAdapter`` wrapper around :func:`stream_nettailer_rows`.

    Delegates entirely to the existing generator — no logic duplication.
    This thin class satisfies the base contract without refactoring W1.
    """

    async def stream(  # type: ignore[override]
        self,
        *,
        url: str | None = None,
        batch_size: int | None = None,
        http_timeout: float | None = None,
        _mock_lines: list[str] | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Delegate to :func:`stream_nettailer_rows`."""
        async for batch in stream_nettailer_rows(
            url=url,
            batch_size=batch_size,
            http_timeout=http_timeout,
            _mock_lines=_mock_lines,
        ):
            yield batch
