"""
nce/vertical_modules/system_design/netbox_bridge.py
====================================================
Phase 1b — Functional-location sync + ``promoted_to_asbuilt`` reconciliation.

System Design **authors** design-intent ``FUNCTIONAL_LOCATION`` nodes
(``SITE > BUILDING > FLOOR > ROOM > POSITION``).  The install/NetBox layer
**promotes** them to as-built.  This bridge does three things:

1. **Push** — creates the functional-location hierarchy as NetBox sites and
   locations from authored design intent so installers can reference them.
2. **Reconcile** — when NetBox/Assets confirm a room exists post-install, writes
   a ``promoted_to_asbuilt`` edge from the design-intent
   ``FUNCTIONAL_LOCATION`` node to its as-built counterpart in the graph.
3. **Diverge** — if the as-built record differs from the design intent (renamed
   location, missing room, extra position), writes a ``has_divergence`` edge
   from the intent node to the as-built node and logs the delta.

Direction invariant (Correction #2):
  Design intent → install promotion → as-built confirmation.
  The design-intent node is **never overwritten** — it is linked via edge.

Phase 1b is NOT a Phase-1a gate: the core system-design engine works without
NetBox connectivity.  If NetBox is unreachable, ``sync_fl_to_netbox`` raises
``ExternalAPIError`` and the caller must decide whether to abort or skip.  The
``promoted_to_asbuilt`` reconciliation path is fully independent.

Modelled on ``nce/vertical_modules/dynamics365/netbox_bridge.py`` (mapping
pattern: paginated REST fetch → edge batch upsert).

HTTP:
    All outbound calls route through ``nce.http_resilience.request_with_retry``.

Secrets:
    NetBox token is **environment-only** — read from ``cfg.NCE_NETBOX_TOKEN``.
    Never log or hard-code it.

RLS:
    All DB writes use an ``asyncpg.Connection`` whose namespace GUC is already
    set by the caller (via ``scoped_pg_session`` or equivalent).  The module
    never sets or clears the GUC itself.

Edges written:
    ``FL:<ns>:<path>``        -[sync_to_netbox]->        ``NetBoxSite:<nb_id>``
    ``FL:<ns>:<path>``        -[sync_to_netbox]->        ``NetBoxLocation:<nb_id>``
    ``FL:<ns>:<path>``        -[promoted_to_asbuilt]->   ``AsBuilt:FL:<ns>:<path>``
    ``FL:<ns>:<path>``        -[has_divergence]->        ``AsBuilt:FL:<ns>:<path>``
    ``AsBuilt:FL:<ns>:<path>``-[as_built_confirms]->     ``FL:<ns>:<path>``

``confidence`` on edges only (wave rule 7).  ``kg_nodes`` has no confidence
column and no metadata/payload/state column.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import httpx

from nce.config import cfg

log = logging.getLogger("nce.vertical_modules.system_design.netbox_bridge")

# ---------------------------------------------------------------------------
# NetBox hierarchy levels mapped from functional-location levels
#
#   SITE       → dcim/sites/
#   BUILDING   → dcim/locations/  (parent = site)
#   FLOOR      → dcim/locations/  (parent = building-location)
#   ROOM       → dcim/locations/  (parent = floor-location)
#   POSITION   → dcim/locations/  (parent = room-location)
# ---------------------------------------------------------------------------

_NB_SITES_PATH = "/api/dcim/sites/"
_NB_LOCATIONS_PATH = "/api/dcim/locations/"

# Edge predicates
_PRED_SYNC_TO_NB: str = "sync_to_netbox"
_PRED_PROMOTED: str = "promoted_to_asbuilt"
_PRED_DIVERGENCE: str = "has_divergence"
_PRED_CONFIRMS: str = "as_built_confirms"

# Confidence values
_CONF_STRUCTURAL: float = 1.0
_CONF_DIVERGED: float = 0.7


# ---------------------------------------------------------------------------
# Small helpers — name normalisation and slugs
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a display name to a NetBox-compatible slug (≤ 50 chars)."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    slug = name.strip("-")
    return slug[:50]


def _normalize(name: str) -> str:
    """Lower-case, strip, collapse internal whitespace."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


# ---------------------------------------------------------------------------
# FL label helpers — must match graph.py conventions exactly
# ---------------------------------------------------------------------------


def _fl_label(namespace_slug: str, *path_parts: str) -> str:
    """Deterministic FL label: ``FL:<NS_SLUG>:<PART1>:<PART2>:...``."""
    parts = ":".join(p.upper() for p in path_parts)
    return f"FL:{namespace_slug.upper()}:{parts}"


