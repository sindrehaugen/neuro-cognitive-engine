"""
nce/vertical_modules/sales/source_adapters/d365.py
===================================================
Dynamics 365 / Dataverse source adapter for the Sales vertical module.
Syncs CRM entities into the tenant-isolated ``sales_read_model`` table.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.config import cfg
from nce.vertical_modules.dynamics365.client import CURSOR_OVERLAP_SECONDS, DataverseClient

log = logging.getLogger("nce.vertical_modules.sales.source_adapters.d365")


def parse_datetime(val: Any) -> datetime | None:
    """Parse OData ISO-8601 modifiedon string to UTC timezone-aware datetime."""
    if not val:
        return None
    try:
        ts = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return ts.astimezone(timezone.utc)
    except ValueError:
        return None


# Dataverse entity configurations
_ENTITY_SETS = {
    "accounts": "accounts",
    "contacts": "contacts",
    "opportunities": "opportunities",
    "quotes": "quotes",
    "agreements": "msdyn_agreements",
    "systemusers": "systemusers",
    "incidents": "incidents",
    "appointments": "appointments",
    "customerassets": "msdyn_customerassets",
    "functionallocations": "msdyn_functionallocations",
}

_ENTITY_PK_FIELDS = {
    "accounts": "accountid",
    "contacts": "contactid",
    "opportunities": "opportunityid",
    "quotes": "quoteid",
    "agreements": "msdyn_agreementid",
    "systemusers": "systemuserid",
    "incidents": "incidentid",
    "appointments": "activityid",
    "customerassets": "msdyn_customerassetid",
    "functionallocations": "msdyn_functionallocationid",
}

_ENTITY_NAME_FIELDS = {
    "accounts": "name",
    "contacts": "fullname",
    "opportunities": "name",
    "quotes": "name",
    "agreements": "msdyn_name",
    "systemusers": "fullname",
    "incidents": "title",
    "appointments": "subject",
    "customerassets": "msdyn_name",
    "functionallocations": "msdyn_name",
}

_SELECT_FIELDS = {
    "accounts": ["accountid", "name", "address1_city", "example_industry", "modifiedon"],
    "contacts": ["contactid", "fullname", "_parentcustomerid_value", "modifiedon"],
    "opportunities": [
        "opportunityid",
        "name",
        "statecode",
        "estimatedvalue",
        "estimatedvalue_base",
        "salesstagecode",
        "stepname",
        "estimatedclosedate",
        "actualclosedate",
        "createdon",
        "_ownerid_value",
        "_ownerid_value@OData.Community.Display.V1.FormattedValue",
        "_customerid_value",
        "_customerid_value@OData.Community.Display.V1.FormattedValue",
        "example_estrecurringmonthly",
        "example_estrecurringmonthly_base",
        "example_customerneeds",
        "example_jobdescription",
        "description",
        "example_subject@OData.Community.Display.V1.FormattedValue",
        "modifiedon",
    ],
    "quotes": ["quoteid", "name", "statecode", "modifiedon"],
    "agreements": ["msdyn_agreementid", "msdyn_name", "modifiedon"],
    "systemusers": ["systemuserid", "fullname", "isdisabled", "title", "modifiedon"],
    "incidents": [
        "incidentid",
        "statecode",
        "title",
        "prioritycode",
        "ticketnumber",
        "createdon",
        "_ownerid_value",
        "_customerid_value",
        "_example_opportunityid_value",
        "modifiedon",
    ],
    "appointments": [
        "activityid",
        "statecode",
        "scheduledstart",
        "_ownerid_value",
        "subject",
        "modifiedon",
    ],
    "customerassets": [
        "msdyn_customerassetid",
        "msdyn_name",
        "_msdyn_account_value",
        "_msdyn_functionallocation_value",
        "modifiedon",
    ],
    "functionallocations": [
        "msdyn_functionallocationid",
        "msdyn_name",
        "msdyn_address1",
        "msdyn_city",
        "_msdyn_parentfunctionallocation_value@OData.Community.Display.V1.FormattedValue",
        "modifiedon",
    ],
}


class SalesD365SyncEngine:
    """
    Syncs Dynamics 365 CRM entities into NCE's tenant-isolated sales_read_model.
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

    async def _load_watermark(self, entity_name: str) -> str | None:
        """Load stored watermark/delta_link from d365_delta_tokens table."""
        row = await self._conn.fetchrow(
            "SELECT delta_link FROM d365_delta_tokens WHERE namespace_id = $1::uuid AND entity = $2",
            str(self._ns),
            f"sales:{entity_name}",
        )
        return row["delta_link"] if row else None

    async def _save_watermark(self, entity_name: str, watermark: str) -> None:
        """Persist watermark/delta_link to d365_delta_tokens table."""
        await self._conn.execute(
            """
            INSERT INTO d365_delta_tokens (namespace_id, entity, delta_link, updated_at)
            VALUES ($1::uuid, $2, $3, now())
            ON CONFLICT (namespace_id, entity) DO UPDATE
                SET delta_link = EXCLUDED.delta_link, updated_at = now()
            """,
            str(self._ns),
            f"sales:{entity_name}",
            watermark,
        )

    async def _upsert_record(
        self,
        entity_name: str,
        source_id: str,
        name: str | None,
        modifiedon: datetime | None,
        source_json: dict[str, Any],
        is_deleted: bool = False,
    ) -> None:
        """Upsert a single record into the sales_read_model database table."""
        # Sanitize name
        name_val = name.replace("\x00", "") if name else None

        await self._conn.execute(
            """
            INSERT INTO sales_read_model
                (namespace_id, entity, source_id, name, modifiedon, source_json, is_deleted, synced_at, updated_at)
            VALUES
                ($1::uuid, $2, $3, $4, $5::timestamptz, $6::jsonb, $7::boolean, now(), now())
            ON CONFLICT (namespace_id, entity, source_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                modifiedon = EXCLUDED.modifiedon,
                source_json = EXCLUDED.source_json,
                is_deleted = EXCLUDED.is_deleted,
                updated_at = now()
            """,
            str(self._ns),
            entity_name,
            source_id,
            name_val,
            modifiedon,
            json.dumps(source_json),
            is_deleted,
        )

    async def _reconcile_deletions(self, entity_name: str, entity_set: str, pk_field: str) -> int:
        """Reconcile deletions during a full sync by marking missing items as is_deleted = true."""
        active_ids = set()
        async for rec in self._client.paginate(
            entity_set, select=[pk_field], page_size=self._page_size
        ):
            source_id = rec.get(pk_field)
            if source_id:
                active_ids.add(str(source_id).lower())

        db_rows = await self._conn.fetch(
            "SELECT source_id FROM sales_read_model WHERE namespace_id = $1::uuid AND entity = $2 AND is_deleted = false",
            str(self._ns),
            entity_name,
        )
        db_ids = {row["source_id"].lower() for row in db_rows if row["source_id"]}

        missing_ids = list(db_ids - active_ids)
        if missing_ids:
            await self._conn.execute(
                """
                UPDATE sales_read_model
                SET is_deleted = true, updated_at = now()
                WHERE namespace_id = $1::uuid AND entity = $2 AND source_id = ANY($3::text[])
                """,
                str(self._ns),
                entity_name,
                missing_ids,
            )
            return len(missing_ids)
        return 0

    async def sync_entity(self, entity_name: str, incremental: bool = True) -> dict[str, Any]:
        """Sync a single Dataverse entity into the local database read-model."""
        entity_set = _ENTITY_SETS[entity_name]
        pk_field = _ENTITY_PK_FIELDS[entity_name]
        name_field = _ENTITY_NAME_FIELDS[entity_name]
        select = _SELECT_FIELDS[entity_name]

        watermark = await self._load_watermark(entity_name)
        upserted_count = 0
        deleted_count = 0
        method = "watermark"

        # Check if we can and should use change-tracking
        use_change_tracking = cfg.NCE_D365_CHANGE_TRACKING_ENABLED and incremental

        if use_change_tracking:
            delta_link = watermark if (watermark and watermark.startswith("http")) else None
            try:
                changed_records, removed_ids, new_delta = await self._client.track_changes(
                    entity_set, select=select, delta_link=delta_link, page_size=self._page_size
                )

                for rec in changed_records:
                    source_id = rec.get(pk_field)
                    if not source_id:
                        continue
                    name = rec.get(name_field) or source_id
                    mod_on = parse_datetime(rec.get("modifiedon"))
                    await self._upsert_record(
                        entity_name, source_id, name, mod_on, rec, is_deleted=False
                    )
                    upserted_count += 1

                if removed_ids:
                    await self._conn.execute(
                        """
                        UPDATE sales_read_model
                        SET is_deleted = true, updated_at = now()
                        WHERE namespace_id = $1::uuid AND entity = $2 AND source_id = ANY($3::text[])
                        """,
                        str(self._ns),
                        entity_name,
                        removed_ids,
                    )
                    deleted_count = len(removed_ids)

                if new_delta:
                    await self._save_watermark(entity_name, new_delta)

                return {
                    "entity": entity_name,
                    "upserted": upserted_count,
                    "deleted": deleted_count,
                    "method": "change_tracking",
                }
            except Exception as exc:
                log.warning(
                    "Change tracking sync failed for %s, falling back to watermark sync: %s",
                    entity_name,
                    exc,
                )

        # Fallback to Watermark-based sync
        watermark_ts = None
        if watermark and not watermark.startswith("http"):
            watermark_ts = watermark

        filter_expr = None
        if watermark_ts and incremental:
            ts = parse_datetime(watermark_ts)
            if ts:
                ts_skewed = ts - timedelta(seconds=CURSOR_OVERLAP_SECONDS)
                filter_expr = f"modifiedon gt {ts_skewed.strftime('%Y-%m-%dT%H:%M:%SZ')}"

        max_modified = None
        async for rec in self._client.paginate(
            entity_set, select=select, filter_expr=filter_expr, page_size=self._page_size
        ):
            source_id = rec.get(pk_field)
            if not source_id:
                continue
            name = rec.get(name_field) or source_id
            mod_on = parse_datetime(rec.get("modifiedon"))
            if mod_on:
                if max_modified is None or mod_on > max_modified:
                    max_modified = mod_on
            await self._upsert_record(entity_name, source_id, name, mod_on, rec, is_deleted=False)
            upserted_count += 1

        if max_modified:
            new_watermark = max_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
            await self._save_watermark(entity_name, new_watermark)

        if not incremental:
            deleted_count = await self._reconcile_deletions(entity_name, entity_set, pk_field)
            method = "full_reconcile"

        return {
            "entity": entity_name,
            "upserted": upserted_count,
            "deleted": deleted_count,
            "method": method,
        }

    async def run_incremental_sync(self, entity_types: list[str] | None = None) -> dict[str, Any]:
        """Perform incremental sync on requested entities."""
        targets = entity_types or list(_ENTITY_SETS.keys())
        results = []
        for entity in targets:
            if entity in _ENTITY_SETS:
                res = await self.sync_entity(entity, incremental=True)
                results.append(res)
        return {
            "namespace_id": str(self._ns),
            "mode": "incremental",
            "results": results,
        }

    async def run_full_sync(self, entity_types: list[str] | None = None) -> dict[str, Any]:
        """Perform full sync on requested entities, with delete reconciliation."""
        targets = entity_types or list(_ENTITY_SETS.keys())
        results = []
        for entity in targets:
            if entity in _ENTITY_SETS:
                res = await self.sync_entity(entity, incremental=False)
                results.append(res)
        return {
            "namespace_id": str(self._ns),
            "mode": "full",
            "results": results,
        }
