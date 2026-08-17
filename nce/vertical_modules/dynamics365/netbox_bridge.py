"""
nce/vertical_modules/dynamics365/netbox_bridge.py
==================================================
Cross-system mapping bridge: Dynamics 365 ↔ NetBox SOT.

Resolves the identity connection between the CRM layer (D365) and the network
source-of-truth (NetBox) by matching:

  D365 Accounts        ↔  NetBox Tenants
  D365 Functional Locations ↔  NetBox Sites / Locations

Matching cascade (in priority order)
--------------------------------------
1. **Custom field** — NetBox tenant has ``custom_fields[NCE_D365_NETBOX_TENANT_CF_NAME]``
   equal to the D365 account GUID.  Confidence = 1.0.
2. **Exact name** — names match case-insensitively after stripping whitespace.
   Confidence = 1.0.
3. **Slug** — ``_slugify(d365_name) == netbox_slug``.  Confidence = 0.95.
4. **Fuzzy** — ``difflib.SequenceMatcher`` ratio ≥ ``NCE_D365_NETBOX_FUZZY_THRESHOLD``.
   Confidence = ratio value.

All confirmed (method != 'fuzzy') and unconfirmed (fuzzy) matches are written
to ``d365_netbox_mappings`` and simultaneously materialised as kg_edges:

  Account:{d365_name}           → MAPS_TO_TENANT         → Tenant:{nb_name}
  Tenant:{nb_name}              → CRM_ACCOUNT             → Account:{d365_name}
  FunctionalLocation:{d365_name}→ MAPS_TO_SITE            → Site:{nb_name}
  Site:{nb_name}                → PHYSICAL_HOST_OF        → FunctionalLocation:{d365_name}

The ``confirmed`` flag on a ``d365_netbox_mappings`` row is set via the admin
UI (or manually in the DB) and is never overwritten by the bridge sync —
once confirmed, the row's match_method and confidence become read-only.
"""

from __future__ import annotations

import difflib
import logging
import re
import uuid
from typing import Any

import asyncpg
import httpx

from nce.config import cfg
from nce.vertical_modules.dynamics365.client import DataverseClient

log = logging.getLogger("nce.vertical_modules.dynamics365.netbox_bridge")


# ---------------------------------------------------------------------------
# Thin NetBox REST client  (only the endpoints this bridge needs)
# ---------------------------------------------------------------------------


