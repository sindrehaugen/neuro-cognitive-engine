"""
nce/vertical_modules/sites/address_registry.py
======================================
Address registry feed for C17 Site Master Data (MLV16 charter, Lane F,
Wave F-9): on-demand address validation/geocoding against Kartverket's
public Adresse REST API (Geonorge, against the cadastre) — public, free,
no authentication.

Read for shape only (Q-25): the host's own Kartverket address client.
This module's schema, functions and tests are its own, not a port of the
host's.

What this wave does NOT do, stated rather than discovered
--------------------------------------------------------------
The host's own client solves a much harder problem than this wave needs:
matching a MESSY, freetext D365 location name (abbreviated street types,
house-number ranges, parenthetical building nicknames, transposed
letters) against the cadastre, because that name was typed once, years
ago, into a CRM field with no validation. A `sites` row's own `address`
fields are supplied by the caller at write time — there is no messy
legacy freetext to reverse-engineer here, so none of the host's fuzzy
matching, abbreviation-expansion, or house-number-range collapsing is
re-implemented. This module does the plain version of the same lookup:
one query string in, the best structured hit back (or none).

No new migration, unlike this lane's earlier geodata waves
--------------------------------------------------------------
F-9 writes into `sites`' own `address`/`latitude`/`longitude` columns
(migration 095, Wave A-9) rather than a store of its own — the same
shape as F-8's BRREG feed writing into `legal_entities.metadata`, not
the shape of F-11/F-12/F-13's own GLOBAL tables.

Q-25 (AGPL / proprietary boundary)
--------------------------------------
Kartverket's Adresse API (matrikkelen) is public sector information
(NLOD-licensed); nothing about it is the host's proprietary work. What
was read from the host for shape only was the ARCHITECTURAL DECISION
(on-demand lookup, never a bulk mirror; degrade-never-raise; a
deterministic dedupe key per hit) — re-implemented here independently,
never the host's regex-heavy freetext-matching code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.sites.service import get_site, update_site

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.sites.address_registry")

_BASE_PATH = "/adresser/v1/sok"
_BASE_URL = f"https://ws.geonorge.no{_BASE_PATH}"
_TIMEOUT = 8.0

# The allow-list literal (charter §0 rule 3): one GET, one exact path. No
# credentials exist for this gate to protect (Geonorge's search is
# public), so its job is narrower and just as real: it is what stops this
# module from silently growing into a different Geonorge endpoint without
# a code change AND a new test.
_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({("GET", _BASE_PATH)})


class AddressRegistryAllowListRefusal(PermissionError):
    """A (method, path) pair fell outside this feed's allow-list.

    Raised BEFORE any HTTP call.
    """

    def __init__(self, method: str, path: str) -> None:
        super().__init__(
            f"address registry feed refused {method} {path}: not in its read allow-list"
        )
        self.method = method
        self.path = path


async def _gated_request(client: httpx.AsyncClient, method: str, query: str) -> httpx.Response:
    if (method, _BASE_PATH) not in _ALLOWED_READS:
        raise AddressRegistryAllowListRefusal(method, _BASE_PATH)
    return await client.request(
        method, _BASE_URL, params={"sok": query, "treffPerSide": 1, "fuzzy": "true"}
    )


def _query_from_site_address(address: dict[str, Any]) -> str:
    """Build a search string from a site's own ``address`` fields.

    Prefers an already-formatted address (``formatted_address``, matching
    ``SiteAddress``'s own field); falls back to assembling
    street/postal_code/city, since a caller may have filled those without
    ever setting the formatted string.
    """
    formatted = str(address.get("formatted_address") or "").strip()
    if formatted:
        return formatted
    parts = [
        str(address.get("street") or "").strip(),
        " ".join(
            p
            for p in (
                str(address.get("postal_code") or "").strip(),
                str(address.get("city") or "").strip(),
            )
            if p
        ),
    ]
    return ", ".join(p for p in parts if p)


def address_key(hit: dict[str, Any]) -> str:
    """Deterministic id for one Geonorge hit — a road address is
    identified by (municipality, address code, house number, letter); a
    cadastre-only address (no address code) falls back to
    (municipality, cadastral unit, cadastral sub-unit). Same physical
    address always yields the same key, however many times it is looked
    up."""
    municipality = str(hit.get("kommunenummer") or "").strip()
    address_code = str(hit.get("adressekode") or "").strip()
    number = str(hit.get("nummer") or "").strip()
    letter = str(hit.get("bokstav") or "").strip()
    if address_code:
        return "-".join(p for p in (municipality, address_code, number, letter) if p)
    plot = str(hit.get("gardsnummer") or "").strip()
    sub_plot = str(hit.get("bruksnummer") or "").strip()
    return "-".join(
        p for p in (municipality, f"g{plot}", f"b{sub_plot}") if p not in ("", "g", "b")
    )


def structure_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """One Geonorge hit -> this module's structured address object."""
    point = hit.get("representasjonspunkt") or {}
    return {
        "address_key": address_key(hit),
        "formatted_address": hit.get("adressetekst") or "",
        "postal_code": hit.get("postnummer") or "",
        "city": hit.get("poststed") or "",
        "municipality": hit.get("kommunenavn") or "",
        "municipality_number": hit.get("kommunenummer") or "",
        "lat": point.get("lat"),
        "lon": point.get("lon"),
    }


async def lookup_address(
    query: str,
    *,
    client: httpx.AsyncClient | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any] | None:
    """Look up one freetext address against Kartverket. Returns the
    structured, validated address object for the best hit, or ``None``
    (empty query/no hits/network error — never fabricated). ``transport``
    is a test-only escape hatch (mirrors ``brreg_feed``'s ``transport=``
    kwarg)."""
    q = (query or "").strip()
    if not q:
        return None
    owns = client is None
    cl = client or httpx.AsyncClient(timeout=_TIMEOUT, transport=transport)
    try:
        resp = await _gated_request(cl, "GET", q)
        resp.raise_for_status()
        data = resp.json()
    except AddressRegistryAllowListRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into the caller
        log.warning("address_registry: lookup failed for %r: %s", q, str(exc)[:200])
        return None
    finally:
        if owns:
            await cl.aclose()
    hits = data.get("adresser") or []
    return structure_hit(hits[0]) if hits else None


async def _require_site(engine: NCEEngine, ns_uuid: UUID, site_id: UUID) -> dict[str, Any]:
    """Refuse a site that is not visible (or archived) in the caller's
    namespace. Explicit predicate, not left to RLS alone — same reasoning
    as ``assets.telemetry._require_asset_in_namespace`` /
    ``brreg_feed._require_legal_entity_org_nr``."""
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        site = await get_site(conn, ns_uuid, site_id)
    if site is None:
        raise ValueError(
            f"do_enrich_site_from_address_registry: site {site_id} is not an active site "
            f"in namespace {ns_uuid}"
        )
    return site.model_dump()


async def do_enrich_site_from_address_registry(
    engine: NCEEngine, params: dict[str, Any], *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Validate/geocode a site's own address against Kartverket's cadastre
    and merge the structured result into ``sites.address``/``latitude``/
    ``longitude``.

    Parameters
    ----------
    params:
        ``{"namespace_id": str | UUID, "site_id": str | UUID, "query":
        str | None}`` — ``namespace_id``/``site_id`` required. ``query``
        is optional freetext to look up instead of the site's own stored
        address (useful when registering a brand-new site whose address
        has never been validated).

    Fail-soft on a genuine "not found in the registry" (mirrors
    ``brreg_feed``: a 0-hit search means the address simply isn't
    resolvable there, not an error) — the caller gets ``{"matched":
    False, "reason": ...}`` and the site's row is left untouched. Refuses
    (raises) before any HTTP call for a namespace mismatch or a query
    that resolves to nothing to search on; those are internal data
    problems, not registry-lookup outcomes.

    ``transport`` is a test-only escape hatch: ``None`` in production
    uses httpx's real transport; tests inject an ``httpx.MockTransport``
    so no test reaches the network.

    Returns
    -------
    dict
        ``{"ok": True, "site_id": str, "matched": bool, "reason": str |
        None, "address": dict | None}``.
    """
    ns_uuid = UUID(str(params.get("namespace_id") or "")) if params.get("namespace_id") else None
    site_id = UUID(str(params.get("site_id") or "")) if params.get("site_id") else None
    if ns_uuid is None:
        raise ValueError("do_enrich_site_from_address_registry: 'namespace_id' is required")
    if site_id is None:
        raise ValueError("do_enrich_site_from_address_registry: 'site_id' is required")

    site = await _require_site(engine, ns_uuid, site_id)
    query = str(params.get("query") or "").strip() or _query_from_site_address(
        site.get("address") or {}
    )
    if not query:
        raise ValueError(
            f"do_enrich_site_from_address_registry: site {site_id} has no address text to "
            "look up and no 'query' override was supplied"
        )

    hit = await lookup_address(query, transport=transport)
    if hit is None:
        return {
            "ok": True,
            "site_id": str(site_id),
            "matched": False,
            "reason": "not_found_in_registry",
            "address": None,
        }

    merged_address = {
        **(site.get("address") or {}),
        "street": hit["formatted_address"].split(",")[0].strip() or None,
        "postal_code": hit["postal_code"] or None,
        "city": hit["city"] or None,
        "country": (site.get("address") or {}).get("country") or "NO",
        "formatted_address": hit["formatted_address"] or None,
        "is_validated": True,
    }
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await update_site(
            conn,
            ns_uuid,
            site_id,
            {"address": merged_address, "latitude": hit["lat"], "longitude": hit["lon"]},
        )

    log.info(
        "do_enrich_site_from_address_registry site=%s address_key=%s",
        site_id,
        hit["address_key"],
    )
    return {
        "ok": True,
        "site_id": str(site_id),
        "matched": True,
        "reason": None,
        "address": hit,
    }