def _asbuilt_label(intent_label: str) -> str:
    """As-built counterpart label for a design-intent FL label."""
    return f"AsBuilt:{intent_label}"


# ---------------------------------------------------------------------------
# NetBox REST client (minimal — only DCIM sites + locations)
# ---------------------------------------------------------------------------


class _NetBoxClient:
    """Minimal async REST client for DCIM sites and locations.

    Fetches existing objects and creates new ones via the REST API.
    All HTTP routes through ``request_with_retry`` (exponential back-off +
    full jitter).  Token is never logged.
    """

    _HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

    def __init__(self, base_url: str, token: str, page_size: int = 1000) -> None:
        self._base = base_url.rstrip("/")
        self._auth = f"Token {token}"
        self._page_size = page_size

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    async def fetch_sites(self) -> list[dict[str, Any]]:
        """Return all NetBox DCIM sites as a flat list."""
        return await self._paginate(_NB_SITES_PATH)

    async def fetch_locations(self) -> list[dict[str, Any]]:
        """Return all NetBox DCIM locations as a flat list."""
        return await self._paginate(_NB_LOCATIONS_PATH)

    async def _paginate(self, path: str) -> list[dict[str, Any]]:
        from nce.http_resilience import request_with_retry

        results: list[dict[str, Any]] = []
        next_url: str | None = f"{self._base}{path}?limit={self._page_size}&offset=0"
        headers = {**self._HEADERS, "Authorization": self._auth}

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
                body: dict[str, Any] = resp.json()
                results.extend(body.get("results") or [])
                next_url = body.get("next")

        return results

    # ------------------------------------------------------------------
    # Create helpers
    # ------------------------------------------------------------------

    async def create_site(self, name: str, slug: str) -> dict[str, Any]:
        """Create a NetBox site and return the created object."""
        return await self._post(_NB_SITES_PATH, {"name": name, "slug": slug, "status": "active"})

    async def create_location(
        self,
        name: str,
        slug: str,
        site_id: int,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a NetBox location under *site_id*, optionally nested under *parent_id*."""
        payload: dict[str, Any] = {
            "name": name,
            "slug": slug,
            "site": site_id,
            "status": "active",
        }
        if parent_id is not None:
            payload["parent"] = parent_id
        return await self._post(_NB_LOCATIONS_PATH, payload)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        from nce.http_resilience import request_with_retry

        url = f"{self._base}{path}"
        headers = {**self._HEADERS, "Authorization": self._auth}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await request_with_retry(
                client,
                "POST",
                url,
                headers=headers,
                json=payload,
                operation_name="netbox:create",
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ReconcileStatus(str, Enum):
    """Outcome of a single node reconciliation."""

    PROMOTED = "promoted"
    DIVERGED = "diverged"
    UNCHANGED = "unchanged"


@dataclass
class FLNode:
    """Lightweight representation of a design-intent FL node."""

    label: str
    name: str
    level: str  # SITE | BUILDING | FLOOR | ROOM | POSITION


@dataclass
class SyncResult:
    """Aggregated result of ``sync_fl_to_netbox``."""

    sites_created: int = 0
    locations_created: int = 0
    sites_reused: int = 0
    locations_reused: int = 0
    edges_written: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ReconcileResult:
    """Aggregated result of ``reconcile_asbuilt``."""

    promoted: int = 0
    diverged: int = 0
    unchanged: int = 0
    edges_written: int = 0


# ---------------------------------------------------------------------------
# Bridge engine
# ---------------------------------------------------------------------------


class SystemDesignNetBoxBridge:
    """Functional-location sync + promoted_to_asbuilt reconciliation engine.

    Parameters
    ----------
    conn:
        ``asyncpg.Connection`` — namespace GUC must already be set by the caller
        (``scoped_pg_session``).  The bridge never touches the GUC.
    namespace_id:
        Active namespace UUID (for RLS-scoped graph writes).
    namespace_slug:
        Human-readable slug used in FL label construction — must match the slug
        used when the design-intent nodes were authored by ``graph.py``.
    netbox_client:
        ``_NetBoxClient`` pointed at the tenant's NetBox instance.
    """

    def __init__(
        self,
        conn: asyncpg.Connection,  # type: ignore[type-arg]
        namespace_id: uuid.UUID,
        namespace_slug: str,
        netbox_client: _NetBoxClient,
    ) -> None:
        self._conn = conn
        self._ns = namespace_id
        self._slug = namespace_slug
        self._nb = netbox_client

    # ------------------------------------------------------------------
    # Phase 1b, Step 1: push design-intent tree to NetBox
    # ------------------------------------------------------------------

    async def sync_fl_to_netbox(
        self,
        site_name: str,
        buildings: list[dict[str, Any]],
        source_id: str | None = None,
    ) -> SyncResult:
        """Push the SITE>BUILDING>FLOOR>ROOM>POSITION tree to NetBox.

        Creates NetBox sites and locations from the authored design-intent
        hierarchy.  Existing objects are reused (matched by normalised name).
        Writes ``sync_to_netbox`` edges from each FL intent node to its
        corresponding NetBox object label.

        Parameters
        ----------
        site_name:
            Top-level site name (root of the FL tree, authored by ``graph.py``).
        buildings:
            Same ``buildings`` structure accepted by ``do_author_functional_location``.
        source_id:
            Optional provenance tag (``system_design_source_id``).

        Returns
        -------
        SyncResult
            Counts of objects created / reused and edges written.
        """
        result = SyncResult()
        edges: list[tuple[str, str, str, float]] = []

        # Fetch existing NetBox objects once per sync run (not per node) to
        # minimise round-trips (the D365 bridge uses the same pattern).
        existing_sites = await self._nb.fetch_sites()
        existing_locs = await self._nb.fetch_locations()

        site_by_norm: dict[str, dict[str, Any]] = {_normalize(s["name"]): s for s in existing_sites}
        loc_by_norm_site: dict[tuple[str, int], dict[str, Any]] = {
            (_normalize(loc["name"]), (loc.get("site") or {}).get("id", -1)): loc
            for loc in existing_locs
        }

        # --- SITE ---
        nb_site = site_by_norm.get(_normalize(site_name))
        if nb_site is None:
            slug = _slugify(site_name)
            try:
                nb_site = await self._nb.create_site(site_name, slug)
                result.sites_created += 1
                log.info("[SD-NB-BRIDGE] created NetBox site name=%r slug=%r", site_name, slug)
            except Exception as exc:
                msg = f"Failed to create site {site_name!r}: {exc}"
                log.warning("[SD-NB-BRIDGE] %s", msg)
                result.errors.append(msg)
                return result
        else:
            result.sites_reused += 1
            log.debug("[SD-NB-BRIDGE] reused NetBox site name=%r id=%d", site_name, nb_site["id"])

        nb_site_id: int = nb_site["id"]
        site_fl_label = _fl_label(self._slug, site_name)
        nb_site_label = f"NetBoxSite:{nb_site_id}"
        edges.append((site_fl_label, _PRED_SYNC_TO_NB, nb_site_label, _CONF_STRUCTURAL))

        # --- BUILDING > FLOOR > ROOM > POSITION ---
        for building in buildings:
            bld_name: str = building["name"]
            nb_bld = loc_by_norm_site.get((_normalize(bld_name), nb_site_id))
            if nb_bld is None:
                try:
                    nb_bld = await self._nb.create_location(
                        bld_name, _slugify(bld_name), nb_site_id
                    )
                    result.locations_created += 1
                    # Refresh lookup with newly created object
                    loc_by_norm_site[(_normalize(bld_name), nb_site_id)] = nb_bld
                    log.info("[SD-NB-BRIDGE] created location building=%r", bld_name)
                except Exception as exc:
                    result.errors.append(f"Failed to create building {bld_name!r}: {exc}")
                    continue
            else:
                result.locations_reused += 1

            nb_bld_id: int = nb_bld["id"]
            bld_fl_label = _fl_label(self._slug, site_name, bld_name)
            edges.append(
                (bld_fl_label, _PRED_SYNC_TO_NB, f"NetBoxLocation:{nb_bld_id}", _CONF_STRUCTURAL)
            )

            for floor in building.get("floors", []):
                flr_name: str = floor["name"]
                nb_flr = loc_by_norm_site.get((_normalize(flr_name), nb_site_id))
                if nb_flr is None:
                    try:
                        nb_flr = await self._nb.create_location(
                            flr_name, _slugify(flr_name), nb_site_id, parent_id=nb_bld_id
                        )
                        result.locations_created += 1
                        loc_by_norm_site[(_normalize(flr_name), nb_site_id)] = nb_flr
                        log.info("[SD-NB-BRIDGE] created location floor=%r", flr_name)
                    except Exception as exc:
                        result.errors.append(f"Failed to create floor {flr_name!r}: {exc}")
                        continue
                else:
                    result.locations_reused += 1

                nb_flr_id: int = nb_flr["id"]
                flr_fl_label = _fl_label(self._slug, site_name, bld_name, flr_name)
                edges.append(
                    (
                        flr_fl_label,
                        _PRED_SYNC_TO_NB,
                        f"NetBoxLocation:{nb_flr_id}",
                        _CONF_STRUCTURAL,
                    )
                )

                for room in floor.get("rooms", []):
                    room_name: str = room["name"]
                    nb_room = loc_by_norm_site.get((_normalize(room_name), nb_site_id))
                    if nb_room is None:
                        try:
                            nb_room = await self._nb.create_location(
                                room_name,
                                _slugify(room_name),
                                nb_site_id,
                                parent_id=nb_flr_id,
                            )
                            result.locations_created += 1
                            loc_by_norm_site[(_normalize(room_name), nb_site_id)] = nb_room
                            log.info("[SD-NB-BRIDGE] created location room=%r", room_name)
                        except Exception as exc:
                            result.errors.append(f"Failed to create room {room_name!r}: {exc}")
                            continue
                    else:
                        result.locations_reused += 1

                    nb_room_id: int = nb_room["id"]
                    room_fl_label = _fl_label(self._slug, site_name, bld_name, flr_name, room_name)
                    edges.append(
                        (
                            room_fl_label,
                            _PRED_SYNC_TO_NB,
                            f"NetBoxLocation:{nb_room_id}",
                            _CONF_STRUCTURAL,
                        )
                    )

                    for pos_name in room.get("positions", []):
                        nb_pos = loc_by_norm_site.get((_normalize(pos_name), nb_site_id))
                        if nb_pos is None:
                            try:
                                nb_pos = await self._nb.create_location(
                                    pos_name,
                                    _slugify(pos_name),
                                    nb_site_id,
                                    parent_id=nb_room_id,
                                )
                                result.locations_created += 1
                                loc_by_norm_site[(_normalize(pos_name), nb_site_id)] = nb_pos
                                log.info("[SD-NB-BRIDGE] created location position=%r", pos_name)
                            except Exception as exc:
                                result.errors.append(
                                    f"Failed to create position {pos_name!r}: {exc}"
                                )
                                continue
                        else:
                            result.locations_reused += 1

                        pos_fl_label = _fl_label(
                            self._slug, site_name, bld_name, flr_name, room_name, pos_name
                        )
                        nb_pos_id: int = nb_pos["id"]
                        edges.append(
                            (
                                pos_fl_label,
                                _PRED_SYNC_TO_NB,
                                f"NetBoxLocation:{nb_pos_id}",
                                _CONF_STRUCTURAL,
                            )
                        )

        result.edges_written = await self._upsert_kg_edges_batch(edges, source_id)
        log.info(
            "[SD-NB-BRIDGE] sync_fl_to_netbox ns=%s site=%r sites_created=%d "
            "locs_created=%d edges=%d errors=%d",
            self._ns,
            site_name,
            result.sites_created,
            result.locations_created,
            result.edges_written,
            len(result.errors),
        )
        return result

    # ------------------------------------------------------------------
    # Phase 1b, Step 2: reconcile as-built confirmations
    # ------------------------------------------------------------------

    async def reconcile_asbuilt(
        self,
        confirmations: list[dict[str, Any]],
        source_id: str | None = None,
    ) -> ReconcileResult:
        """Reconcile as-built confirmations from NetBox/Assets post-install.

        Each confirmation carries the design-intent FL label and the observed
        as-built properties (name, status, any delta fields).  The bridge
        links the design-intent node to its as-built counterpart via a
        ``promoted_to_asbuilt`` edge.  If the as-built properties diverge from
        the intent, a ``has_divergence`` edge is written instead (and the
        promote edge is still written to preserve the link).

        The design-intent ``kg_node`` is **never modified** — it is linked only.

        Parameters
        ----------
        confirmations:
            List of dicts, each with:

            .. code-block:: python

                {
                    "intent_label": str,      # FL:<ns>:<path> from graph.py
                    "asbuilt_name": str,       # observed name in NetBox/Assets
                    "intent_name": str,        # authored name (for divergence check)
                    "confirmed": bool,         # True = install confirmed this room
                }

        source_id:
            Optional provenance tag.

        Returns
        -------
        ReconcileResult
            Counts of promoted / diverged / unchanged nodes and edges written.
        """
        result = ReconcileResult()
        edges: list[tuple[str, str, str, float]] = []

        for conf in confirmations:
            intent_label: str = conf["intent_label"]
            asbuilt_name: str = conf.get("asbuilt_name", "")
            intent_name: str = conf.get("intent_name", "")
            confirmed: bool = bool(conf.get("confirmed", False))

            if not confirmed:
                result.unchanged += 1
                continue

            asbuilt_label = _asbuilt_label(intent_label)
            diverged = _normalize(asbuilt_name) != _normalize(intent_name) and bool(asbuilt_name)
            status = ReconcileStatus.DIVERGED if diverged else ReconcileStatus.PROMOTED

            # promoted_to_asbuilt: intent → as-built (always written on confirmation)
            edges.append((intent_label, _PRED_PROMOTED, asbuilt_label, _CONF_STRUCTURAL))
            # as_built_confirms: as-built → intent (reverse link)
            edges.append((asbuilt_label, _PRED_CONFIRMS, intent_label, _CONF_STRUCTURAL))

            if diverged:
                # has_divergence: intent → as-built with reduced confidence
                edges.append((intent_label, _PRED_DIVERGENCE, asbuilt_label, _CONF_DIVERGED))
                result.diverged += 1
                log.warning(
                    "[SD-NB-BRIDGE] divergence: intent=%r asbuilt=%r ns=%s",
                    intent_name,
                    asbuilt_name,
                    self._ns,
                )
            else:
                result.promoted += 1
                log.info("[SD-NB-BRIDGE] promoted: label=%r ns=%s", intent_label, self._ns)

            _ = status  # used above for branching; kept for explicit named enum

        result.edges_written = await self._upsert_kg_edges_batch(edges, source_id)
        log.info(
            "[SD-NB-BRIDGE] reconcile_asbuilt ns=%s promoted=%d diverged=%d unchanged=%d edges=%d",
            self._ns,
            result.promoted,
            result.diverged,
            result.unchanged,
            result.edges_written,
        )
        return result

    # ------------------------------------------------------------------
    # DB helper — same UNNEST pattern as dynamics365/netbox_bridge.py
    # ------------------------------------------------------------------

    async def _upsert_kg_edges_batch(
        self,
        edges: list[tuple[str, str, str, float]],
        source_id: str | None = None,
    ) -> int:
        """Batch-upsert kg_edges using UNNEST (same pattern as D365 bridge)."""
        if not edges:
            return 0

        result = await self._conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence,
                 namespace_id, change_origin, system_design_source_id)
            SELECT
                unnest($1::text[]),
                unnest($2::text[]),
                unnest($3::text[]),
                unnest($4::float[]),
                $5::uuid,
                'sync',
                $6
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence               = EXCLUDED.confidence,
                    change_origin            = 'sync',
                    system_design_source_id  = COALESCE(
                        EXCLUDED.system_design_source_id,
                        kg_edges.system_design_source_id
                    ),
                    updated_at               = NOW()
            """,
            [e[0] for e in edges],
            [e[1] for e in edges],
            [e[2] for e in edges],
            [e[3] for e in edges],
            str(self._ns),
            source_id,
        )
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return len(edges)


