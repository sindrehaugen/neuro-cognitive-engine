"""
nce/vertical_modules/system_design/sharepoint.py
=================================================
SharePoint document-store adapter for System Design frozen SoW documents
(Wave 10, Phase 1b — off the critical path).

Two concerns, kept separate (SRP / uncle-bob Dependency Rule):

``store_sow(design_id, sow_doc) -> ref``
    POST a frozen SoWDoc to SharePoint and return the opaque reference string
    (SharePoint drive-item id).  Callers persist *only* the ref — never the
    document body.

``fetch_sow(ref) -> SoWDoc``
    GET a previously stored SoWDoc by its opaque reference string.

**Phase 1b no-op contract**:
    When ``NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID`` is unset both functions
    return ``None`` silently.  Phase 1b never gates Phase 1a — callers must
    treat ``None`` as "not stored/not found" and continue normally.

**Credentials**:
    All credentials are read at call time via ``resolve_secret`` (env-only
    accessor).  They are NEVER logged, NEVER stored in module-level state.

Env vars consumed (all via ``resolve_secret``):
    NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID      — SharePoint site id
    NCE_SYSTEM_DESIGN_SHAREPOINT_DRIVE_ID     — drive id (defaults to "root")
    NCE_SYSTEM_DESIGN_SHAREPOINT_FOLDER_PATH  — folder path (defaults to "SoW")
    NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN — Bearer token for Graph API

HTTP:
    httpx.AsyncClient with 30s timeout, routed through
    ``nce.http_resilience.request_with_retry`` for exponential backoff on
    transient errors.

uncle-bob design notes:
    - Domain code (sow.py) has *zero* dependency on this module.
    - This module has *zero* dependency on DB, tools, routes, or admin app.
    - ``store_sow`` and ``fetch_sow`` are separate functions — SRP.
    - Introduce abstraction only when a third duplication appears.
"""

from __future__ import annotations

import json
import logging

import httpx

from nce.config import resolve_secret
from nce.http_resilience import request_with_retry
from nce.vertical_modules.system_design.sow import SoWDoc

log = logging.getLogger("nce.vertical_modules.system_design.sharepoint")

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_DEFAULT_DRIVE_ID = "root"
_DEFAULT_FOLDER_PATH = "SoW"
_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Private: credential resolution
# ---------------------------------------------------------------------------


def _creds() -> tuple[str, str, str, str] | None:
    """Return (site_id, drive_id, folder_path, access_token) or None when unconfigured.

    Credentials are resolved at call time so ``monkeypatch.setenv`` in tests
    takes effect without reloading the module.
    """
    site_id = resolve_secret("NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID")
    if not site_id:
        return None  # Phase 1b not configured — clean no-op
    drive_id = resolve_secret("NCE_SYSTEM_DESIGN_SHAREPOINT_DRIVE_ID") or _DEFAULT_DRIVE_ID
    folder_path = resolve_secret("NCE_SYSTEM_DESIGN_SHAREPOINT_FOLDER_PATH") or _DEFAULT_FOLDER_PATH
    access_token = resolve_secret("NCE_SYSTEM_DESIGN_SHAREPOINT_ACCESS_TOKEN") or ""
    return site_id, drive_id, folder_path, access_token


def _auth_headers(access_token: str) -> dict[str, str]:
    """Build Graph API auth headers — never log the token value."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _upload_url(site_id: str, drive_id: str, folder_path: str, filename: str) -> str:
    """Construct the Graph upload URL for a named file inside the configured folder."""
    if drive_id == "root":
        return f"{_GRAPH_ROOT}/sites/{site_id}/drive/root:/{folder_path}/{filename}:/content"
    return (
        f"{_GRAPH_ROOT}/sites/{site_id}/drives/{drive_id}/root:/{folder_path}/{filename}:/content"
    )


def _download_url(site_id: str, drive_id: str, ref: str) -> str:
    """Construct the Graph download URL for a drive-item id (opaque ref)."""
    if drive_id == "root":
        return f"{_GRAPH_ROOT}/sites/{site_id}/drive/items/{ref}/content"
    return f"{_GRAPH_ROOT}/sites/{site_id}/drives/{drive_id}/items/{ref}/content"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def store_sow(design_id: str, sow_doc: SoWDoc) -> str | None:
    """Store a frozen SoWDoc in SharePoint and return its opaque reference.

    Parameters
    ----------
    design_id:
        The design identifier — used only to derive the filename; not logged
        with any credential value.
    sow_doc:
        The frozen Statement of Work document returned by ``generate_sow`` /
        ``do_generate_sow``.  Only the opaque *reference* (SharePoint
        drive-item id) returned by this function should be persisted by the
        caller; the document body must not be persisted to a DB column.

    Returns
    -------
    str | None
        Opaque SharePoint drive-item id, or ``None`` when credentials are
        unset (Phase 1b not configured).

    Raises
    ------
    ``nce.http_resilience.ExternalAPIError`` subclasses on non-recoverable
    upstream errors.  Transient errors are retried internally.
    """
    creds = _creds()
    if creds is None:
        log.debug("store_sow: NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID unset — no-op")
        return None

    site_id, drive_id, folder_path, access_token = creds
    doc_ref = sow_doc.get("documentRef", design_id)
    filename = f"{doc_ref}.json"
    url = _upload_url(site_id, drive_id, folder_path, filename)
    body = json.dumps(sow_doc).encode("utf-8")

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await request_with_retry(
            client,
            "PUT",
            url,
            operation_name="system_design:sharepoint:store_sow",
            content=body,
            headers=_auth_headers(access_token),
        )

    item_id: str = resp.json().get("id", "")
    log.info(
        "store_sow: stored design_id=%s doc_ref=%s sharepoint_item_id=%s",
        design_id,
        doc_ref,
        item_id,
    )
    return item_id


async def fetch_sow(ref: str) -> SoWDoc | None:
    """Fetch a previously stored SoWDoc from SharePoint by its opaque reference.

    Parameters
    ----------
    ref:
        The opaque SharePoint drive-item id returned by :func:`store_sow`.

    Returns
    -------
    SoWDoc | None
        The deserialized SoW document, or ``None`` when credentials are unset
        (Phase 1b not configured) or when *ref* is empty.
    """
    if not ref:
        return None

    creds = _creds()
    if creds is None:
        log.debug("fetch_sow: NCE_SYSTEM_DESIGN_SHAREPOINT_SITE_ID unset — no-op")
        return None

    site_id, drive_id, _folder_path, access_token = creds
    url = _download_url(site_id, drive_id, ref)

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await request_with_retry(
            client,
            "GET",
            url,
            operation_name="system_design:sharepoint:fetch_sow",
            headers=_auth_headers(access_token),
        )

    doc: SoWDoc = resp.json()
    log.info("fetch_sow: retrieved ref=%s", ref)
    return doc
