"""Integration tests for the C1 write-path ownership guard.

Verifies that ``assert_owner`` in ``nce.entity_resolution.ownership``:
  - refuses a write by a non-owner engine (raises ``OwnershipError``),
  - allows a write by the registered owner engine,
  - honours per-transition rows (transition-specific row beats node-type-wide),
  - applies deny-by-default when no registry row exists,
  - allows the registered owner for a transition-specific row.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError, assert_owner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_ownership(
    conn,
    namespace_id,
    *,
    node_type: str,
    owner_engine: str,
    transition: str | None = None,
) -> None:
    """Insert a row into node_ownership_registry under the given namespace."""
    if transition is not None:
        await conn.execute(
            """
            INSERT INTO node_ownership_registry (namespace_id, node_type, transition, owner_engine)
            VALUES ($1, $2, $3, $4)
            """,
            namespace_id,
            node_type,
            transition,
            owner_engine,
        )
    else:
        await conn.execute(
            """
            INSERT INTO node_ownership_registry (namespace_id, node_type, owner_engine)
            VALUES ($1, $2, $3)
            """,
            namespace_id,
            node_type,
            owner_engine,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_owner_is_refused(pg_pool, make_namespace) -> None:
    """A write by an engine that is NOT the registered owner raises OwnershipError."""
    ns = await make_namespace()
    node_type = f"device-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_ownership(conn, ns, node_type=node_type, owner_engine="engine-alpha")

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(conn, ns, node_type, "engine-beta")

    err = exc_info.value
    assert err.node_type == node_type
    assert err.writer_engine == "engine-beta"
    assert err.owner_engine == "engine-alpha"
    assert err.transition is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_owner_is_allowed(pg_pool, make_namespace) -> None:
    """A write by the registered owner engine succeeds without raising."""
    ns = await make_namespace()
    node_type = f"interface-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_ownership(conn, ns, node_type=node_type, owner_engine="engine-alpha")

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            # Must not raise.
            await assert_owner(conn, ns, node_type, "engine-alpha")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deny_by_default_when_no_row(pg_pool, make_namespace) -> None:
    """No registry row for a node type → write is denied regardless of engine."""
    ns = await make_namespace()
    node_type = f"prefix-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(conn, ns, node_type, "any-engine")

    err = exc_info.value
    assert err.owner_engine is None
    assert err.node_type == node_type


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transition_specific_row_overrides_wide_row(pg_pool, make_namespace) -> None:
    """A transition-specific row beats the node-type-wide row.

    Scenario:
      - engine-alpha owns 'vlan' for all transitions (node-type-wide row)
      - engine-beta owns 'vlan' for the 'update' transition (per-transition row)

    When writer_engine='engine-beta' requests transition='update', it should
    be allowed.  When the same engine requests transition='create' (no
    transition-specific row), the wide row applies → refused.
    """
    ns = await make_namespace()
    node_type = f"vlan-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            # Wide row: engine-alpha owns all transitions.
            await _seed_ownership(conn, ns, node_type=node_type, owner_engine="engine-alpha")
            # Transition-specific row: engine-beta owns 'update'.
            await _seed_ownership(
                conn,
                ns,
                node_type=node_type,
                owner_engine="engine-beta",
                transition="update",
            )

    # engine-beta + transition='update' → allowed (transition-specific row).
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await assert_owner(conn, ns, node_type, "engine-beta", transition="update")

    # engine-beta + transition='create' → refused (falls back to wide row = engine-alpha).
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(conn, ns, node_type, "engine-beta", transition="create")

    err = exc_info.value
    assert err.owner_engine == "engine-alpha"
    assert err.transition == "create"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transition_owner_allowed(pg_pool, make_namespace) -> None:
    """The engine registered for a specific transition is allowed for that transition."""
    ns = await make_namespace()
    node_type = f"route-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_ownership(
                conn,
                ns,
                node_type=node_type,
                owner_engine="engine-netops",
                transition="create",
            )

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            # Should not raise.
            await assert_owner(conn, ns, node_type, "engine-netops", transition="create")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transition_non_owner_refused(pg_pool, make_namespace) -> None:
    """An engine that is NOT the transition-specific owner is refused."""
    ns = await make_namespace()
    node_type = f"bgp-peer-{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_ownership(
                conn,
                ns,
                node_type=node_type,
                owner_engine="engine-netops",
                transition="create",
            )

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(conn, ns, node_type, "engine-crm", transition="create")

    err = exc_info.value
    assert err.owner_engine == "engine-netops"
    assert err.writer_engine == "engine-crm"
    assert err.transition == "create"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_namespace_isolation(pg_pool, make_namespace) -> None:
    """An ownership row in namespace A does not grant rights in namespace B."""
    ns_a = await make_namespace()
    ns_b = await make_namespace()
    node_type = f"switch-{uuid4().hex[:8]}"

    # Register engine-alpha as owner in namespace A only.
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_a)
            await _seed_ownership(conn, ns_a, node_type=node_type, owner_engine="engine-alpha")

    # In namespace B there is no row → deny-by-default.
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_b)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(conn, ns_b, node_type, "engine-alpha")

    assert exc_info.value.owner_engine is None
