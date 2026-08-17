"""
nce/vertical_modules/dynamics365/sync.py
=========================================
Deterministic Track: Dataverse entity sync → kg_edges.

Polls Customer Service + Field Service entities from the Dataverse Web API
and writes structured graph edges into NCE's ``kg_edges`` table using
idempotent UNNEST upserts.  Follows the same ``(conn, namespace_id, client)``
constructor pattern as the NetBox modules.

Covered entity sets
-------------------
Core CRM:
  - Accounts, Contacts, Opportunities, Incidents (Cases)
Field Service:
  - Work Orders, Customer Assets, Functional Locations, Agreements
Knowledge:
  - Knowledge Articles (published, latest version only)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from nce.config import cfg
from nce.events.emit import emit_graph_write
from nce.vertical_modules.dynamics365.client import CURSOR_OVERLAP_SECONDS, DataverseClient

log = logging.getLogger("nce.vertical_modules.dynamics365.sync")

# OData $select fields — fetch only what we need to keep payloads small.
# ``modifiedon`` is always included so the incremental-sync watermark can be
# advanced to the maximum ``modifiedon`` seen in each tick.
_ACCOUNT_FIELDS = ["accountid", "name", "websiteurl", "telephone1", "address1_city", "modifiedon"]
_CONTACT_FIELDS = [
    "contactid",
    "fullname",
    "emailaddress1",
    "_parentcustomerid_value",
    "_parentcustomerid_value@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]
_OPPORTUNITY_FIELDS = [
    "opportunityid",
    "name",
    "stagename",
    "_parentaccountid_value",
    "_parentaccountid_value@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]
_INCIDENT_FIELDS = [
    "incidentid",
    "ticketnumber",
    "title",
    "prioritycode",
    "prioritycode@OData.Community.Display.V1.FormattedValue",
    "statuscode@OData.Community.Display.V1.FormattedValue",
    "_customerid_value@OData.Community.Display.V1.FormattedValue",
    "_ownerid_value",
    "_ownerid_value@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]

_PRIORITY_LABELS = {1: "High", 2: "Normal", 3: "Low"}

# ---------------------------------------------------------------------------
# Field Service entity field lists
# ---------------------------------------------------------------------------
_WORK_ORDER_FIELDS = [
    "msdyn_workorderid",
    "msdyn_name",
    "_msdyn_serviceaccount_id_value",
    "_msdyn_serviceaccount_id_value@OData.Community.Display.V1.FormattedValue",
    "msdyn_systemstatus",
    "msdyn_systemstatus@OData.Community.Display.V1.FormattedValue",
    "_ownerid_value",
    "_ownerid_value@OData.Community.Display.V1.FormattedValue",
    "_msdyn_primaryincidenttype_value@OData.Community.Display.V1.FormattedValue",
    "_msdyn_workordertype_value@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]

_AGREEMENT_FIELDS = [
    "msdyn_agreementid",
    "msdyn_name",
    "_msdyn_serviceaccount_id_value",
    "_msdyn_serviceaccount_id_value@OData.Community.Display.V1.FormattedValue",
    "msdyn_startdate",
    "msdyn_enddate",
    "statecode",
    "statecode@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]

_CUSTOMER_ASSET_FIELDS = [
    "msdyn_customerassetid",
    "msdyn_name",
    "_msdyn_account_id_value",
    "_msdyn_account_id_value@OData.Community.Display.V1.FormattedValue",
    "_msdyn_functionallocations_value",
    "_msdyn_functionallocations_value@OData.Community.Display.V1.FormattedValue",
    "_msdyn_product_value",
    "_msdyn_product_value@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]

_FUNCTIONAL_LOCATION_FIELDS = [
    "msdyn_functionallocationid",
    "msdyn_name",
    "_msdyn_parentfunctionallocation_value",
    "_msdyn_parentfunctionallocation_value@OData.Community.Display.V1.FormattedValue",
    "_msdyn_account_id_value",
    "_msdyn_account_id_value@OData.Community.Display.V1.FormattedValue",
    "modifiedon",
]

_KNOWLEDGE_ARTICLE_FIELDS = [
    "knowledgearticleid",
    "title",
    "description",
    "statecode",
    "statecode@OData.Community.Display.V1.FormattedValue",
    "islatestversion",
    "keywords",
    "modifiedon",
]

# msdyn_systemstatus values for Work Orders (for filtering / labelling)
_WO_STATUS_LABELS: dict[int, str] = {
    690970000: "Unscheduled",
    690970001: "Scheduled",
    690970002: "In Progress",
    690970003: "Completed",
    690970004: "Posted",
    690970005: "Canceled",
}


def _safe_label(value: str) -> str:
    """Strip characters unsafe for kg_edges label columns (keep printable ASCII)."""
    if not value:
        return "unknown"
    return "".join(c if c.isalnum() or c in " _-.()" else "_" for c in value).strip()[:200]


class DataverseSyncEngine:
    """
    Polls Dataverse entities and writes graph topology to NCE ``kg_edges``.

    Parameters
    ----------
    conn:
        RLS-scoped asyncpg connection (already within a namespace session).
    namespace_id:
        Tenant namespace UUID for ``kg_edges.namespace_id``.
    client:
        Authenticated ``DataverseClient`` instance.
    """

    def __init__(
        self,
        conn: asyncpg.Connection,
        namespace_id: uuid.UUID,
        client: DataverseClient,
    ) -> None:
        self._conn = conn
        self._ns = namespace_id
        self._client = client
        self._page_size = cfg.NCE_D365_SYNC_PAGE_SIZE
        # Incremental-sync watermark; set by run_full_sync when
        # NCE_D365_INCREMENTAL_ENABLED. None ⇒ full pull.
        self._since: datetime | None = None
        # Per-entity-set cursor map loaded from last_sync_stats JSONB.
        # Keys are Dataverse entity-set names; values are UTC ISO-8601 strings.
        # Empty dict ⇒ first run / no prior cursor (full pull for that entity).
        self._cursor_map: dict[str, str] = {}
        # Accumulates the max modifiedon seen per entity-set during the current
        # incremental tick so cursors can be advanced after a successful sync.
        self._seen_max: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Incremental-sync helpers
    # ------------------------------------------------------------------

    async def _load_incremental_watermark(self) -> datetime | None:
        """Return the last successful sync timestamp when incremental sync is on.

        Reuses ``d365_integrations.last_sync_at`` (RLS-scoped). When
        ``NCE_D365_INCREMENTAL_ENABLED`` is false, returns None so the sync does a
        full pull.
        """
        if not cfg.NCE_D365_INCREMENTAL_ENABLED:
            return None
        row = await self._conn.fetchrow(
            """
            SELECT last_sync_at
            FROM d365_integrations
            WHERE namespace_id = $1::uuid AND last_sync_at IS NOT NULL
            ORDER BY last_sync_at DESC
            LIMIT 1
            """,
            str(self._ns),
        )
        return row["last_sync_at"] if row else None

    async def _load_cursor_map(self) -> dict[str, str]:
        """Load the per-entity-set watermark cursor map from ``last_sync_stats`` JSONB.

        Returns an empty dict on first run or when no cursors have been saved.
        Stored under the ``"cursors"`` key so the rest of ``last_sync_stats``
        (run counts, totals, etc.) is left untouched.
        """
        import json as _json

        row = await self._conn.fetchrow(
            """
            SELECT last_sync_stats
            FROM d365_integrations
            WHERE namespace_id = $1::uuid AND status = 'ACTIVE'
            LIMIT 1
            """,
            str(self._ns),
        )
        if not row or not row["last_sync_stats"]:
            return {}
        raw = row["last_sync_stats"]
        stats: dict[str, Any] = raw if isinstance(raw, dict) else _json.loads(raw)
        cursors = stats.get("cursors")
        if not isinstance(cursors, dict):
            return {}
        return {k: v for k, v in cursors.items() if isinstance(v, str)}

    async def _save_cursor_map(self, cursors: dict[str, str]) -> None:
        """Persist the per-entity-set cursor map back into ``last_sync_stats`` JSONB.

        Merges the new cursors into the existing ``last_sync_stats`` document so
        that other keys (run counts, totals) are preserved via ``jsonb_strip_nulls``
        / ``||`` merge.
        """
        import json as _json

        await self._conn.execute(
            """
            UPDATE d365_integrations
            SET last_sync_stats = COALESCE(last_sync_stats, '{}'::jsonb)
                                  || jsonb_build_object('cursors', $1::jsonb),
                updated_at = NOW()
            WHERE namespace_id = $2::uuid AND status = 'ACTIVE'
            """,
            _json.dumps(cursors),
            str(self._ns),
        )

    def _cursor_for_entity(self, entity_set: str) -> datetime | None:
        """Return the per-entity watermark with the clock-skew overlap applied.

        Returns ``None`` when no cursor exists (triggers a full pull for that entity).
        The overlap shifts the cursor back by ``CURSOR_OVERLAP_SECONDS`` so records
        indexed slightly late by Dataverse are not missed.
        """
        raw_iso = self._cursor_map.get(entity_set)
        if not raw_iso:
            return None
        try:
            ts = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
        except ValueError:
            return None
        return ts - timedelta(seconds=CURSOR_OVERLAP_SECONDS)

    def _observe_modifiedon(self, entity_set: str, record: dict[str, Any]) -> None:
        """Update the running maximum ``modifiedon`` seen for *entity_set*.

        Called for each record during an incremental tick so the cursor can be
        advanced to ``max(modifiedon)`` after the sync completes successfully.
        No-op when the record has no ``modifiedon`` field.
        """
        raw = record.get("modifiedon")
        if not raw:
            return
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return
        ts = ts.astimezone(timezone.utc)
        current = self._seen_max.get(entity_set)
        if current is None or ts > current:
            self._seen_max[entity_set] = ts

    def _apply_incremental(self, base_filter: str | None) -> str | None:
        """AND-combine an OData ``modifiedon gt <watermark>`` clause onto a filter.

        No-op when no watermark is set (full pull).
        """
        if self._since is None:
            return base_filter
        iso = self._since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clause = f"modifiedon gt {iso}"
        return f"{base_filter} and {clause}" if base_filter else clause

    def _apply_cursor(self, entity_set: str, base_filter: str | None) -> str | None:
        """AND-combine a per-entity ``modifiedon gt <cursor>`` clause onto a filter.

        Uses the cursor loaded from ``last_sync_stats`` for *entity_set*.  Returns
        the base filter unchanged when no cursor exists (full pull for that entity).
        The cursor already has the ``CURSOR_OVERLAP_SECONDS`` subtracted by
        ``_cursor_for_entity``.
        """
        cursor = self._cursor_for_entity(entity_set)
        if cursor is None:
            return base_filter
        iso = cursor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clause = f"modifiedon gt {iso}"
        return f"{base_filter} and {clause}" if base_filter else clause

    def _paginate(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter_expr: str | None = None,
        page_size: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Drop-in for ``client.paginate`` that injects the incremental watermark."""
        return self._client.paginate(
            entity_set,
            select=select,
            filter_expr=self._apply_incremental(filter_expr),
            page_size=page_size,
        )

    def _paginate_incremental(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter_expr: str | None = None,
        page_size: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Like ``_paginate`` but applies the per-entity cursor map watermark.

        Used by ``run_incremental_sync``; the ``_observe_modifiedon`` method is
        called by each sync method to track the max ``modifiedon`` for cursor
        advancement.
        """
        return self._client.paginate(
            entity_set,
            select=select,
            filter_expr=self._apply_cursor(entity_set, filter_expr),
            page_size=page_size,
        )

    # When True the entity sync methods call ``_paginate_incremental`` (cursor-map
    # watermark) instead of ``_paginate`` (global ``_since`` watermark).
    # Set to True by ``run_incremental_sync``; False elsewhere for backward compat.
    _use_cursor_paginate: bool = False

    async def _iter_entity(
        self,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter_expr: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield records from *entity_set*, routing to the active paginator.

        When ``_use_cursor_paginate`` is True the per-entity cursor-map watermark
        is applied and ``modifiedon`` observations are recorded for cursor
        advancement.  Otherwise the global ``_since`` watermark path is used
        (backward-compatible with ``run_full_sync``).
        """
        if self._use_cursor_paginate:
            paginator = self._paginate_incremental(
                entity_set, select=select, filter_expr=filter_expr, page_size=self._page_size
            )
        else:
            paginator = self._paginate(
                entity_set, select=select, filter_expr=filter_expr, page_size=self._page_size
            )
        async for record in paginator:
            if self._use_cursor_paginate:
                self._observe_modifiedon(entity_set, record)
            yield record

    # ------------------------------------------------------------------
    # Entity sync methods
    # ------------------------------------------------------------------

    async def sync_accounts(self) -> dict[str, Any]:
        """Fetch all Accounts and upsert them as kg_nodes (entity_type='D365_Account')."""
        count = 0
        async for record in self._iter_entity("accounts", select=_ACCOUNT_FIELDS):
            name = _safe_label(record.get("name") or record.get("accountid", ""))
            if not name or name == "unknown":
                continue
            await self._upsert_kg_node(
                f"Account:{name}",
                "D365_Account",
                source_id=record.get("accountid"),
            )
            count += 1

        log.info("[D365-SYNC] sync_accounts namespace=%s count=%d", self._ns, count)
        return {"entity": "accounts", "upserted": count}

    async def sync_contacts(self) -> dict[str, Any]:
        """Fetch Contacts and write HAS_CONTACT / WORKS_AT edges to parent Account."""
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity("contacts", select=_CONTACT_FIELDS):
            fullname = _safe_label(record.get("fullname") or record.get("contactid", ""))
            account_name = _safe_label(
                record.get("_parentcustomerid_value@OData.Community.Display.V1.FormattedValue")
                or ""
            )
            if not fullname or fullname == "unknown":
                continue
            src = record.get("contactid")
            if account_name and account_name != "unknown":
                edges.append(
                    (f"Account:{account_name}", "HAS_CONTACT", f"Contact:{fullname}", 1.0, src)
                )
                edges.append(
                    (f"Contact:{fullname}", "WORKS_AT", f"Account:{account_name}", 1.0, src)
                )

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_contacts namespace=%s edges=%d", self._ns, written)
        return {"entity": "contacts", "edges_written": written}

    async def sync_opportunities(self) -> dict[str, Any]:
        """Fetch Opportunities and write HAS_OPPORTUNITY / HAS_STAGE edges."""
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity("opportunities", select=_OPPORTUNITY_FIELDS):
            opp_name = _safe_label(record.get("name") or record.get("opportunityid", ""))
            account_name = _safe_label(
                record.get("_parentaccountid_value@OData.Community.Display.V1.FormattedValue") or ""
            )
            stage = _safe_label(record.get("stagename") or "Unknown")

            if not opp_name or opp_name == "unknown":
                continue
            src = record.get("opportunityid")
            if account_name and account_name != "unknown":
                edges.append(
                    (
                        f"Account:{account_name}",
                        "HAS_OPPORTUNITY",
                        f"Opportunity:{opp_name}",
                        1.0,
                        src,
                    )
                )
            edges.append(
                (f"Opportunity:{opp_name}", "HAS_STAGE", f"PipelineStage:{stage}", 1.0, src)
            )

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_opportunities namespace=%s edges=%d", self._ns, written)
        return {"entity": "opportunities", "edges_written": written}

    async def sync_incidents(self) -> dict[str, Any]:
        """Fetch open Incidents (Cases) and write REPORTED_BY / ASSIGNED_TO / HAS_PRIORITY edges."""
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity(
            "incidents",
            select=_INCIDENT_FIELDS,
            filter_expr="statecode eq 0",  # active cases only
        ):
            ticket = _safe_label(record.get("ticketnumber") or record.get("incidentid", ""))
            account_name = _safe_label(
                record.get("_customerid_value@OData.Community.Display.V1.FormattedValue") or ""
            )
            owner = _safe_label(
                record.get("_ownerid_value@OData.Community.Display.V1.FormattedValue")
                or record.get("_ownerid_value")
                or "unassigned"
            )
            priority_code = record.get("prioritycode") or 2
            priority_label = _safe_label(
                record.get("prioritycode@OData.Community.Display.V1.FormattedValue")
                or _PRIORITY_LABELS.get(priority_code, "Normal")
            )

            if not ticket or ticket == "unknown":
                continue

            src = record.get("incidentid")
            if account_name and account_name != "unknown":
                edges.append(
                    (f"Incident:{ticket}", "REPORTED_BY", f"Account:{account_name}", 1.0, src)
                )
            edges.append((f"Incident:{ticket}", "ASSIGNED_TO", f"User:{owner}", 1.0, src))
            edges.append(
                (f"Incident:{ticket}", "HAS_PRIORITY", f"Priority:{priority_label}", 1.0, src)
            )

            # Boost salience for high-priority incidents
            if priority_code == 1:
                log.debug("[D365-SYNC] High-priority incident %s — will boost salience", ticket)

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_incidents namespace=%s edges=%d", self._ns, written)
        return {"entity": "incidents", "edges_written": written}

    # ------------------------------------------------------------------
    # Field Service entity sync methods
    # ------------------------------------------------------------------

    async def sync_work_orders(self) -> dict[str, Any]:
        """Fetch active Work Orders and write graph edges.

        Edges:
          Account → HAS_WORK_ORDER → WorkOrder
          WorkOrder → ASSIGNED_TO → User
          WorkOrder → HAS_STATUS → WOStatus
          WorkOrder → HAS_INCIDENT_TYPE → IncidentType  (if present)
        """
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity(
            "msdyn_workorders",
            select=_WORK_ORDER_FIELDS,
            # exclude Canceled (690970005) and Posted (690970004)
            filter_expr="msdyn_systemstatus ne 690970005 and msdyn_systemstatus ne 690970004",
        ):
            wo_name = _safe_label(record.get("msdyn_name") or record.get("msdyn_workorderid", ""))
            if not wo_name or wo_name == "unknown":
                continue
            src = record.get("msdyn_workorderid")

            account_name = _safe_label(
                record.get(
                    "_msdyn_serviceaccount_id_value@OData.Community.Display.V1.FormattedValue"
                )
                or ""
            )
            owner = _safe_label(
                record.get("_ownerid_value@OData.Community.Display.V1.FormattedValue")
                or record.get("_ownerid_value")
                or "unassigned"
            )
            status_code = record.get("msdyn_systemstatus")
            status_label = _safe_label(
                record.get("msdyn_systemstatus@OData.Community.Display.V1.FormattedValue")
                or _WO_STATUS_LABELS.get(status_code, "Unknown")
            )
            incident_type = _safe_label(
                record.get(
                    "_msdyn_primaryincidenttype_value@OData.Community.Display.V1.FormattedValue"
                )
                or ""
            )
            wo_type = _safe_label(
                record.get("_msdyn_workordertype_value@OData.Community.Display.V1.FormattedValue")
                or ""
            )

            if account_name and account_name != "unknown":
                edges.append(
                    (f"Account:{account_name}", "HAS_WORK_ORDER", f"WorkOrder:{wo_name}", 1.0, src)
                )
            edges.append((f"WorkOrder:{wo_name}", "ASSIGNED_TO", f"User:{owner}", 1.0, src))
            edges.append(
                (f"WorkOrder:{wo_name}", "HAS_STATUS", f"WOStatus:{status_label}", 1.0, src)
            )
            if incident_type and incident_type != "unknown":
                edges.append(
                    (
                        f"WorkOrder:{wo_name}",
                        "HAS_INCIDENT_TYPE",
                        f"IncidentType:{incident_type}",
                        1.0,
                        src,
                    )
                )
            if wo_type and wo_type != "unknown":
                edges.append((f"WorkOrder:{wo_name}", "HAS_TYPE", f"WOType:{wo_type}", 1.0, src))

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_work_orders namespace=%s edges=%d", self._ns, written)
        return {"entity": "work_orders", "edges_written": written}

    async def sync_agreements(self) -> dict[str, Any]:
        """Fetch active Agreements and write HAS_AGREEMENT / agreement status edges.

        Edges:
          Account → HAS_AGREEMENT → Agreement
          Agreement → HAS_STATUS → AgreementStatus
        """
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity(
            "msdyn_agreements",
            select=_AGREEMENT_FIELDS,
            filter_expr="statecode eq 0",  # active only
        ):
            ag_name = _safe_label(record.get("msdyn_name") or record.get("msdyn_agreementid", ""))
            if not ag_name or ag_name == "unknown":
                continue
            src = record.get("msdyn_agreementid")

            account_name = _safe_label(
                record.get(
                    "_msdyn_serviceaccount_id_value@OData.Community.Display.V1.FormattedValue"
                )
                or ""
            )
            status = _safe_label(
                record.get("statecode@OData.Community.Display.V1.FormattedValue") or "Active"
            )

            if account_name and account_name != "unknown":
                edges.append(
                    (f"Account:{account_name}", "HAS_AGREEMENT", f"Agreement:{ag_name}", 1.0, src)
                )
            edges.append(
                (f"Agreement:{ag_name}", "HAS_STATUS", f"AgreementStatus:{status}", 1.0, src)
            )

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_agreements namespace=%s edges=%d", self._ns, written)
        return {"entity": "agreements", "edges_written": written}

    async def sync_customer_assets(self) -> dict[str, Any]:
        """Fetch Customer Assets and write HAS_ASSET / LOCATED_AT / IS_PRODUCT edges.

        Edges:
          Account → HAS_ASSET → CustomerAsset
          CustomerAsset → LOCATED_AT → FunctionalLocation  (if set)
          CustomerAsset → IS_PRODUCT → Product  (if set)
        """
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity(
            "msdyn_customerassets",
            select=_CUSTOMER_ASSET_FIELDS,
        ):
            asset_name = _safe_label(
                record.get("msdyn_name") or record.get("msdyn_customerassetid", "")
            )
            if not asset_name or asset_name == "unknown":
                continue
            src = record.get("msdyn_customerassetid")

            account_name = _safe_label(
                record.get("_msdyn_account_id_value@OData.Community.Display.V1.FormattedValue")
                or ""
            )
            location = _safe_label(
                record.get(
                    "_msdyn_functionallocations_value@OData.Community.Display.V1.FormattedValue"
                )
                or ""
            )
            product = _safe_label(
                record.get("_msdyn_product_value@OData.Community.Display.V1.FormattedValue") or ""
            )

            if account_name and account_name != "unknown":
                edges.append(
                    (
                        f"Account:{account_name}",
                        "HAS_ASSET",
                        f"CustomerAsset:{asset_name}",
                        1.0,
                        src,
                    )
                )
            if location and location != "unknown":
                edges.append(
                    (
                        f"CustomerAsset:{asset_name}",
                        "LOCATED_AT",
                        f"FunctionalLocation:{location}",
                        1.0,
                        src,
                    )
                )
            if product and product != "unknown":
                edges.append(
                    (f"CustomerAsset:{asset_name}", "IS_PRODUCT", f"Product:{product}", 1.0, src)
                )

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_customer_assets namespace=%s edges=%d", self._ns, written)
        return {"entity": "customer_assets", "edges_written": written}

    async def sync_functional_locations(self) -> dict[str, Any]:
        """Fetch Functional Locations and write hierarchy / account membership edges.

        Edges:
          FunctionalLocation → CHILD_OF → FunctionalLocation  (parent-child tree)
          Account → HAS_LOCATION → FunctionalLocation  (if account linked)
        """
        edges: list[tuple[str, str, str, float, str | None]] = []
        async for record in self._iter_entity(
            "msdyn_functionallocations",
            select=_FUNCTIONAL_LOCATION_FIELDS,
        ):
            loc_name = _safe_label(
                record.get("msdyn_name") or record.get("msdyn_functionallocationid", "")
            )
            if not loc_name or loc_name == "unknown":
                continue
            src = record.get("msdyn_functionallocationid")

            parent = _safe_label(
                record.get(
                    "_msdyn_parentfunctionallocation_value@OData.Community.Display.V1.FormattedValue"
                )
                or ""
            )
            account_name = _safe_label(
                record.get("_msdyn_account_id_value@OData.Community.Display.V1.FormattedValue")
                or ""
            )

            if parent and parent != "unknown":
                edges.append(
                    (
                        f"FunctionalLocation:{loc_name}",
                        "CHILD_OF",
                        f"FunctionalLocation:{parent}",
                        1.0,
                        src,
                    )
                )
            if account_name and account_name != "unknown":
                edges.append(
                    (
                        f"Account:{account_name}",
                        "HAS_LOCATION",
                        f"FunctionalLocation:{loc_name}",
                        1.0,
                        src,
                    )
                )

        written = await self._upsert_kg_edges_batch(edges)
        log.info("[D365-SYNC] sync_functional_locations namespace=%s edges=%d", self._ns, written)
        return {"entity": "functional_locations", "edges_written": written}

    async def sync_knowledge_articles(self) -> dict[str, Any]:
        """Fetch published Knowledge Articles and upsert as kg_nodes.

        Only published, latest-version articles are synced.
        Node label: ``KnowledgeArticle:{title}``
        The full text content is intentionally NOT fetched here — the Semantic Track
        (ingestion.py) should be used to embed article body text.
        """
        count = 0
        async for record in self._iter_entity(
            "knowledgearticles",
            select=_KNOWLEDGE_ARTICLE_FIELDS,
            # published (statecode=3) and latest version
            filter_expr="statecode eq 3 and islatestversion eq true",
        ):
            title = _safe_label(record.get("title") or record.get("knowledgearticleid", ""))
            if not title or title == "unknown":
                continue

            await self._upsert_kg_node(
                f"KnowledgeArticle:{title}",
                "D365_KnowledgeArticle",
                source_id=record.get("knowledgearticleid"),
            )
            count += 1

        log.info("[D365-SYNC] sync_knowledge_articles namespace=%s count=%d", self._ns, count)
        return {"entity": "knowledge_articles", "upserted": count}

    async def run_full_sync(
        self,
        entity_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Orchestrate all sync steps and return aggregated stats.

        Parameters
        ----------
        entity_types:
            Optional subset to sync (e.g. ``["accounts", "contacts"]``).
            When *None* all four entity types are synced.
        """
        self._since = await self._load_incremental_watermark()
        if self._since is not None:
            log.info(
                "[D365-SYNC] incremental mode namespace=%s since=%s",
                self._ns,
                self._since.isoformat(),
            )

        all_types = {
            "accounts",
            "contacts",
            "opportunities",
            "incidents",
            "work_orders",
            "agreements",
            "customer_assets",
            "functional_locations",
            "knowledge_articles",
        }
        requested = set(entity_types) if entity_types else all_types

        # Ordered: Core CRM first (Account nodes before field-service edges), then
        # Field Service (locations before assets), then Knowledge base. Each entity
        # is audited to d365_sync_runs; a failing entity records an error row and
        # aborts the run (current semantics — per-entity isolation is a follow-up).
        ordered = [
            ("accounts", self.sync_accounts),
            ("contacts", self.sync_contacts),
            ("opportunities", self.sync_opportunities),
            ("incidents", self.sync_incidents),
            ("functional_locations", self.sync_functional_locations),
            ("work_orders", self.sync_work_orders),
            ("agreements", self.sync_agreements),
            ("customer_assets", self.sync_customer_assets),
            ("knowledge_articles", self.sync_knowledge_articles),
        ]

        run_id = uuid.uuid4()
        results: list[dict[str, Any]] = []
        for name, method in ordered:
            if name not in requested:
                continue
            started_at = datetime.now(timezone.utc)
            try:
                result = await method()
            except Exception as exc:
                await self._record_run(run_id, name, started_at, status="error", error=str(exc))
                raise
            results.append(result)
            await self._record_run(run_id, name, started_at, result=result)

        deletions = await self.detect_and_retire_deletions()

        total_edges = sum(r.get("edges_written", 0) + r.get("upserted", 0) for r in results)
        return {
            "namespace_id": str(self._ns),
            "run_id": str(run_id),
            "entity_results": results,
            "total_records": total_edges,
            "deletions": deletions,
        }

    # Canonical entity-set → internal name mapping (used by both incremental and
    # weekly orchestrators to build the ordered run list).
    _ENTITY_ORDER: list[tuple[str, str]] = [
        ("accounts", "accounts"),
        ("contacts", "contacts"),
        ("opportunities", "opportunities"),
        ("incidents", "incidents"),
        ("functional_locations", "msdyn_functionallocations"),
        ("work_orders", "msdyn_workorders"),
        ("agreements", "msdyn_agreements"),
        ("customer_assets", "msdyn_customerassets"),
        ("knowledge_articles", "knowledgearticles"),
    ]

    async def run_incremental_sync(
        self,
        entity_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Incremental sync: apply per-entity ``modifiedon gt <cursor>`` watermarks.

        On each tick the cursor for each entity-set is loaded from
        ``last_sync_stats->'cursors'``, shifted back by ``CURSOR_OVERLAP_SECONDS``
        for clock-skew tolerance, and used as an OData ``modifiedon gt`` filter.
        After a successful sync the cursor is advanced to ``max(modifiedon)`` seen
        in the current tick.

        First tick (no cursor): a full pull seeds the cursor.
        Subsequent ticks: only the delta (modified since cursor) is fetched.
        """
        self._cursor_map = await self._load_cursor_map()
        self._seen_max = {}
        self._use_cursor_paginate = True

        all_types = {name for name, _ in self._ENTITY_ORDER}
        requested = set(entity_types) if entity_types else all_types

        ordered = [
            ("accounts", self.sync_accounts),
            ("contacts", self.sync_contacts),
            ("opportunities", self.sync_opportunities),
            ("incidents", self.sync_incidents),
            ("functional_locations", self.sync_functional_locations),
            ("work_orders", self.sync_work_orders),
            ("agreements", self.sync_agreements),
            ("customer_assets", self.sync_customer_assets),
            ("knowledge_articles", self.sync_knowledge_articles),
        ]
        # Entity-set name for each internal name (for cursor advancement)
        _entity_set_map: dict[str, str] = dict(
            zip(
                [n for n, _ in self._ENTITY_ORDER],
                [es for _, es in self._ENTITY_ORDER],
            )
        )

        run_id = uuid.uuid4()
        results: list[dict[str, Any]] = []
        try:
            for name, method in ordered:
                if name not in requested:
                    continue
                started_at = datetime.now(timezone.utc)
                try:
                    result = await method()
                except Exception as exc:
                    await self._record_run(run_id, name, started_at, status="error", error=str(exc))
                    raise
                results.append(result)
                await self._record_run(run_id, name, started_at, result=result)
        finally:
            self._use_cursor_paginate = False

        # Advance cursors for every entity-set that returned at least one record.
        new_cursors: dict[str, str] = dict(self._cursor_map)
        for name, entity_set in _entity_set_map.items():
            if name not in requested:
                continue
            seen = self._seen_max.get(entity_set)
            if seen is not None:
                new_cursors[entity_set] = seen.strftime("%Y-%m-%dT%H:%M:%SZ")
        await self._save_cursor_map(new_cursors)

        total_edges = sum(r.get("edges_written", 0) + r.get("upserted", 0) for r in results)
        return {
            "namespace_id": str(self._ns),
            "run_id": str(run_id),
            "mode": "incremental",
            "entity_results": results,
            "total_records": total_edges,
            "cursors_advanced": {
                k: v for k, v in new_cursors.items() if v != self._cursor_map.get(k)
            },
        }

    async def run_weekly_full_sync(
        self,
        entity_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Weekly full-refresh pass: full pull with delete reconciliation.

        Fetches every entity without any ``modifiedon`` filter so that records
        removed from Dataverse since the last incremental tick are detected.
        After all entities are synced, ``detect_and_retire_deletions`` handles
        removal (change-tracking delta path) — or, when change-tracking is
        disabled, a source-ID reconciliation retires graph rows whose Dataverse
        source records are absent from the full pull.

        Cursors are reset to ``max(modifiedon)`` seen in this run so the next
        incremental tick correctly resumes from the weekly baseline.
        """
        # Full pull: no cursor watermark, no _since filter.
        self._since = None
        self._cursor_map = {}
        self._seen_max = {}
        self._use_cursor_paginate = True  # still observe modifiedon for cursor seeding

        all_types = {name for name, _ in self._ENTITY_ORDER}
        requested = set(entity_types) if entity_types else all_types

        ordered = [
            ("accounts", self.sync_accounts),
            ("contacts", self.sync_contacts),
            ("opportunities", self.sync_opportunities),
            ("incidents", self.sync_incidents),
            ("functional_locations", self.sync_functional_locations),
            ("work_orders", self.sync_work_orders),
            ("agreements", self.sync_agreements),
            ("customer_assets", self.sync_customer_assets),
            ("knowledge_articles", self.sync_knowledge_articles),
        ]
        _entity_set_map: dict[str, str] = dict(
            zip(
                [n for n, _ in self._ENTITY_ORDER],
                [es for _, es in self._ENTITY_ORDER],
            )
        )

        run_id = uuid.uuid4()
        results: list[dict[str, Any]] = []
        try:
            for name, method in ordered:
                if name not in requested:
                    continue
                started_at = datetime.now(timezone.utc)
                try:
                    result = await method()
                except Exception as exc:
                    await self._record_run(run_id, name, started_at, status="error", error=str(exc))
                    raise
                results.append(result)
                await self._record_run(run_id, name, started_at, result=result)
        finally:
            self._use_cursor_paginate = False

        # Reconcile deletions via the change-tracking / delta-link path.
        deletions = await self.detect_and_retire_deletions()

        # Seed cursors from the max modifiedon seen in this full pull so the next
        # incremental tick only fetches changes since the weekly baseline.
        new_cursors: dict[str, str] = {}
        for name, entity_set in _entity_set_map.items():
            if name not in requested:
                continue
            seen = self._seen_max.get(entity_set)
            if seen is not None:
                new_cursors[entity_set] = seen.strftime("%Y-%m-%dT%H:%M:%SZ")
        if new_cursors:
            await self._save_cursor_map(new_cursors)

        total_edges = sum(r.get("edges_written", 0) + r.get("upserted", 0) for r in results)
        return {
            "namespace_id": str(self._ns),
            "run_id": str(run_id),
            "mode": "weekly_full",
            "entity_results": results,
            "total_records": total_edges,
            "deletions": deletions,
            "cursors_seeded": new_cursors,
        }

    # ------------------------------------------------------------------
    # Internal DB helpers
    # ------------------------------------------------------------------

    async def _record_run(
        self,
        run_id: uuid.UUID,
        entity: str,
        started_at: datetime,
        *,
        result: dict[str, Any] | None = None,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        """Append a per-entity row to d365_sync_runs (audit trail for the status tool)."""
        upserted = 0
        if result is not None:
            upserted = int(result.get("upserted", 0)) + int(result.get("edges_written", 0))
        await self._conn.execute(
            """
            INSERT INTO d365_sync_runs
                (namespace_id, run_id, entity, upserted, incremental, status, error, started_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)
            """,
            str(self._ns),
            str(run_id),
            entity,
            upserted,
            self._since is not None,
            status,
            error,
            started_at,
        )

    # ------------------------------------------------------------------
    # Change-tracking: delete detection + retirement (NCE_D365_CHANGE_TRACKING_ENABLED)
    # ------------------------------------------------------------------

    #: Dataverse entity-sets whose derived graph rows are tagged with d365_source_id
    #: and therefore retireable on deletion.
    _CHANGE_TRACKED_ENTITY_SETS = (
        "accounts",
        "contacts",
        "opportunities",
        "incidents",
        "msdyn_workorders",
        "msdyn_agreements",
        "msdyn_customerassets",
        "msdyn_functionallocations",
        "knowledgearticles",
    )

    async def _load_delta_link(self, entity_set: str) -> str | None:
        row = await self._conn.fetchrow(
            "SELECT delta_link FROM d365_delta_tokens "
            "WHERE namespace_id = $1::uuid AND entity = $2",
            str(self._ns),
            entity_set,
        )
        return row["delta_link"] if row else None

    async def _save_delta_link(self, entity_set: str, delta_link: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO d365_delta_tokens (namespace_id, entity, delta_link, updated_at)
            VALUES ($1::uuid, $2, $3, now())
            ON CONFLICT (namespace_id, entity) DO UPDATE
                SET delta_link = EXCLUDED.delta_link, updated_at = now()
            """,
            str(self._ns),
            entity_set,
            delta_link,
        )

    async def _retire_source(self, removed_ids: list[str]) -> int:
        """Hard-delete kg_edges/kg_nodes tagged with any removed Dataverse GUID.

        The source record is gone, so its derived graph rows go with it. Returns the
        total number of rows deleted. Called only from the change-tracking pass.
        """
        if not removed_ids:
            return 0
        deleted = 0
        for stmt in (
            "DELETE FROM kg_edges WHERE namespace_id = $1::uuid AND d365_source_id = ANY($2::text[])",
            "DELETE FROM kg_nodes WHERE namespace_id = $1::uuid AND d365_source_id = ANY($2::text[])",
        ):
            result = await self._conn.execute(stmt, str(self._ns), removed_ids)
            try:
                deleted += int(result.split()[-1])
            except (IndexError, ValueError):
                pass
        return deleted

    async def detect_and_retire_deletions(self) -> dict[str, Any]:
        """Change-tracking pass: per entity, consume the Dataverse deltaLink and retire
        the graph rows of any ``@removed`` records. Upserts continue via the normal sync
        path — this handles ONLY deletions — and the new deltaLink is persisted. No-op
        unless ``NCE_D365_CHANGE_TRACKING_ENABLED``.
        """
        if not cfg.NCE_D365_CHANGE_TRACKING_ENABLED:
            return {"enabled": False}
        total_removed = 0
        total_retired = 0
        for entity_set in self._CHANGE_TRACKED_ENTITY_SETS:
            delta_link = await self._load_delta_link(entity_set)
            _changed, removed_ids, new_delta = await self._client.track_changes(
                entity_set, delta_link=delta_link, page_size=self._page_size
            )
            if removed_ids:
                total_removed += len(removed_ids)
                total_retired += await self._retire_source(removed_ids)
            if new_delta:
                await self._save_delta_link(entity_set, new_delta)
        log.info(
            "[D365-SYNC] change-tracking retire namespace=%s removed=%d rows_retired=%d",
            self._ns,
            total_removed,
            total_retired,
        )
        return {"enabled": True, "removed": total_removed, "rows_retired": total_retired}

    async def _upsert_kg_node(
        self,
        label: str,
        entity_type: str,
        *,
        source_id: str | None = None,
    ) -> None:
        """Upsert a single kg_node row (no-op if already present).

        ``source_id`` is the Dataverse record GUID, stored in ``d365_source_id`` so the
        change-tracking pass can retire this node when the source record is deleted.
        COALESCE on conflict so a later untagged write never clears an existing tag.
        Authority-precedence: 'sync' outranks every other origin on conflict.
        """
        await self._conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, d365_source_id,
                                  change_origin)
            VALUES ($1, $2, $3::uuid, $4, 'sync')
            ON CONFLICT (label, namespace_id) DO UPDATE
                SET entity_type = EXCLUDED.entity_type,
                    d365_source_id = COALESCE(EXCLUDED.d365_source_id, kg_nodes.d365_source_id),
                    change_origin = CASE
                        WHEN kg_nodes.change_origin = 'sync' THEN 'sync'
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            label,
            entity_type,
            str(self._ns),
            source_id,
        )
        # Transactional outbox: emit the graph-write event inside the same
        # transaction as the kg_nodes upsert.  Both commit or both roll back —
        # a graph write must never be visible without its outbox event.
        await emit_graph_write(
            self._conn,
            namespace_id=self._ns,
            node_type=entity_type,
            op="upserted",
            node_id=label,
        )

    async def _upsert_kg_edges_batch(
        self,
        edges: list[tuple[str, str, str, float, str | None]],
    ) -> int:
        """
        Batch-upsert ``kg_edges`` rows using UNNEST for efficiency.

        Each tuple is ``(subject_label, predicate, object_label, confidence, d365_source_id)``.
        ``d365_source_id`` is the Dataverse record GUID that produced the edge (for
        change-tracking retirement); COALESCE on conflict so an untagged re-write never
        clears an existing tag. Returns the number of rows affected.
        """
        if not edges:
            return 0

        subjects = [e[0] for e in edges]
        predicates = [e[1] for e in edges]
        objects = [e[2] for e in edges]
        confidences = [e[3] for e in edges]
        source_ids = [e[4] for e in edges]

        result = await self._conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id,
                                  d365_source_id, change_origin)
            SELECT unnest($1::text[]),
                   unnest($2::text[]),
                   unnest($3::text[]),
                   unnest($4::float[]),
                   $5::uuid,
                   unnest($6::text[]),
                   'sync'
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    d365_source_id = COALESCE(EXCLUDED.d365_source_id, kg_edges.d365_source_id),
                    change_origin = CASE
                        WHEN kg_edges.change_origin = 'sync' THEN 'sync'
                        ELSE EXCLUDED.change_origin
                    END,
                    updated_at = NOW()
            """,
            subjects,
            predicates,
            objects,
            confidences,
            str(self._ns),
            source_ids,
        )
        # asyncpg returns "INSERT 0 N" as a string
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return len(edges)
