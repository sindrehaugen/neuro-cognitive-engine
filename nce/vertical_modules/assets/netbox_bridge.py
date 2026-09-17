"""
nce/vertical_modules/assets/netbox_bridge.py
=============================================
NetBox DCIM bridge for the Assets vertical module (Wave A-3).

Role: **Operator / Bridge** — reconciles NCE assets with NetBox devices
using a 4-stage match cascade (serial, custom field, location, fuzzy name)
and writes bidirectional ``maps_to`` / ``mapped_to_asset`` edges to ``kg_edges``.

Invariants:
  - NetBox owns DCIM truth (rack, IP, cabling, site); Assets owns lifecycle,
    health, and warranty.
  - Zero DDL migrations (Q-4 compliance): mappings live as graph edges in
    ``kg_edges`` without requiring a new relational mapping table.
  - Single-writer ownership invariant: does not mutate node ownership.
  - Graceful degradation: when NetBox is unconfigured or unreachable, degrades
    cleanly without crashing the engine.
  - Resilient HTTP: uses ``request_with_retry`` for all external calls.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any
from uuid import UUID

import httpx

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.http_resilience import request_with_retry
from nce.mcp_args import require_namespace_id

log = logging.getLogger("nce.vertical_modules.assets.netbox_bridge")

# Default fuzzy matching threshold
_DEFAULT_FUZZY_THRESHOLD: float = 0.85

# Predicates for graph edges
_PRED_MAPS_TO: str = "maps_to"
_PRED_MAPPED_TO_ASSET: str = "mapped_to_asset"

# Change origin tag for bridge writes
_CHANGE_ORIGIN: str = "agent"


def _normalize(name: str | None) -> str:
    """Lower-case, strip, collapse internal whitespace."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _fuzzy_ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio between two normalised strings (0.0-1.0)."""
    norm_a = _normalize(a)
    norm_b = _normalize(b)
    if not norm_a or not norm_b:
        return 0.0
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio()


