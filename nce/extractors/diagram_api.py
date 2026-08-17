"""J.14 — Miro / Lucidchart REST extraction (OAuth bearer); not file-byte routed."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from nce.extractors.core import ExtractionResult, Section, empty_skipped
from nce.net_safety import BridgeURLValidationError, validate_extractor_url
from nce.observability import inject_trace_headers

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_MIRO_PAGES = int(os.environ.get("NCE_MIRO_MAX_PAGES", "100"))


def _text_from_miro_item(item: dict[str, Any]) -> str | None:
    try:
        data = item.get("data") or {}
        if isinstance(data, dict):
            for key in ("content", "title", "text"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        # geometry labels
        if item.get("type") == "text" and isinstance(data, dict):
            c = data.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
    except Exception as e:
        log.debug("miro_item_text: %s", e)
    return None


async def miro_extract_board(
    board_id: str,
    *,
    access_token: str | None = None,
    base_url: str = "https://api.miro.com/v2",
) -> ExtractionResult:
    """
    Paginated Miro board items → Sections by item type.
    Token: ``access_token`` or env ``NCE_MIRO_ACCESS_TOKEN`` / ``MIRO_ACCESS_TOKEN``.
    """
    warnings: list[str] = []
    token = (
        access_token
        or os.environ.get("NCE_MIRO_ACCESS_TOKEN")
        or os.environ.get("MIRO_ACCESS_TOKEN")
    )
    if not token:
        return empty_skipped(
            "miro_api",
            "no_token",
            warnings=["Set NCE_MIRO_ACCESS_TOKEN (or pass access_token=) for Miro extraction"],
        )
    # SSRF guard: validate base_url before any outbound request
    try:
        await validate_extractor_url(base_url)
    except BridgeURLValidationError as e:
        return empty_skipped("miro_api", "ssrf_blocked", warnings=[str(e)])
    if not _BOARD_ID_RE.match(board_id):
        return empty_skipped(
            "miro_api",
            "invalid_board_id",
            warnings=[f"invalid board_id: {board_id!r}"],
        )
    safe_board_id = quote(board_id, safe="")
    sections: list[Section] = []
    order = 0
    cursor: str | None = None
    page = 0
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            while True:
                page += 1
                if page > _MAX_MIRO_PAGES:
                    warnings.append(f"miro_page_limit: capped at {_MAX_MIRO_PAGES} pages")
                    break
                params: dict[str, str] = {"limit": "50"}
                if cursor:
                    params["cursor"] = cursor
                url = f"{base_url.rstrip('/')}/boards/{safe_board_id}/items"
                try:
                    r = await client.get(
                        url,
                        params=params,
                        headers=inject_trace_headers(
                            {
                                "Authorization": f"Bearer {token}",
                                "Accept": "application/json",
                            }
                        ),
                    )
                    r.raise_for_status()
                    payload = r.json()
                except httpx.HTTPStatusError as e:
                    log.warning("miro_http_error: %s", e)
                    return empty_skipped(
                        "miro_api",
                        "http_error",
                        warnings=[
                            f"Miro API HTTP {e.response.status_code}: {e.response.text[:300]}"
                        ],
                    )
                except (httpx.RequestError, json.JSONDecodeError, ValueError) as e:
                    log.warning("miro_request_failed: %s", e)
                    return empty_skipped("miro_api", "request_failed", warnings=[str(e)])

                items = payload.get("data") or []
                if not isinstance(items, list):
                    warnings.append("miro_unexpected_payload_shape")
                    break
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    t = _text_from_miro_item(it)
                    itype = str(it.get("type") or "item")
                    if not t:
                        continue
                    sections.append(
                        Section(
                            text=t,
                            structure_path=f"Miro:{itype}:{it.get('id', '')}",
                            section_type="diagram",
                            order=order,
                        )
                    )
                    order += 1
                cursor = (payload.get("cursor") or "").strip() or None
                if not cursor:
                    break
    except Exception as e:
        log.warning("miro_extract_failed: %s", e, exc_info=True)
        return empty_skipped("miro_api", "extraction_failed", warnings=[str(e)])

    full = "\n\n".join(s.text for s in sections)
    if not full.strip():
        warnings.append("miro_no_text_items")
    return ExtractionResult(
        method="miro_api",
        text=full,
        sections=sections,
        metadata={"board_id": board_id, "section_count": len(sections)},
        warnings=warnings,
    )


def _text_from_lucid_part(obj: Any, depth: int = 0) -> list[str]:
    if depth > 18:
        return []
    out: list[str] = []
    if isinstance(obj, str) and obj.strip():
        lowered = obj.strip().lower()
        if lowered not in ("title", "text", "name") and len(obj.strip()) > 1:
            out.append(obj.strip())
    elif isinstance(obj, dict):
        for k in ("title", "name", "text", "content", "label", "description"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for v in obj.values():
            out.extend(_text_from_lucid_part(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_text_from_lucid_part(v, depth + 1))
    return out


async def lucidchart_extract_document(
    document_id: str,
    *,
    access_token: str | None = None,
    base_url: str = "https://api.lucid.co",
) -> ExtractionResult:
    """
    Lucid Suite document JSON → flattened text Sections (best-effort).
    Token: ``access_token`` or env ``NCE_LUCID_ACCESS_TOKEN`` / ``LUCID_ACCESS_TOKEN``.
    """
    warnings: list[str] = []
    token = (
        access_token
        or os.environ.get("NCE_LUCID_ACCESS_TOKEN")
        or os.environ.get("LUCID_ACCESS_TOKEN")
    )
    if not token:
        return empty_skipped(
            "lucid_api",
            "no_token",
            warnings=["Set NCE_LUCID_ACCESS_TOKEN (or pass access_token=) for Lucid extraction"],
        )
    # SSRF guard: validate base_url before any outbound request
    try:
        await validate_extractor_url(base_url)
    except BridgeURLValidationError as e:
        return empty_skipped("lucid_api", "ssrf_blocked", warnings=[str(e)])
    url = f"{base_url.rstrip('/')}/documents/{document_id}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            r = await client.get(
                url,
                headers=inject_trace_headers(
                    {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                ),
            )
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPStatusError as e:
        return empty_skipped(
            "lucid_api",
            "http_error",
            warnings=[f"Lucid API HTTP {e.response.status_code}: {e.response.text[:300]}"],
        )
    except (httpx.RequestError, json.JSONDecodeError, ValueError) as e:
        return empty_skipped("lucid_api", "request_failed", warnings=[str(e)])
    except Exception as e:
        log.warning("lucid_extract_failed: %s", e, exc_info=True)
        return empty_skipped("lucid_api", "extraction_failed", warnings=[str(e)])

    texts = _text_from_lucid_part(payload)
    seen: set[str] = set()
    uniq: list[str] = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    sections: list[Section] = []
    for i, t in enumerate(uniq):
        sections.append(
            Section(
                text=t,
                structure_path=f"Lucid:{document_id}:{i + 1}",
                section_type="diagram",
                order=i,
            )
        )
    full = "\n\n".join(uniq)
    if not full.strip():
        warnings.append("lucid_no_text_found")
    return ExtractionResult(
        method="lucid_api",
        text=full,
        sections=sections,
        metadata={"document_id": document_id, "section_count": len(sections)},
        warnings=warnings,
    )
