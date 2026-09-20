"""
tests/integration/test_sales_contact_resource_surface_live.py
=================================================================
Live-Postgres verification of Wave B-2: the CONTACT C12 resource
declaration (nce/vertical_modules/sales/resources.py, CONTACT_SPEC) --
kg_nodes-primary identity plus a new ``sales_contacts`` satellite table
(migration 097), following the DEVICE/PORT/RACK/CABLE multi-table pattern
rather than CUSTOMER/LEAD/DEAL/QUOTE's single-relational-table shape.

Two things this spec deliberately does NOT do, each proven here rather than
just asserted in the docstring:
  1. Bulk create stays refused (test_bulk_refused_with_specific_reason) --
     the dispatch's "5 routes including bulk" describes the HOST's
     inventory, not an NCE requirement; identity-plus-satellite partial-
     failure semantics have no precedent in this generator.
  2. Create does not perform entity resolution / deduplication
     (test_create_does_not_deduplicate_even_with_matching_email_and_phone)
     -- resolve() is read-only and never auto-merges (its own docstring,
     nce/entity_resolution/resolver.py:102), so "C1-resolved on email+phone"
     describes a property of the node type, not a create-path behaviour.

Also proves the explicit-empty tier_allowlists actually redacts email/phone
for external-customer/contractor callers on a live GET -- the concrete
reason CONTACT declares tier_allowlists at all: a contact's email/phone is
PII an external principal must not see by default.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.auth import set_namespace_context
from nce.engine_registry import populate_engine_modules
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.entity_resolution.resolver import resolve
from nce.orchestrator import NCEEngine
from nce.resource_surface.mcp import build_mcp_tool_specs
from nce.resource_surface.rest import make_resource_routes
from nce.vertical_modules.sales.resources import CONTACT_SPEC

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)
    return eng


def _rest_client(engine: NCEEngine) -> httpx.AsyncClient:
    admin_state.engine = engine
    app = Starlette(routes=make_resource_routes(CONTACT_SPEC))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_rest_create_then_get(engine: NCEEngine, namespace_id: uuid.UUID) -> None:
    node_label = f"probe-contact-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine) as client:
        r1 = await client.post(
            "/api/sales/contacts",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "name": "Kari Nordmann",
                "email": "kari@example.com",
                "phone": "+4791234567",
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.get(
            f"/api/sales/contacts/{node_label}",
            params={"namespace_id": str(namespace_id)},
        )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["entity_type"] == "CONTACT"
    assert body["name"] == "Kari Nordmann"
    assert body["email"] == "kari@example.com"
    assert body["phone"] == "+4791234567"

    async with engine.pg_pool.acquire() as conn:
        kg_row = await conn.fetchrow(
            "SELECT entity_type, change_origin FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            node_label,
        )
        sat_row = await conn.fetchrow(
            "SELECT name, email, phone FROM sales_contacts WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert kg_row is not None and kg_row["entity_type"] == "CONTACT"
    assert sat_row is not None
    assert sat_row["email"] == "kari@example.com"


@pytest.mark.asyncio
async def test_mcp_upsert_then_patch_change_origin(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    tools = build_mcp_tool_specs(CONTACT_SPEC)
    node_label = f"probe-contact-mcp-{uuid.uuid4().hex[:8]}"

    upsert = tools[f"sales_upsert_{CONTACT_SPEC.mcp_slug}"]
    result = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "name": "Ola Hansen",
                "email": "ola@example.com",
                "phone": "+4798765432",
            },
        )
    )
    assert result["status"] == "ok", result

    result2 = json.loads(
        await upsert.handler(
            engine,
            {
                "namespace_id": str(namespace_id),
                "id": node_label,
                "change_origin": "operator",
            },
        )
    )
    assert result2["status"] == "ok", result2

    async with engine.pg_pool.acquire() as conn:
        change_origin = await conn.fetchval(
            "SELECT change_origin FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
            namespace_id,
            node_label,
        )
        name = await conn.fetchval(
            "SELECT name FROM sales_contacts WHERE namespace_id = $1 AND node_label = $2",
            namespace_id,
            node_label,
        )
    assert change_origin == "operator"
    assert name == "Ola Hansen", "the second upsert must not have wiped the first's satellite row"


@pytest.mark.asyncio
async def test_create_refused_without_ownership_seed(pg_pool: asyncpg.Pool) -> None:
    """Deny-by-default proof for CONTACT specifically: an un-seeded
    namespace refuses the write rather than silently succeeding.
    """
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    populate_engine_modules(eng)

    async with pg_pool.acquire() as conn:
        fresh_ns = await conn.fetchval(
            "INSERT INTO namespaces (slug) VALUES ($1) RETURNING id",
            f"pytest-contact-unseeded-{uuid.uuid4().hex[:8]}",
        )
    try:
        async with _rest_client(eng) as client:
            r = await client.post(
                "/api/sales/contacts",
                json={"namespace_id": str(fresh_ns), "node_label": "should-be-refused"},
            )
        assert r.status_code == 403, r.text
        assert r.json()["reason"] == "ownership_denied"
        async with pg_pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT 1 FROM kg_nodes WHERE namespace_id = $1 AND label = $2",
                fresh_ns,
                "should-be-refused",
            )
        assert row is None, "refused write must not have reached kg_nodes"
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM namespaces WHERE id = $1", fresh_ns)


@pytest.mark.asyncio
async def test_bulk_refused_with_specific_reason(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The dispatch named 'bulk' as one of the host's 5 contact routes --
    this spec ships the other 4 and refuses bulk explicitly, with a reason
    naming the actual open question (partial-failure semantics), not a
    generic 'not supported yet'.
    """
    async with _rest_client(engine) as client:
        r = await client.post(
            "/api/sales/contacts/bulk",
            json={"namespace_id": str(namespace_id), "items": [{"node_label": "x"}]},
        )
    assert r.status_code == 501
    assert "partial-failure" in r.text, r.text


