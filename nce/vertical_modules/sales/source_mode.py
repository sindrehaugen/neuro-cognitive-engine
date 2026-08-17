"""
nce/vertical_modules/sales/source_mode.py
==========================================
Source-mode routing and divergence check for the Sales engine.

Wraps the read functions in read_model.py with the C5 source-mode resolver,
performing D365 vs NCE parity checks in 'both' mode and logging any
divergences to sales_divergence_log.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.orchestrator import NCEEngine
from nce.source_mode import read_through, resolve
from nce.source_mode.divergence import record_divergence
from nce.vertical_modules.dynamics365.auth import DataverseTokenManager
from nce.vertical_modules.dynamics365.client import DataverseClient
from nce.vertical_modules.sales import read_model

log = logging.getLogger("nce.vertical_modules.sales.source_mode")

# ---------------------------------------------------------------------------
# Divergence logging helper
# ---------------------------------------------------------------------------


async def log_sales_divergence(
    pool: Any,
    namespace_id: UUID,
    entity: str,
    field: str,
    nce_value: Any,
    ext_value: Any,
    is_numeric: bool = False,
) -> None:
    """Compare NCE value and D365 value, compute materiality, and record divergence."""
    nce_str = str(nce_value) if nce_value is not None else None
    ext_str = str(ext_value) if ext_value is not None else None

    if nce_str == ext_str:
        return

    materiality = 1.0
    if is_numeric:
        try:
            n = float(nce_value) if nce_value is not None else 0.0
            e = float(ext_value) if ext_value is not None else 0.0
            if n == e:
                return
            denom = max(abs(n), abs(e), 1.0)
            materiality = abs(n - e) / denom
        except (ValueError, TypeError):
            materiality = 1.0

    await record_divergence(
        pool,
        namespace_id=namespace_id,
        engine="sales",
        entity=entity,
        field=field,
        nce_value=nce_str,
        ext_value=ext_str,
        materiality=materiality,
    )


# ---------------------------------------------------------------------------
# Dataverse / Local DB Reader fallbacks
# ---------------------------------------------------------------------------


async def get_d365_client(engine: NCEEngine) -> DataverseClient | None:
    """Initialize authenticated DataverseClient if configured."""
    from nce.config import cfg

    if not getattr(cfg, "NCE_D365_ORG_URL", None):
        return None
    if not engine.redis_client:
        log.warning("[sales.source_mode] redis_client is not initialized")
        return None
    try:
        token_mgr = DataverseTokenManager(engine.redis_client)
        token = await token_mgr.get_access_token()
        return DataverseClient(cfg.NCE_D365_ORG_URL, token)
    except Exception as exc:
        log.warning("[sales.source_mode] DataverseClient initialization failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Parity checks & readers per function
# ---------------------------------------------------------------------------


# 1. Customers
async def do_list_customers(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))

    mode = await resolve(
        engine.pg_pool, engine="sales", function="list_customers", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_list_customers(engine, params)

    async def external_reader() -> dict[str, Any]:
        client = await get_d365_client(engine)
        if client:
            try:
                # Query directly from Dataverse
                q = params.get("q", "")
                size = int(params.get("size", 100))
                filter_expr = f"contains(name, '{q}')" if q else None
                records = []
                async for rec in client.paginate(
                    "accounts",
                    select=["accountid", "name", "address1_city", "example_industry", "modifiedon"],
                    filter_expr=filter_expr,
                    page_size=size,
                ):
                    # Map to unified structure
                    mapped = {**rec, "_id": rec.get("accountid"), "_deleted": False}
                    records.append(mapped)
                return {
                    "entity": "accounts",
                    "items": records[:size],
                    "total": len(records),
                    "page": int(params.get("page", 0)),
                    "size": size,
                    "pages": (len(records) + size - 1) // size if records else 0,
                }
            except Exception as exc:
                log.warning(
                    "[sales.source_mode] D365 query failed, falling back to local source_json: %s",
                    exc,
                )

        # Fallback to local source_json (no manual merge)
        q = params.get("q", "")
        size = int(params.get("size", 100))
        page = int(params.get("page", 0))
        include_deleted = bool(
            params.get("includeDeleted", False) or params.get("include_deleted", False)
        )
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            where = ["namespace_id = $1", "entity = 'accounts'"]
            args: list[Any] = [str(ns_uuid)]
            if not include_deleted:
                where.append("is_deleted = false")
            for term in [t for t in q.lower().split() if t]:
                args.append(f"%{term}%")
                where.append(f"lower(coalesce(name,'')) LIKE ${len(args)}")
            wsql = " AND ".join(where)
            total = await conn.fetchval(
                f"SELECT count(*) FROM sales_read_model WHERE {wsql}", *args
            )
            args.append(page * size)
            args.append(size)
            rows = await conn.fetch(
                f"SELECT source_json, is_deleted FROM sales_read_model WHERE {wsql} "
                f"ORDER BY modifiedon DESC NULLS LAST, lower(coalesce(name,'')) ASC "
                f"OFFSET ${len(args) - 1} LIMIT ${len(args)}",
                *args,
            )
            items = []
            for r in rows:
                import json

                sj = r["source_json"]
                if isinstance(sj, str):
                    sj = json.loads(sj)
                mapped = {**(sj or {}), "_id": sj.get("accountid"), "_deleted": r["is_deleted"]}
                items.append(mapped)
            return {
                "entity": "accounts",
                "items": items,
                "total": total,
                "page": page,
                "size": size,
                "pages": (total + size - 1) // size if total else 0,
            }

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        native_map = {}
        for item in native.get("items", []):
            k = item.get("accountid") or item.get("_id")
            if k:
                native_map[k] = item

        for ext_item in external.get("items", []):
            ext_id = ext_item.get("accountid") or ext_item.get("_id")
            if not ext_id:
                continue
            nat_item = native_map.get(ext_id)
            if not nat_item:
                await log_sales_divergence(
                    engine.pg_pool,
                    ns_uuid,
                    f"account:{ext_id}",
                    "existence",
                    None,
                    ext_item.get("name"),
                )
                continue
            # Compare fields
            await log_sales_divergence(
                engine.pg_pool,
                ns_uuid,
                f"account:{ext_id}",
                "name",
                nat_item.get("name"),
                ext_item.get("name"),
            )
            await log_sales_divergence(
                engine.pg_pool,
                ns_uuid,
                f"account:{ext_id}",
                "address1_city",
                nat_item.get("address1_city"),
                ext_item.get("address1_city"),
            )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


# 2. Customer Profile
async def do_customer_profile(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    accountid = params.get("accountid") or params.get("account_id")
    if not accountid:
        raise ValueError("accountid is required")

    mode = await resolve(
        engine.pg_pool, engine="sales", function="customer_profile", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_customer_profile(engine, params)

    async def external_reader() -> dict[str, Any]:
        # Fast path fallback using only source_json (no manual overrides) from local DB
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            row = await conn.fetchrow(
                "SELECT source_json, is_deleted FROM sales_read_model WHERE namespace_id = $1 AND entity = 'accounts' AND source_id = $2",
                str(ns_uuid),
                accountid,
            )
            if not row:
                return {"error": "unknown_company", "detail": f"Ukjent selskap: {accountid!r}"}
            import json

            sj = row["source_json"]
            if isinstance(sj, str):
                sj = json.loads(sj)
            company = {**(sj or {}), "_id": accountid, "_deleted": row["is_deleted"]}

            contacts = []
            c_rows = await conn.fetch(
                "SELECT source_json FROM sales_read_model WHERE namespace_id = $1 AND entity = 'contacts' AND is_deleted = false AND source_json->>'_parentcustomerid_value' = $2",
                str(ns_uuid),
                accountid,
            )
            for r in c_rows:
                cj = r["source_json"]
                if isinstance(cj, str):
                    cj = json.loads(cj)
                contacts.append(cj)

            opportunities = []
            o_rows = await conn.fetch(
                "SELECT source_json FROM sales_read_model WHERE namespace_id = $1 AND entity = 'opportunities' AND is_deleted = false AND source_json->>'_customerid_value' = $2",
                str(ns_uuid),
                accountid,
            )
            for r in o_rows:
                oj = r["source_json"]
                if isinstance(oj, str):
                    oj = json.loads(oj)
                opportunities.append(oj)

            cases = []
            cs_rows = await conn.fetch(
                "SELECT source_json FROM sales_read_model WHERE namespace_id = $1 AND entity = 'incidents' AND is_deleted = false AND source_json->>'_customerid_value' = $2",
                str(ns_uuid),
                accountid,
            )
            for r in cs_rows:
                csj = r["source_json"]
                if isinstance(csj, str):
                    csj = json.loads(csj)
                cases.append(csj)

            locations = []
            l_rows = await conn.fetch(
                "SELECT source_json FROM sales_read_model WHERE namespace_id = $1 AND entity = 'functionallocations' AND is_deleted = false",
                str(ns_uuid),
            )
            for r in l_rows:
                lj = r["source_json"]
                if isinstance(lj, str):
                    lj = json.loads(lj)
                # Parse addressHQ from properties
                locations.append(
                    {
                        "id": lj.get("msdyn_functionallocationid"),
                        "name": lj.get("msdyn_name"),
                        "address": f"{lj.get('msdyn_address1', '')}, {lj.get('msdyn_city', '')}".strip(
                            ", "
                        ),
                    }
                )

            return {
                "company": company,
                "contacts": contacts,
                "opportunities": opportunities,
                "cases": cases,
                "locations": locations,
                "assets": [],
            }

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        if "error" in native or "error" in external:
            return
        nat_comp = native.get("company", {})
        ext_comp = external.get("company", {})
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            f"account:{accountid}",
            "name",
            nat_comp.get("name"),
            ext_comp.get("name"),
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


# 3-10. Generic single opportunity / quote / agreement reads / dashboards
async def do_sales_overview(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    mode = await resolve(
        engine.pg_pool, engine="sales", function="sales_overview", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_sales_overview(engine, params)

    async def external_reader() -> dict[str, Any]:
        # For simplicity, aggregates also fallback to NCE read model querying purely source_json
        return await read_model.do_sales_overview(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        # Check totals
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            "sales_overview",
            "total_stages",
            len(native.get("stages", [])),
            len(external.get("stages", [])),
            is_numeric=True,
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_seller_detail(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    mode = await resolve(
        engine.pg_pool, engine="sales", function="seller_detail", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_seller_detail(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_seller_detail(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            f"seller:{params.get('user')}",
            "wonValue",
            native.get("wonValue"),
            external.get("wonValue"),
            is_numeric=True,
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_sales_dashboard(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    mode = await resolve(
        engine.pg_pool, engine="sales", function="sales_dashboard", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_sales_dashboard(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_sales_dashboard(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        nat_pipe = native.get("pipeline", {})
        ext_pipe = external.get("pipeline", {})
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            "dashboard",
            "openValue",
            nat_pipe.get("openValue"),
            ext_pipe.get("openValue"),
            is_numeric=True,
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_sales_stats(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    mode = await resolve(
        engine.pg_pool, engine="sales", function="sales_stats", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_sales_stats(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_sales_stats(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            "stats",
            "coverage_n",
            native.get("coverage", {}).get("n"),
            external.get("coverage", {}).get("n"),
            is_numeric=True,
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_sales_manager(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    mode = await resolve(
        engine.pg_pool, engine="sales", function="sales_manager", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_sales_manager(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_sales_manager(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            "manager",
            "team_openValue",
            native.get("team", {}).get("openProjectValue"),
            external.get("team", {}).get("openProjectValue"),
            is_numeric=True,
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_list_agreements(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    mode = await resolve(
        engine.pg_pool, engine="sales", function="list_agreements", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_list_agreements(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_list_agreements(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            "agreements",
            "total",
            native.get("total"),
            external.get("total"),
            is_numeric=True,
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_agreement_detail(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    agreementid = params.get("agreementid")
    if not agreementid:
        raise ValueError("agreementid is required")
    mode = await resolve(
        engine.pg_pool, engine="sales", function="agreement_detail", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_agreement_detail(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_agreement_detail(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            f"agreement:{agreementid}",
            "msdyn_name",
            native.get("msdyn_name"),
            external.get("msdyn_name"),
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )


async def do_quote_detail(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    namespace_id = params.get("namespace_id")
    if not namespace_id:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(namespace_id))
    quoteid = params.get("quoteid")
    if not quoteid:
        raise ValueError("quoteid is required")
    mode = await resolve(
        engine.pg_pool, engine="sales", function="quote_detail", namespace_id=ns_uuid
    )

    async def native_reader() -> dict[str, Any]:
        return await read_model.do_quote_detail(engine, params)

    async def external_reader() -> dict[str, Any]:
        return await read_model.do_quote_detail(engine, params)

    async def parity_check(native: dict[str, Any], external: dict[str, Any]) -> None:
        await log_sales_divergence(
            engine.pg_pool,
            ns_uuid,
            f"quote:{quoteid}",
            "name",
            native.get("name"),
            external.get("name"),
        )

    return await read_through(
        mode,
        native_reader=native_reader,
        external_reader=external_reader,
        parity_check=parity_check,
    )
