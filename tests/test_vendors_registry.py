"""Integration tests for the Vendors Registry vertical module (Batch 094)."""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from nce.auth import set_namespace_context
from nce.db_utils import scoped_mongo_session, scoped_pg_session
from nce.entity_resolution.ownership import OwnershipError
from nce.vertical_modules.vendors.registry import do_upsert_vendor


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(
        self, pg_pool: asyncpg.Pool, mongo_client: AsyncIOMotorClient | None = None
    ) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client


async def _seed_ownership(
    conn: asyncpg.Connection, ns_id: uuid.UUID, node_type: str, owner_engine: str
) -> None:
    """Seed the node ownership registry for tests."""
    await conn.execute(
        """
        INSERT INTO node_ownership_registry (namespace_id, node_type, owner_engine)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
        """,
        ns_id,
        node_type,
        owner_engine,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vendor_upsert_idempotency_and_field_merge(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Verify that upserting a vendor twice with the same orgnr merges fields and is idempotent."""
    ns_id = await make_namespace()

    # 1. Seed ownership for VENDOR owned by vendors
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await _seed_ownership(conn, ns_id, "VENDOR", "vendors")

    mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
    mongo_client = AsyncIOMotorClient(mongo_uri)
    engine = EngineStub(pg_pool, mongo_client)

    # Use orgnr/name containing 'vendor' and the orgnr to guarantee pg_trgm similarity >= 0.2
    # against the VENDOR:{ORGNR} label, since the C1 resolver averages key similarities.
    orgnr = "VENDOR-987654321"
    name1 = "Vendor 987654321 First"

    # First upsert (Feed source, sets some feed fields)
    params1 = {
        "namespace_id": ns_id,
        "orgnr": orgnr,
        "name": name1,
        "feed_fields": {"website": "https://acme.org", "phone": "123-456"},
        "source_id": "feed-source-01",
        "source_type": "feed",
    }

    res1 = await do_upsert_vendor(engine, params1)
    assert res1["ok"] is True
    assert res1["label"] == f"VENDOR:{orgnr.upper()}"
    payload_ref1 = res1["payload_ref"]
    assert payload_ref1 != "000000000000000000000000"

    # Second upsert (Admin source, updates name, sets admin fields and overrides phone)
    name2 = "Vendor 987654321 Second"
    params2 = {
        "namespace_id": ns_id,
        "orgnr": orgnr,
        "name": name2,
        "admin_fields": {"email": "admin@acme.org", "phone": "999-888"},
        "source_id": "admin-source-02",
        "source_type": "admin",
    }

    res2 = await do_upsert_vendor(engine, params2)
    assert res2["ok"] is True
    assert res2["label"] == f"VENDOR:{orgnr.upper()}"
    assert res2["payload_ref"] == payload_ref1  # ID / payload ref remains stable

    # 2. Verify PostgreSQL state (only one node should exist in this namespace)
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            "SELECT id, label, payload_ref, vendors_source_id FROM kg_nodes WHERE entity_type = 'VENDOR' AND namespace_id = $1",
            ns_id,
        )
        assert len(rows) == 1
        assert rows[0]["label"] == f"VENDOR:{orgnr.upper()}"
        assert rows[0]["payload_ref"] == payload_ref1
        # source_id is updated to the latest
        assert rows[0]["vendors_source_id"] == "admin-source-02"

    # 3. Verify MongoDB state (merged fields, admin wins on phone)
    async with scoped_mongo_session(mongo_client, ns_id) as db:
        doc = await db.episodes.find_one({"_id": ObjectId(payload_ref1)})
        assert doc is not None
        assert doc["orgnr"] == orgnr
        assert doc["name"] == name2
        assert doc["feed_fields"]["website"] == "https://acme.org"
        assert doc["admin_fields"]["email"] == "admin@acme.org"
        # Admin overrides feed phone number
        assert doc["merged_fields"]["phone"] == "999-888"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vendor_namespace_isolation(
    pg_pool: asyncpg.Pool,
    pg_app_conn: asyncpg.Connection,
    make_namespace: Any,
) -> None:
    """Verify that vendors upserted in namespace A are not visible or merged in namespace B."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()

    # Seed ownership in both namespaces using pg_pool (database owner role)
    async with scoped_pg_session(pg_pool, ns_a) as conn:
        await _seed_ownership(conn, ns_a, "VENDOR", "vendors")
    async with scoped_pg_session(pg_pool, ns_b) as conn:
        await _seed_ownership(conn, ns_b, "VENDOR", "vendors")

    mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
    mongo_client = AsyncIOMotorClient(mongo_uri)
    engine = EngineStub(pg_pool, mongo_client)

    orgnr = "VENDOR-ISOLATION-999"
    name = "Vendor Isolation 999"

    # Upsert in namespace A
    params_a = {
        "namespace_id": ns_a,
        "orgnr": orgnr,
        "name": name,
        "feed_fields": {"tag": "A"},
        "source_id": "source-a",
        "source_type": "feed",
    }
    res_a = await do_upsert_vendor(engine, params_a)
    assert res_a["ok"] is True

    # Upsert in namespace B (same orgnr, different name/fields)
    params_b = {
        "namespace_id": ns_b,
        "orgnr": orgnr,
        "name": name,
        "feed_fields": {"tag": "B"},
        "source_id": "source-b",
        "source_type": "feed",
    }
    res_b = await do_upsert_vendor(engine, params_b)
    assert res_b["ok"] is True

    # Ensure they have different Mongo documents (no leakage/merging across namespaces)
    assert res_a["payload_ref"] != res_b["payload_ref"]

    label = f"VENDOR:{orgnr.upper()}"

    # Verify namespace A can see namespace A's row but NOT namespace B's row using pg_app_conn (RLS)
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row_a = await pg_app_conn.fetchrow(
            "SELECT label, payload_ref FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            label,
            ns_a,
        )
    assert row_a is not None
    assert row_a["payload_ref"] == res_a["payload_ref"]

    # Verify namespace B can see namespace B's row but NOT namespace A's row using pg_app_conn (RLS)
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_b)
        row_b = await pg_app_conn.fetchrow(
            "SELECT label, payload_ref FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            label,
            ns_b,
        )
    assert row_b is not None
    assert row_b["payload_ref"] == res_b["payload_ref"]

    # Query B's row while GUC is set to ns_a should return None due to RLS
    async with pg_app_conn.transaction():
        await set_namespace_context(pg_app_conn, ns_a)
        row_b_hidden = await pg_app_conn.fetchrow(
            "SELECT label FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
            label,
            ns_b,
        )
    assert row_b_hidden is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vendor_ownership_enforcement(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify that do_upsert_vendor raises OwnershipError if 'vendors' is not the registered owner."""
    ns_id = await make_namespace()

    # Seed ownership assigning VENDOR to a different engine (e.g. 'crm')
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await _seed_ownership(conn, ns_id, "VENDOR", "crm")

    engine = EngineStub(pg_pool)
    params = {
        "namespace_id": ns_id,
        "orgnr": "123456789",
        "name": "Forbidden Vendor",
    }

    with pytest.raises(OwnershipError):
        await do_upsert_vendor(engine, params)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vendor_fallback_without_mongodb(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify do_upsert_vendor degrades gracefully to a default payload_ref when Mongo is unavailable."""
    ns_id = await make_namespace()

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await _seed_ownership(conn, ns_id, "VENDOR", "vendors")

    # Engine without mongo_client
    engine = EngineStub(pg_pool, mongo_client=None)
    orgnr = "no-mongo-org"
    params = {
        "namespace_id": ns_id,
        "orgnr": orgnr,
        "name": "Fallback Vendor",
    }

    res = await do_upsert_vendor(engine, params)
    assert res["ok"] is True
    # Should fall back to the 24-character zero hex string
    assert res["payload_ref"] == "000000000000000000000000"

    # Verify PostgreSQL node was still written
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, label, payload_ref FROM kg_nodes WHERE label = $1",
            f"VENDOR:{orgnr.upper()}",
        )
        assert row is not None
        assert row["payload_ref"] == "000000000000000000000000"