class NetBoxAssetClient:
    """Minimal async REST client for NetBox DCIM device endpoints."""

    def __init__(self, base_url: str, token: str, page_size: int = 500) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Token {token}",
        }
        self._page_size = page_size

    async def fetch_devices(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Paginate and return NetBox devices."""
        results: list[dict[str, Any]] = []
        next_url: str | None = f"{self._base}/api/dcim/devices/?limit={self._page_size}&offset=0"

        async with httpx.AsyncClient(timeout=30.0) as client:
            while next_url:
                resp = await request_with_retry(
                    client,
                    "GET",
                    next_url,
                    headers=self._headers,
                    operation_name="netbox:fetch_devices",
                )
                resp.raise_for_status()
                body = resp.json()
                items = body.get("results") or []
                results.extend(items)
                if limit and len(results) >= limit:
                    return results[:limit]
                next_url = body.get("next")
        return results


async def do_sync_netbox(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Reconcile assets with NetBox DCIM devices and write graph edges.

    Match cascade:
      1. Exact serial: asset ``serial`` == device ``serial`` (confidence 1.0)
      2. Custom fields: device ``custom_fields.asset_id`` or ``custom_fields.bom_line_id`` (confidence 1.0)
      3. Location: device ``site``/``location`` matches asset ``functional_location_id`` (confidence 0.95)
      4. Fuzzy name: SequenceMatcher ratio between asset serial/bom_line_id and device name (confidence >= threshold)

    Parameters
    ----------
    engine:
        NCEEngine instance with ``pg_pool``.
    params:
        - ``namespace_id`` (UUID | str, required): tenant namespace.
        - ``fuzzy_threshold`` (float, optional): minimum ratio for fuzzy match.
        - ``limit`` (int, optional): limit of NetBox devices to fetch.
        - ``strict`` (bool, optional): if true, raise on NetBox connectivity failure.

    Returns
    -------
    dict
        Reconciliation summary and list of matches.
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = UUID(str(namespace_id))

    threshold = float(
        params.get("fuzzy_threshold")
        or getattr(cfg, "NCE_ASSETS_NETBOX_FUZZY_THRESHOLD", _DEFAULT_FUZZY_THRESHOLD)
    )
    dev_limit = int(params["limit"]) if params.get("limit") is not None else None
    strict = bool(params.get("strict", False))

    # Read assets from database
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        asset_rows = await conn.fetch(
            """
            SELECT id, namespace_id, bom_line_id, serial, functional_location_id,
                   lifecycle_state, created_at, updated_at
            FROM assets
            WHERE namespace_id = $1::uuid
            ORDER BY created_at ASC
            """,
            str(ns_uuid),
        )

    if not asset_rows:
        return {
            "ok": True,
            "synced": True,
            "namespace_id": str(ns_uuid),
            "assets_scanned": 0,
            "devices_fetched": 0,
            "matched_count": 0,
            "edges_written": 0,
            "matches": [],
        }

    # Verify NetBox credentials
    netbox_url = getattr(cfg, "NCE_NETBOX_URL", "").strip()
    netbox_token = getattr(cfg, "NCE_NETBOX_TOKEN", "").strip()

    if not netbox_url or not netbox_token:
        log.warning("assets_sync_netbox: NetBox URL or token not configured; sync skipped")
        return {
            "ok": True,
            "synced": False,
            "reason": "NetBox URL or token not configured",
            "namespace_id": str(ns_uuid),
            "assets_scanned": len(asset_rows),
            "devices_fetched": 0,
            "matched_count": 0,
            "edges_written": 0,
            "matches": [],
        }

    # Fetch devices from NetBox
    client = NetBoxAssetClient(base_url=netbox_url, token=netbox_token)
    try:
        devices = await client.fetch_devices(limit=dev_limit)
    except Exception as exc:
        log.warning("assets_sync_netbox: failed to fetch NetBox devices: %s", exc)
        if strict:
            raise
        return {
            "ok": True,
            "synced": False,
            "error": f"Failed to fetch NetBox devices: {exc}",
            "namespace_id": str(ns_uuid),
            "assets_scanned": len(asset_rows),
            "devices_fetched": 0,
            "matched_count": 0,
            "edges_written": 0,
            "matches": [],
        }

    # Index NetBox devices for matching
    devices_by_serial: dict[str, dict[str, Any]] = {}
    devices_by_asset_cf: dict[str, dict[str, Any]] = {}
    devices_by_bom_cf: dict[str, dict[str, Any]] = {}

    for d in devices:
        ser = str(d.get("serial") or "").strip().lower()
        if ser:
            devices_by_serial[ser] = d

        cfs = d.get("custom_fields") or {}
        if isinstance(cfs, dict):
            cf_aid = str(cfs.get("asset_id") or "").strip().lower()
            if cf_aid:
                devices_by_asset_cf[cf_aid] = d
            cf_bom = str(cfs.get("bom_line_id") or "").strip().lower()
            if cf_bom:
                devices_by_bom_cf[cf_bom] = d

    matches: list[dict[str, Any]] = []
    edges_to_write: list[tuple[str, str, str, float]] = []

    matched_device_ids: set[int] = set()

    for row in asset_rows:
        asset_id = str(row["id"])
        serial = str(row["serial"] or "").strip()
        bom_line_id = str(row["bom_line_id"] or "").strip()
        func_loc = str(row["functional_location_id"] or "").strip()

        matched_dev: dict[str, Any] | None = None
        match_method = ""
        match_confidence = 0.0

        # Stage 1: Exact serial match
        if serial and serial.lower() in devices_by_serial:
            dev = devices_by_serial[serial.lower()]
            if dev.get("id") not in matched_device_ids:
                matched_dev = dev
                match_method = "exact_serial"
                match_confidence = 1.0

        # Stage 2: Custom fields match
        if not matched_dev and asset_id.lower() in devices_by_asset_cf:
            dev = devices_by_asset_cf[asset_id.lower()]
            if dev.get("id") not in matched_device_ids:
                matched_dev = dev
                match_method = "custom_field_asset_id"
                match_confidence = 1.0

        if not matched_dev and bom_line_id and bom_line_id.lower() in devices_by_bom_cf:
            dev = devices_by_bom_cf[bom_line_id.lower()]
            if dev.get("id") not in matched_device_ids:
                matched_dev = dev
                match_method = "custom_field_bom_line"
                match_confidence = 1.0

        # Stage 3: Location match
        if not matched_dev and func_loc:
            loc_norm = _normalize(func_loc)
            for d in devices:
                if d.get("id") in matched_device_ids:
                    continue
                site_name = _normalize((d.get("site") or {}).get("name"))
                loc_name = _normalize((d.get("location") or {}).get("name"))
                if loc_norm in (site_name, loc_name) and (serial or bom_line_id):
                    # Check if device name also relates to asset
                    d_name = _normalize(d.get("name"))
                    if (serial and _normalize(serial) in d_name) or (
                        bom_line_id and _normalize(bom_line_id) in d_name
                    ):
                        matched_dev = d
                        match_method = "location_and_name"
                        match_confidence = 0.95
                        break

        # Stage 4: Fuzzy name match
        if not matched_dev:
            best_score = 0.0
            best_dev: dict[str, Any] | None = None
            query_str = serial or bom_line_id
            if query_str:
                for d in devices:
                    if d.get("id") in matched_device_ids:
                        continue
                    d_name = str(d.get("name") or "")
                    score = _fuzzy_ratio(query_str, d_name)
                    if score > best_score and score >= threshold:
                        best_score = score
                        best_dev = d
            if best_dev:
                matched_dev = best_dev
                match_method = "fuzzy_name"
                match_confidence = round(best_score, 3)

        if matched_dev:
            dev_id = int(matched_dev["id"])
            matched_device_ids.add(dev_id)

            asset_label = f"ASSET:{asset_id}"
            device_label = f"NetBoxDevice:{dev_id}"

            # Bidirectional graph edges
            edges_to_write.append((asset_label, _PRED_MAPS_TO, device_label, match_confidence))
            edges_to_write.append(
                (device_label, _PRED_MAPPED_TO_ASSET, asset_label, match_confidence)
            )

            matches.append(
                {
                    "asset_id": asset_id,
                    "netbox_device_id": dev_id,
                    "netbox_device_name": matched_dev.get("name"),
                    "match_method": match_method,
                    "match_confidence": match_confidence,
                }
            )

    # Batch write edges to kg_edges
    edges_written = 0
    if edges_to_write:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            for s_label, pred, o_label, conf in edges_to_write:
                await conn.execute(
                    """
                    INSERT INTO kg_edges (subject_label, predicate, object_label, confidence,
                                          namespace_id, change_origin)
                    VALUES ($1, $2, $3, $4, $5::uuid, $6)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                        SET confidence = EXCLUDED.confidence,
                            change_origin = EXCLUDED.change_origin,
                            updated_at = NOW()
                    """,
                    s_label,
                    pred,
                    o_label,
                    conf,
                    str(ns_uuid),
                    _CHANGE_ORIGIN,
                )
                edges_written += 1

    return {
        "ok": True,
        "synced": True,
        "namespace_id": str(ns_uuid),
        "assets_scanned": len(asset_rows),
        "devices_fetched": len(devices),
        "matched_count": len(matches),
        "edges_written": edges_written,
        "matches": matches,
    }