class NetBoxBridgeClient:
    """
    Minimal async REST client for NetBox Tenancy and DCIM endpoints.

    Parameters
    ----------
    base_url:
        NetBox root URL, e.g. ``https://netbox.example.com``.
    token:
        NetBox API token (``Token <token>`` auth scheme).
    page_size:
        ``limit`` parameter per REST page.  Defaults to 1000.
    """

    _HEADERS = {"Accept": "application/json"}

    def __init__(
        self,
        base_url: str,
        token: str,
        page_size: int = 1000,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = {"Authorization": f"Token {token}"}
        self._page_size = page_size

    async def fetch_tenants(self) -> list[dict[str, Any]]:
        """Return all NetBox tenants as a flat list."""
        return await self._paginate(f"{self._base}/api/tenancy/tenants/")

    async def fetch_sites(self) -> list[dict[str, Any]]:
        """Return all NetBox sites."""
        return await self._paginate(f"{self._base}/api/dcim/sites/")

    async def fetch_locations(self) -> list[dict[str, Any]]:
        """Return all NetBox rack/location hierarchy nodes."""
        return await self._paginate(f"{self._base}/api/dcim/locations/")

    async def _paginate(self, url: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_url: str | None = f"{url}?limit={self._page_size}&offset=0"
        headers = {**self._HEADERS, **self._auth}

        from nce.http_resilience import request_with_retry

        async with httpx.AsyncClient(timeout=30.0) as client:
            while next_url:
                resp = await request_with_retry(
                    client,
                    "GET",
                    next_url,
                    headers=headers,
                    operation_name="netbox:paginate",
                )
                resp.raise_for_status()
                body = resp.json()
                results.extend(body.get("results") or [])
                next_url = body.get("next")  # None when last page

        return results


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a display name to a NetBox-compatible slug."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    return name.strip("-")


def _normalize(name: str) -> str:
    """Lower-case, strip, collapse internal whitespace."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _fuzzy_ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio between two normalised strings (0.0–1.0)."""
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _best_fuzzy(
    query: str,
    candidates: list[dict[str, Any]],
    name_key: str,
    threshold: float,
) -> tuple[dict[str, Any], float] | None:
    """
    Find the candidate with the highest fuzzy ratio ≥ threshold.

    Returns ``(candidate_dict, ratio)`` or *None* if nothing clears the bar.
    """
    best: tuple[dict[str, Any], float] | None = None
    qn = _normalize(query)
    for cand in candidates:
        ratio = difflib.SequenceMatcher(None, qn, _normalize(cand.get(name_key, ""))).ratio()
        if ratio >= threshold:
            if best is None or ratio > best[1]:
                best = (cand, ratio)
    return best


# ---------------------------------------------------------------------------
# Bridge engine
# ---------------------------------------------------------------------------


class D365NetBoxBridge:
    """
    Orchestrates the D365 ↔ NetBox cross-reference mapping.

    Parameters
    ----------
    conn:
        ``asyncpg.Connection`` — must already be scoped to the correct namespace
        (RLS set via ``set_namespace_context``).
    namespace_id:
        Tenant namespace UUID.
    d365_client:
        Authenticated ``DataverseClient`` instance.
    netbox_client:
        ``NetBoxBridgeClient`` instance pointed at the org's NetBox.
    fuzzy_threshold:
        Minimum SequenceMatcher ratio to accept a fuzzy match.  Defaults to
        ``cfg.NCE_D365_NETBOX_FUZZY_THRESHOLD``.
    tenant_cf_name:
        Name of the NetBox custom field that stores the D365 account GUID.
    """

    _D365_ACCOUNT_FIELDS = [
        "accountid",
        "name",
    ]
    _D365_LOCATION_FIELDS = [
        "msdyn_functionallocationid",
        "msdyn_name",
    ]

    def __init__(
        self,
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        d365_client: DataverseClient,
        netbox_client: NetBoxBridgeClient,
        fuzzy_threshold: float | None = None,
        tenant_cf_name: str | None = None,
    ) -> None:
        self._conn = conn
        self._ns = namespace_id
        self._d365 = d365_client
        self._nb = netbox_client
        self._threshold = (
            fuzzy_threshold if fuzzy_threshold is not None else cfg.NCE_D365_NETBOX_FUZZY_THRESHOLD
        )
        self._tenant_cf = tenant_cf_name or cfg.NCE_D365_NETBOX_TENANT_CF_NAME

    # ------------------------------------------------------------------
    # Public sync methods
    # ------------------------------------------------------------------

    async def sync_account_tenant_mappings(self) -> dict[str, Any]:
        """
        Match D365 Accounts to NetBox Tenants and persist mapping + kg_edges.

        Returns stats dict with counts for each match method and total edges written.
        """
        log.info("[D365-NB-BRIDGE] Fetching accounts + tenants for ns=%s", self._ns)
        accounts = await self._fetch_d365_accounts()
        tenants = await self._nb.fetch_tenants()
        log.info("[D365-NB-BRIDGE] %d accounts, %d tenants", len(accounts), len(tenants))

        stats: dict[str, int] = {
            "custom_field": 0,
            "exact": 0,
            "slug": 0,
            "fuzzy": 0,
            "unmatched": 0,
        }
        edges: list[tuple[str, str, str, float]] = []

        # Build lookup structures for fast matching
        tenant_by_norm: dict[str, dict] = {_normalize(t["name"]): t for t in tenants}
        tenant_by_slug: dict[str, dict] = {t.get("slug", ""): t for t in tenants}

        for account in accounts:
            acc_id = account.get("accountid", "")
            acc_name = account.get("name", "").strip()
            if not acc_name:
                continue

            match: dict[str, Any] | None = None
            method = ""
            confidence = 0.0

            # Priority 1: custom field match
            for t in tenants:
                cf_val = (t.get("custom_fields") or {}).get(self._tenant_cf, "")
                if cf_val and str(cf_val).strip().lower() == acc_id.lower():
                    match, method, confidence = t, "custom_field", 1.0
                    break

            # Priority 2: exact name
            if not match:
                norm = _normalize(acc_name)
                if norm in tenant_by_norm:
                    match, method, confidence = tenant_by_norm[norm], "exact", 1.0

            # Priority 3: slug
            if not match:
                slug = _slugify(acc_name)
                if slug in tenant_by_slug:
                    match, method, confidence = tenant_by_slug[slug], "slug", 0.95

            # Priority 4: fuzzy
            if not match:
                result = _best_fuzzy(acc_name, tenants, "name", self._threshold)
                if result:
                    match, confidence = result
                    method = "fuzzy"

            if not match:
                stats["unmatched"] += 1
                continue

            stats[method] += 1
            nb_name = match["name"]

            await self._upsert_mapping(
                d365_entity_type="account",
                d365_entity_id=acc_id,
                d365_entity_name=acc_name,
                nb_entity_type="tenant",
                nb_entity_id=match["id"],
                nb_entity_name=nb_name,
                nb_entity_slug=match.get("slug", ""),
                match_method=method,
                match_confidence=confidence,
            )
            edges.append((f"Account:{acc_name}", "MAPS_TO_TENANT", f"Tenant:{nb_name}", confidence))
            edges.append((f"Tenant:{nb_name}", "CRM_ACCOUNT", f"Account:{acc_name}", confidence))

        written = await self._upsert_kg_edges_batch(edges)
        stats["edges_written"] = written
        log.info("[D365-NB-BRIDGE] account-tenant stats: %s", stats)
        return {"entity_pair": "account_tenant", **stats}

    async def sync_location_site_mappings(self) -> dict[str, Any]:
        """
        Match D365 Functional Locations to NetBox Sites (and sub-Locations).

        Tries Sites first; falls back to Locations for more granular entries.

        Returns stats dict.
        """
        log.info("[D365-NB-BRIDGE] Fetching functional_locations + sites for ns=%s", self._ns)
        d365_locs = await self._fetch_d365_functional_locations()
        nb_sites = await self._nb.fetch_sites()
        nb_locs = await self._nb.fetch_locations()
        log.info(
            "[D365-NB-BRIDGE] %d d365-locs, %d nb-sites, %d nb-locs",
            len(d365_locs),
            len(nb_sites),
            len(nb_locs),
        )

        # Combine sites + locations for matching; prefer sites
        all_nb = [{"_nb_type": "site", **s} for s in nb_sites] + [
            {"_nb_type": "location", **loc} for loc in nb_locs
        ]

        site_by_norm: dict[str, dict] = {_normalize(s["name"]): s for s in nb_sites}
        site_by_slug: dict[str, dict] = {s.get("slug", ""): s for s in nb_sites}
        loc_by_norm: dict[str, dict] = {_normalize(loc["name"]): loc for loc in nb_locs}
        loc_by_slug: dict[str, dict] = {loc.get("slug", ""): loc for loc in nb_locs}

        stats: dict[str, int] = {"exact": 0, "slug": 0, "fuzzy": 0, "unmatched": 0}
        edges: list[tuple[str, str, str, float]] = []

        for d365_loc in d365_locs:
            loc_id = d365_loc.get("msdyn_functionallocationid", "")
            loc_name = d365_loc.get("msdyn_name", "").strip()
            if not loc_name:
                continue

            match: dict[str, Any] | None = None
            method = ""
            confidence = 0.0
            nb_type = "site"

            norm = _normalize(loc_name)
            slug = _slugify(loc_name)

            # Exact — prefer sites
            if norm in site_by_norm:
                match, method, confidence, nb_type = site_by_norm[norm], "exact", 1.0, "site"
            elif norm in loc_by_norm:
                match, method, confidence, nb_type = loc_by_norm[norm], "exact", 1.0, "location"
            # Slug — prefer sites
            elif slug in site_by_slug:
                match, method, confidence, nb_type = site_by_slug[slug], "slug", 0.95, "site"
            elif slug in loc_by_slug:
                match, method, confidence, nb_type = loc_by_slug[slug], "slug", 0.95, "location"
            else:
                # Fuzzy — try all_nb (sites first because of list ordering)
                result = _best_fuzzy(loc_name, all_nb, "name", self._threshold)
                if result:
                    best_nb, confidence = result
                    nb_type = best_nb.get("_nb_type", "site")
                    match = best_nb
                    method = "fuzzy"

            if not match:
                stats["unmatched"] += 1
                continue

            stats[method] += 1
            nb_name = match["name"]
            predicate = "MAPS_TO_SITE" if nb_type == "site" else "MAPS_TO_LOCATION"
            inverse = "PHYSICAL_HOST_OF"

            await self._upsert_mapping(
                d365_entity_type="functional_location",
                d365_entity_id=loc_id,
                d365_entity_name=loc_name,
                nb_entity_type=nb_type,
                nb_entity_id=match["id"],
                nb_entity_name=nb_name,
                nb_entity_slug=match.get("slug", ""),
                match_method=method,
                match_confidence=confidence,
            )
            nb_label_prefix = "Site" if nb_type == "site" else "Location"
            edges.append(
                (
                    f"FunctionalLocation:{loc_name}",
                    predicate,
                    f"{nb_label_prefix}:{nb_name}",
                    confidence,
                )
            )
            edges.append(
                (
                    f"{nb_label_prefix}:{nb_name}",
                    inverse,
                    f"FunctionalLocation:{loc_name}",
                    confidence,
                )
            )

        written = await self._upsert_kg_edges_batch(edges)
        stats["edges_written"] = written
        log.info("[D365-NB-BRIDGE] location-site stats: %s", stats)
        return {"entity_pair": "functional_location_site", **stats}

    async def run_full_bridge_sync(self) -> dict[str, Any]:
        """Run both mapping passes and return aggregated stats."""
        account_stats = await self.sync_account_tenant_mappings()
        location_stats = await self.sync_location_site_mappings()
        total_edges = account_stats.get("edges_written", 0) + location_stats.get("edges_written", 0)
        return {
            "namespace_id": str(self._ns),
            "account_tenant": account_stats,
            "location_site": location_stats,
            "total_edges": total_edges,
        }

    # ------------------------------------------------------------------
    # D365 data fetchers (minimal fields only)
    # ------------------------------------------------------------------

    async def _fetch_d365_accounts(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async for rec in self._d365.paginate(
            "accounts",
            select=self._D365_ACCOUNT_FIELDS,
            page_size=cfg.NCE_D365_SYNC_PAGE_SIZE,
        ):
            results.append(rec)
        return results

    async def _fetch_d365_functional_locations(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async for rec in self._d365.paginate(
            "msdyn_functionallocations",
            select=self._D365_LOCATION_FIELDS,
            page_size=cfg.NCE_D365_SYNC_PAGE_SIZE,
        ):
            results.append(rec)
        return results

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _upsert_mapping(
        self,
        *,
        d365_entity_type: str,
        d365_entity_id: str,
        d365_entity_name: str,
        nb_entity_type: str,
        nb_entity_id: int,
        nb_entity_name: str,
        nb_entity_slug: str,
        match_method: str,
        match_confidence: float,
    ) -> None:
        """
        Upsert a row in ``d365_netbox_mappings``.

        Rows already marked ``confirmed = TRUE`` are skipped entirely so that
        human-curated mappings are never overwritten by the automated sync.
        """
        await self._conn.execute(
            """
            INSERT INTO d365_netbox_mappings (
                namespace_id,
                d365_entity_type, d365_entity_id, d365_entity_name,
                nb_entity_type,   nb_entity_id,   nb_entity_name, nb_entity_slug,
                match_method, match_confidence
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (namespace_id, d365_entity_type, d365_entity_id,
                         nb_entity_type, nb_entity_id)
            DO UPDATE SET
                d365_entity_name  = EXCLUDED.d365_entity_name,
                nb_entity_name    = EXCLUDED.nb_entity_name,
                nb_entity_slug    = EXCLUDED.nb_entity_slug,
                match_method      = CASE WHEN d365_netbox_mappings.confirmed THEN
                                        d365_netbox_mappings.match_method
                                    ELSE EXCLUDED.match_method END,
                match_confidence  = CASE WHEN d365_netbox_mappings.confirmed THEN
                                        d365_netbox_mappings.match_confidence
                                    ELSE EXCLUDED.match_confidence END,
                updated_at        = NOW()
            """,
            str(self._ns),
            d365_entity_type,
            d365_entity_id,
            d365_entity_name,
            nb_entity_type,
            nb_entity_id,
            nb_entity_name,
            nb_entity_slug,
            match_method,
            match_confidence,
        )

    async def _upsert_kg_edges_batch(
        self,
        edges: list[tuple[str, str, str, float]],
    ) -> int:
        """Batch-upsert kg_edges using UNNEST (same pattern as sync.py)."""
        if not edges:
            return 0

        result = await self._conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id,
                                  change_origin)
            SELECT unnest($1::text[]),
                   unnest($2::text[]),
                   unnest($3::text[]),
                   unnest($4::float[]),
                   $5::uuid,
                   'webhook'
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    change_origin = CASE
                        WHEN kg_edges.change_origin IN ('sync', 'webhook') THEN kg_edges.change_origin
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            [e[0] for e in edges],
            [e[1] for e in edges],
            [e[2] for e in edges],
            [e[3] for e in edges],
            str(self._ns),
        )
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return len(edges)