# ---------------------------------------------------------------------------
# Public factory — reads token from environment (never from caller)
# ---------------------------------------------------------------------------


def build_bridge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    namespace_slug: str,
    *,
    netbox_url: str | None = None,
    netbox_token: str | None = None,
    page_size: int = 1000,
) -> SystemDesignNetBoxBridge:
    """Construct a ``SystemDesignNetBoxBridge`` using environment credentials.

    ``netbox_url`` and ``netbox_token`` default to ``cfg.NCE_NETBOX_URL`` and
    ``cfg.NCE_NETBOX_TOKEN`` so callers do not need to handle secrets.  Test
    fixtures may inject a mocked URL/token pair without touching the
    environment.

    Raises ``ValueError`` if either value resolves to an empty string.
    """
    url = (netbox_url or cfg.NCE_NETBOX_URL).rstrip("/")
    token = netbox_token or cfg.NCE_NETBOX_TOKEN

    if not url:
        raise ValueError("NetBox URL is not configured (NCE_NETBOX_URL is empty)")
    if not token:
        raise ValueError("NetBox token is not configured (NCE_NETBOX_TOKEN is empty)")

    nb_client = _NetBoxClient(url, token, page_size=page_size)
    return SystemDesignNetBoxBridge(conn, namespace_id, namespace_slug, nb_client)
