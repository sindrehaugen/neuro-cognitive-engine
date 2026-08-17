"""Integration tests for certification modeling and watcher (Batch 101)."""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from nce.db_utils import scoped_mongo_session, scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.vendors.certs import do_check_cert_expiry, do_upsert_cert


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(
        self, pg_pool: asyncpg.Pool, mongo_client: AsyncIOMotorClient | None = None
    ) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cert_upsert_and_relationships(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify that do_upsert_cert creates the CERT node, has edge, and MongoDB payload."""
    ns_id = await make_namespace()

    # Seed ownership registry
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await seed_node_ownership_registry(conn, ns_id)

    mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
    mongo_client: AsyncIOMotorClient | None = AsyncIOMotorClient(mongo_uri)
    engine = EngineStub(pg_pool, mongo_client)

    contractor_id = "CONTRACTOR:TECH_JOHN"
    cert_name = "SAFETY_101"
    expiry_date_val = date.today() + timedelta(days=60)

    params = {
        "namespace_id": ns_id,
        "contractor_id": contractor_id,
        "cert_name": cert_name,
        "expiry_date": expiry_date_val,
        "name": "Safety Training 101",
    }

    res = await do_upsert_cert(engine, params)
    assert res["ok"] is True
    assert res["cert_label"] == f"CERT:TECH_JOHN:{cert_name}"
    assert res["contractor_label"] == contractor_id
    payload_ref = res["payload_ref"]
    assert payload_ref != "000000000000000000000000"

    # 1. Verify Postgres kg_nodes
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        node_row = await conn.fetchrow(
            "SELECT label, entity_type, payload_ref FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
            res["cert_label"],
            ns_id,
        )
        assert node_row is not None
        assert node_row["entity_type"] == "CERT"
        assert node_row["payload_ref"] == payload_ref

        # Verify Postgres kg_edges ( CONTRACTOR -[has]-> CERT )
        edge_row = await conn.fetchrow(
            "SELECT subject_label, predicate, object_label FROM kg_edges WHERE subject_label = $1 AND object_label = $2 AND namespace_id = $3::uuid",
            contractor_id,
            res["cert_label"],
            ns_id,
        )
        assert edge_row is not None
        assert edge_row["predicate"] == "has"

    # 2. Verify MongoDB payload
    async with scoped_mongo_session(mongo_client, ns_id) as db:
        doc = await db.episodes.find_one({"_id": ObjectId(payload_ref)})
        assert doc is not None
        assert doc["contractor_id"] == contractor_id
        assert doc["cert_name"] == cert_name
        assert doc["expiry_date"] == expiry_date_val.isoformat()
        assert doc["friendly_name"] == "Safety Training 101"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cert_watcher_expiry_and_idempotency(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Verify that the cert-expiry watcher detects expiring certs and publishes events idempotently."""
    ns_id = await make_namespace()

    # Seed ownership registry
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await seed_node_ownership_registry(conn, ns_id)

    mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
    mongo_client: AsyncIOMotorClient | None = AsyncIOMotorClient(mongo_uri)
    engine = EngineStub(pg_pool, mongo_client)

    today = date.today()
    expiring_date = today + timedelta(days=15)  # within 30-day default window
    valid_date = today + timedelta(days=45)  # outside 30-day default window

    # 1. Upsert expiring cert
    res1 = await do_upsert_cert(
        engine,
        {
            "namespace_id": ns_id,
            "contractor_id": "CONTRACTOR:ALICE",
            "cert_name": "ROOFING_T1",
            "expiry_date": expiring_date,
        },
    )
    assert res1["ok"] is True
    expiring_cert_label = res1["cert_label"]

    # 2. Upsert valid cert (non-expiring)
    res2 = await do_upsert_cert(
        engine,
        {
            "namespace_id": ns_id,
            "contractor_id": "CONTRACTOR:BOB",
            "cert_name": "ROOFING_T2",
            "expiry_date": valid_date,
        },
    )
    assert res2["ok"] is True

    # 3. Run the watcher
    res_watcher = await do_check_cert_expiry(
        engine,
        {
            "namespace_id": ns_id,
            "reference_date": today,
        },
    )

    assert res_watcher["checked"] == 2
    assert res_watcher["expiring"] == 1
    assert res_watcher["published"] == 1

    # 4. Verify C4 outbox event was published for the expiring cert
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        event_rows = await conn.fetch(
            "SELECT event_type, aggregate_id, payload FROM outbox_events WHERE namespace_id = $1::uuid AND event_type = 'cert.expiry'",
            ns_id,
        )
        assert len(event_rows) == 1
        assert event_rows[0]["aggregate_id"] == expiring_cert_label

    # 5. Run the watcher again — should not publish again (idempotency check)
    res_watcher2 = await do_check_cert_expiry(
        engine,
        {
            "namespace_id": ns_id,
            "reference_date": today,
        },
    )

    assert res_watcher2["checked"] == 2
    assert res_watcher2["expiring"] == 1
    assert res_watcher2["published"] == 0  # 0 new publications

    # Ensure still only 1 event row exists
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        event_rows_after = await conn.fetch(
            "SELECT id FROM outbox_events WHERE namespace_id = $1::uuid AND event_type = 'cert.expiry'",
            ns_id,
        )
        assert len(event_rows_after) == 1