@pytest.mark.asyncio
async def test_archive_refused_no_soft_delete_column(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    async with _rest_client(engine) as client:
        r = await client.post(
            "/api/sales/contacts/some-label/archive",
            json={"namespace_id": str(namespace_id)},
        )
    assert r.status_code == 501
    assert "soft-delete" in r.text, r.text


@pytest.mark.asyncio
async def test_create_does_not_deduplicate_even_with_matching_email_and_phone(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    """The load-bearing proof behind 'C1-resolvable, not C1-resolved-on-
    write': creating a second contact with the SAME email+phone as an
    existing one must succeed as a second, distinct kg_nodes row -- the
    generic create path performs no resolve()/dedup step. resolve() itself
    (called here directly, exactly as a future caller who wants a duplicate
    check would) still finds the match; it just never runs automatically
    and never merges, proving resolve()'s own contract rather than trusting
    its docstring.
    """
    email = f"dup-{uuid.uuid4().hex[:6]}@example.com"
    phone = "+4790000001"

    async with _rest_client(engine) as client:
        r1 = await client.post(
            "/api/sales/contacts",
            json={
                "namespace_id": str(namespace_id),
                "node_label": f"probe-contact-dup-1-{uuid.uuid4().hex[:8]}",
                "name": "First Contact",
                "email": email,
                "phone": phone,
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = await client.post(
            "/api/sales/contacts",
            json={
                "namespace_id": str(namespace_id),
                "node_label": f"probe-contact-dup-2-{uuid.uuid4().hex[:8]}",
                "name": "Second Contact",
                "email": email,
                "phone": phone,
            },
        )
    assert r2.status_code == 201, (
        "create must not refuse or merge a duplicate -- generic create performs no resolution"
    )

    async with engine.pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM sales_contacts WHERE namespace_id = $1 AND email = $2",
            namespace_id,
            email,
        )
    assert count == 2, "both rows must exist independently -- no auto-merge happened"

    # A caller who DOES want a duplicate check runs resolve() itself, against
    # kg_nodes -- proving the primitive this spec deliberately does not wire
    # into create still works exactly as documented (rank-and-score only).
    async with engine.pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            matches = await resolve(
                conn,
                namespace_id=namespace_id,
                candidate={"email": email, "phone": phone},
                keys=["email", "phone"],
                node_type="CONTACT",
            )
    assert len(matches) >= 1, "resolve() should still find the pre-existing candidates"


@pytest.mark.asyncio
async def test_tier_redaction_strips_pii_for_external_and_contractor(
    engine: NCEEngine, namespace_id: uuid.UUID
) -> None:
    node_label = f"probe-contact-tier-{uuid.uuid4().hex[:8]}"
    async with _rest_client(engine) as client:
        r1 = await client.post(
            "/api/sales/contacts",
            json={
                "namespace_id": str(namespace_id),
                "node_label": node_label,
                "name": "Redacted Person",
                "email": "redact@example.com",
                "phone": "+4799999999",
            },
        )
        assert r1.status_code == 201, r1.text

        emp = await client.get(
            f"/api/sales/contacts/{node_label}",
            params={"namespace_id": str(namespace_id)},
            headers={"X-NCE-Principal-Tier": "employee"},
        )
        con = await client.get(
            f"/api/sales/contacts/{node_label}",
            params={"namespace_id": str(namespace_id)},
            headers={"X-NCE-Principal-Tier": "contractor"},
        )
        ext = await client.get(
            f"/api/sales/contacts/{node_label}",
            params={"namespace_id": str(namespace_id)},
            headers={"X-NCE-Principal-Tier": "external-customer"},
        )

    assert emp.status_code == 200
    assert emp.json()["email"] == "redact@example.com"

    for resp in (con, ext):
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "email" not in body, body
        assert "phone" not in body, body
        assert "name" not in body, body
