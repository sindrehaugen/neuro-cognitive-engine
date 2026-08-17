"""Cross-cutting boundary tests for the NetBox vertical module.

Covers the audit domain-5 surfaces:
  * GraphQL topology parsing → undirected adjacency matrix (graphql_activation.parse_topology)
  * Spiking-activation pre-fetch with RLS namespace enforcement under high concurrency
  * Branching API staging (discovery) — header propagation, schema gating, partial-failure semantics
  * Do-calculus circuit escalation (circuits) — threshold gating, ticket shape, stateless concurrency
  * MTBF forecast synthesis (mtbf) — anomaly aggregation, hardware-age wear-out, RLS context
  * set_namespace_context transaction-scoped set_config contract (plugin views.py parity)

All NetBox HTTP traffic is served by httpx.MockTransport; Postgres is replaced by a
recording FakeConn so RLS context-binding can be asserted without a live database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jsonschema.exceptions import ValidationError

import nce.vertical_modules.netbox.circuits as circuits_mod
from nce.auth import set_namespace_context
from nce.graph_query import SpikingActivationEngine
from nce.vertical_modules.netbox.circuits import (
    NetBoxCircuitEscalator,
    NetBoxCircuitsClient,
)
from nce.vertical_modules.netbox.discovery import NetBoxDiscoveryReconciler
from nce.vertical_modules.netbox.graphql_activation import (
    GraphQLSpikingActivator,
    NetBoxGraphQLClient,
    parse_topology,
)
from nce.vertical_modules.netbox.mtbf import NetBoxMTBFForecaster

NETBOX_URL = "http://netbox.test"
TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConn:
    """Recording asyncpg.Connection stand-in.

    Routes by SQL shape: EXISTS probes → ``authorized``; kg_nodes/topology_graph
    label filters → intersection with ``allowed_labels``; event_log scans →
    ``event_rows``. Every call is recorded for RLS-context assertions.
    """

    def __init__(
        self,
        *,
        authorized: bool = True,
        allowed_labels: set[str] | None = None,
        event_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.authorized = authorized
        self.allowed_labels = allowed_labels if allowed_labels is not None else set()
        self.event_rows = event_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "SELECT 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self.authorized

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if "FROM event_log" in sql:
            return self.event_rows
        if "FROM kg_nodes" in sql:
            candidate = set(args[1]) if len(args) > 1 else set()
            return [{"label": lab} for lab in sorted(self.allowed_labels & candidate)]
        return []

    def namespace_context_values(self) -> list[str]:
        """All namespace ids bound via set_config during this connection's life."""
        return [
            str(args[0])
            for sql, args in self.calls
            if "set_config('nce.namespace_id'" in sql and args
        ]


def _cable(a_dev: str, a_int: str, b_dev: str, b_int: str, status: str = "CONNECTED") -> dict:
    return {
        "id": f"cable-{a_dev}-{b_dev}",
        "status": status,
        "a_terminations": [
            {"id": "t1", "name": a_int, "device": {"id": "x", "name": a_dev}},
        ],
        "b_terminations": [
            {"id": "t2", "name": b_int, "device": {"id": "y", "name": b_dev}},
        ],
    }


def _topology_response(cable_status: str = "CONNECTED") -> dict:
    cable = _cable("core-sw-1", "eth0", "edge-rtr-2", "eth4", status=cable_status)
    return {
        "data": {
            "site_list": [
                {
                    "id": "s1",
                    "name": "oslo-dc",
                    "slug": "oslo-dc",
                    "racks": [
                        {
                            "id": "r1",
                            "name": "rack-a1",
                            "devices": [
                                {
                                    "id": "d1",
                                    "name": "core-sw-1",
                                    "interfaces": [
                                        {"id": "i1", "name": "eth0", "cable": cable},
                                    ],
                                },
                            ],
                        },
                    ],
                    "devices": [
                        {
                            "id": "d2",
                            "name": "edge-rtr-2",
                            "rack": {"id": "r1", "name": "rack-a1"},
                            "interfaces": [
                                {"id": "i2", "name": "eth4", "cable": cable},
                            ],
                        },
                    ],
                },
            ],
        },
    }


def _graphql_client(payload: dict) -> tuple[NetBoxGraphQLClient, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NetBoxGraphQLClient(NETBOX_URL, TOKEN, client=http_client), http_client


# ---------------------------------------------------------------------------
# parse_topology — polymorphic GraphQL → undirected adjacency matrix
# ---------------------------------------------------------------------------


def test_parse_topology_builds_undirected_deduplicated_adjacency() -> None:
    adj = parse_topology(_topology_response())

    # Undirected: every edge appears in both endpoints' adjacency lists.
    for node, neighbours in adj.items():
        for other, _w in neighbours:
            assert any(back == node for back, _ in adj[other]), (
                f"edge {node}->{other} missing reverse direction"
            )

    # The cable edge between interface labels exists exactly once per endpoint
    # even though both the rack path and the site-device path visit it.
    iface_neighbours = [n for n, _ in adj["core-sw-1:eth0"]]
    assert iface_neighbours.count("edge-rtr-2:eth4") == 1

    # No duplicate neighbour entries anywhere (sorted-tuple dedup invariant).
    for node, neighbours in adj.items():
        labels = [n for n, _ in neighbours]
        assert len(labels) == len(set(labels)), f"duplicate edges on {node}"

    # Hierarchy edges present: site—rack, rack—device, device—interface.
    assert ("rack-a1", 1.0) in adj["oslo-dc"]
    assert ("core-sw-1", 1.0) in adj["rack-a1"]
    assert ("core-sw-1:eth0", 1.0) in adj["core-sw-1"]


def test_parse_topology_failed_cable_gets_zero_weight() -> None:
    adj = parse_topology(_topology_response(cable_status="FAILED"))
    weights = {n: w for n, w in adj["core-sw-1:eth0"]}
    assert weights["edge-rtr-2:eth4"] == 0.0


def test_spiking_engine_clamps_potential_at_max_charge() -> None:
    engine = SpikingActivationEngine(theta=0.1, decay=1.0, alpha=5.0, max_charge=10.0)
    engine.set_potentials({"a": 5.0})
    engine.step({"a": [("b", 1.0)], "b": [("a", 1.0)]})
    assert engine.potentials["b"] <= 10.0
    assert engine.max_potentials["b"] <= 10.0


# ---------------------------------------------------------------------------
# GraphQLSpikingActivator — RLS enforcement and concurrency isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_fetch_context_default_denies_without_connection() -> None:
    client, http_client = _graphql_client(_topology_response())
    activator = GraphQLSpikingActivator(client)
    try:
        result = await activator.pre_fetch_context(
            anchor_label="core-sw-1",
            telemetry_severity=1.0,
            conn=None,
            namespace_id=uuid.uuid4(),
        )
    finally:
        await http_client.aclose()
    assert result == set()


@pytest.mark.asyncio
async def test_pre_fetch_context_denies_unauthorized_anchor_before_netbox_fetch() -> None:
    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, json=_topology_response())

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    activator = GraphQLSpikingActivator(NetBoxGraphQLClient(NETBOX_URL, TOKEN, client=http_client))
    conn = FakeConn(authorized=False)
    try:
        result = await activator.pre_fetch_context(
            anchor_label="not-our-device",
            telemetry_severity=1.0,
            conn=conn,
            namespace_id=uuid.uuid4(),
        )
    finally:
        await http_client.aclose()

    assert result == set()
    # Zero-trust ordering: the GraphQL fetch must never run for a denied anchor.
    assert fetch_count == 0


@pytest.mark.asyncio
async def test_pre_fetch_context_output_is_intersected_with_namespace_labels() -> None:
    client, http_client = _graphql_client(_topology_response())
    activator = GraphQLSpikingActivator(client)
    ns = uuid.uuid4()
    allowed = {"core-sw-1", "rack-a1"}
    conn = FakeConn(authorized=True, allowed_labels=allowed)
    try:
        result = await activator.pre_fetch_context(
            anchor_label="core-sw-1",
            telemetry_severity=1.0,  # below spike threshold: explicit params apply
            conn=conn,
            namespace_id=ns,
            theta=0.5,
            decay=0.85,
            alpha=1.0,
            ticks=2,
        )
    finally:
        await http_client.aclose()

    # Activation spreads well beyond the allowlist, but the RLS post-filter
    # must clamp the output to labels owned by the namespace.
    assert result, "anchor should activate at least itself"
    assert result <= allowed
    assert conn.namespace_context_values() == [str(ns)]


@pytest.mark.asyncio
async def test_pre_fetch_context_high_concurrency_has_no_tenant_bleed() -> None:
    """25 concurrent activations across two tenants must never cross-leak labels."""
    client, http_client = _graphql_client(_topology_response())
    activator = GraphQLSpikingActivator(client)

    ns_a, ns_b = uuid.uuid4(), uuid.uuid4()
    allowed_a = {"core-sw-1", "rack-a1", "oslo-dc"}
    allowed_b = {"edge-rtr-2", "edge-rtr-2:eth4"}

    async def run_one(ns: uuid.UUID, allowed: set[str], anchor: str) -> tuple[set[str], FakeConn]:
        conn = FakeConn(authorized=True, allowed_labels=allowed)
        result = await activator.pre_fetch_context(
            anchor_label=anchor,
            telemetry_severity=1.0,
            conn=conn,
            namespace_id=ns,
            theta=0.5,
            decay=0.85,
            alpha=1.0,
            ticks=3,
        )
        return result, conn

    tasks = []
    for i in range(25):
        if i % 2 == 0:
            tasks.append(run_one(ns_a, allowed_a, "core-sw-1"))
        else:
            tasks.append(run_one(ns_b, allowed_b, "edge-rtr-2"))

    try:
        outcomes = await asyncio.gather(*tasks)
    finally:
        await http_client.aclose()

    for i, (result, conn) in enumerate(outcomes):
        expected_ns = ns_a if i % 2 == 0 else ns_b
        expected_allowed = allowed_a if i % 2 == 0 else allowed_b
        assert result <= expected_allowed, f"task {i}: tenant bleed {result - expected_allowed}"
        assert conn.namespace_context_values() == [str(expected_ns)]


# ---------------------------------------------------------------------------
# set_namespace_context — transaction-scoped set_config contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_namespace_context_uses_transaction_scoped_set_config() -> None:
    """Mirrors src/nce-netbox-plugin views.py: third arg MUST be true (SET LOCAL).

    A regression to session-scoped set_config would leak the tenant binding
    across pooled connections.
    """
    conn = FakeConn()
    ns = uuid.uuid4()
    await set_namespace_context(conn, ns)

    sql, args = conn.calls[0]
    assert "set_config('nce.namespace_id', $1, true)" in sql
    assert args == (str(ns),)


# ---------------------------------------------------------------------------
# Discovery — Branching API staging boundaries
# ---------------------------------------------------------------------------


def _reconciler_with_transport(
    handler: Any,
) -> tuple[NetBoxDiscoveryReconciler, httpx.AsyncClient, httpx.AsyncClient]:
    gql_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rest_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gql_client = NetBoxGraphQLClient(NETBOX_URL, TOKEN, client=gql_http)
    return (
        NetBoxDiscoveryReconciler(gql_client, rest_client=rest_http),
        gql_http,
        rest_http,
    )


@pytest.mark.asyncio
async def test_get_or_create_staging_branch_reuses_existing_branch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"results": [{"name": "nce-staged-discovery", "schema_id": "br_existing"}]},
        )

    reconciler, gql_http, rest_http = _reconciler_with_transport(handler)
    try:
        schema_id = await reconciler.get_or_create_staging_branch()
    finally:
        await gql_http.aclose()
        await rest_http.aclose()

    assert schema_id == "br_existing"
    assert len(requests) == 1  # no create POST when the branch already exists


@pytest.mark.asyncio
async def test_get_or_create_staging_branch_creates_when_missing() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"results": []})
        import json as _json

        posted.append(_json.loads(request.content))
        return httpx.Response(201, json={"schema_id": "br_new"})

    reconciler, gql_http, rest_http = _reconciler_with_transport(handler)
    try:
        schema_id = await reconciler.get_or_create_staging_branch("nce-staged-discovery")
    finally:
        await gql_http.aclose()
        await rest_http.aclose()

    assert schema_id == "br_new"
    assert posted == [{"name": "nce-staged-discovery", "status": "new"}]


@pytest.mark.asyncio
async def test_stage_discovery_sends_branch_header_on_every_write() -> None:
    writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        writes.append(request)
        return httpx.Response(201, json={"id": len(writes)})

    reconciler, gql_http, rest_http = _reconciler_with_transport(handler)
    assets = {
        "devices": [
            {"name": "new-leaf-9", "device_type": 1, "role": 1, "site": 1},
        ],
        "interfaces": [
            {"device": "new-leaf-9", "name": "eth0", "type": "1000base-t"},
        ],
        "cables": [
            {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": "1"}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": "2"}],
                "status": "connected",
            },
        ],
    }
    try:
        proposals = await reconciler.stage_discovery("br_test", assets)
    finally:
        await gql_http.aclose()
        await rest_http.aclose()

    assert len(writes) == 3
    for req in writes:
        assert req.headers["X-NetBox-Branch"] == "br_test"
    assert [p["status"] for p in proposals] == ["staged", "staged", "staged"]
    assert {p["object_type"] for p in proposals} == {"device", "interface", "cable"}


@pytest.mark.asyncio
async def test_stage_discovery_schema_gate_blocks_invalid_payload_before_any_write() -> None:
    writes: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        writes.append(request)
        return httpx.Response(201, json={"id": 1})

    reconciler, gql_http, rest_http = _reconciler_with_transport(handler)
    bad_assets = {"devices": [{"name": "missing-required-fields"}]}
    try:
        with pytest.raises(ValidationError):
            await reconciler.stage_discovery("br_test", bad_assets)
    finally:
        await gql_http.aclose()
        await rest_http.aclose()

    assert writes == []  # validation must reject before the branch sees anything


@pytest.mark.asyncio
async def test_stage_discovery_partial_failure_leaves_prior_objects_staged() -> None:
    """Documents the audit defect D3: per-object POSTs are non-atomic.

    When the second device write fails, the first remains staged in the branch
    and the caller only sees the exception. If stage_discovery() ever gains
    rollback-on-failure semantics, update this test accordingly.
    """
    device_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal device_posts
        device_posts += 1
        if device_posts >= 2:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(201, json={"id": device_posts})

    reconciler, gql_http, rest_http = _reconciler_with_transport(handler)
    assets = {
        "devices": [
            {"name": "leaf-1", "device_type": 1, "role": 1, "site": 1},
            {"name": "leaf-2", "device_type": 1, "role": 1, "site": 1},
        ],
    }
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await reconciler.stage_discovery("br_test", assets)
    finally:
        await gql_http.aclose()
        await rest_http.aclose()

    # First write reached the branch before the failure: partial staging.
    assert device_posts == 2


@pytest.mark.asyncio
async def test_reconcile_flags_only_unregistered_assets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_topology_response())

    reconciler, gql_http, rest_http = _reconciler_with_transport(handler)
    live_topology = {
        "devices": [
            {"name": "core-sw-1", "interfaces": ["eth0", "eth7"]},  # eth7 is new
            {"name": "ghost-node-3", "interfaces": ["mgmt0"]},  # entirely new
        ],
        "cables": [
            {  # already cached via GraphQL response
                "a_device": "core-sw-1",
                "a_interface": "eth0",
                "b_device": "edge-rtr-2",
                "b_interface": "eth4",
            },
            {  # new connection
                "a_device": "ghost-node-3",
                "a_interface": "mgmt0",
                "b_device": "core-sw-1",
                "b_interface": "eth7",
            },
        ],
    }
    try:
        delta = await reconciler.reconcile(live_topology)
    finally:
        await gql_http.aclose()
        await rest_http.aclose()

    assert [d["name"] for d in delta["devices"]] == ["ghost-node-3"]
    new_ifaces = {(i["device"], i["name"]) for i in delta["interfaces"]}
    assert new_ifaces == {("core-sw-1", "eth7"), ("ghost-node-3", "mgmt0")}
    assert len(delta["cables"]) == 1


# ---------------------------------------------------------------------------
# Circuits — do-calculus escalation boundary
# ---------------------------------------------------------------------------


class _StubGraph:
    node_ids = frozenset({"circuit_CKT-001"})
    node_count = 1


class _StubGraphFactory:
    @classmethod
    async def load_from_db(cls, conn: Any, namespace_id: uuid.UUID, **kwargs: Any) -> _StubGraph:
        return _StubGraph()


class _StubDoCalculusEngine:
    def evaluate(
        self,
        graph: Any,
        intervention_node_id: str,
        intervention_state: str = "failed",
        **kwargs: Any,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            probability_matrix={"node-leaf-1": 0.92, "node-leaf-2": 0.20},
        )


def _circuits_client() -> tuple[NetBoxCircuitsClient, httpx.AsyncClient]:
    payload = {
        "results": [
            {
                "id": 7,
                "cid": "CKT-001",
                "provider": {"id": 3, "name": "Lumen"},
                "custom_fields": {"account_string": "ACCT-123"},
                "commit_rate": 10000,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NetBoxCircuitsClient(NETBOX_URL, TOKEN, client=http_client), http_client


@pytest.mark.asyncio
async def test_circuit_escalation_generates_critical_ticket_for_causal_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(circuits_mod, "CausalGraph", _StubGraphFactory)
    monkeypatch.setattr(circuits_mod, "DoCalculusEngine", _StubDoCalculusEngine)

    client, http_client = _circuits_client()
    escalator = NetBoxCircuitEscalator(client)
    try:
        tickets = await escalator.evaluate_and_escalate(
            conn=FakeConn(),
            namespace_id=uuid.uuid4(),
            telemetry_degradations={"node-leaf-1": 0.85, "node-leaf-2": 0.90},
            degradation_threshold=0.5,
            causal_threshold=0.5,
        )
    finally:
        await http_client.aclose()

    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["circuit_node_id"] == "circuit_CKT-001"
    assert ticket["provider_name"] == "Lumen"
    assert ticket["account_string"] == "ACCT-123"
    assert ticket["commit_rate_kbps"] == 10000
    # node-leaf-2 is degraded but causally improbable (0.20 < 0.5): excluded.
    assert set(ticket["causally_linked_degradations"]) == {"node-leaf-1"}
    # Linked degradation severity 0.85 >= 0.8 → CRITICAL.
    assert ticket["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_circuit_escalation_skips_when_no_degradation_clears_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(circuits_mod, "CausalGraph", _StubGraphFactory)
    monkeypatch.setattr(circuits_mod, "DoCalculusEngine", _StubDoCalculusEngine)

    client, http_client = _circuits_client()
    escalator = NetBoxCircuitEscalator(client)
    try:
        tickets = await escalator.evaluate_and_escalate(
            conn=FakeConn(),
            namespace_id=uuid.uuid4(),
            telemetry_degradations={"node-leaf-1": 0.10},
            degradation_threshold=0.5,
        )
    finally:
        await http_client.aclose()

    assert tickets == []


@pytest.mark.asyncio
async def test_circuit_escalation_is_stateless_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20 concurrent evaluations share one escalator; results must not contaminate.

    Also documents audit defect D4: re-evaluating identical telemetry yields a
    NEW ticket_id every time — deduplication is the caller's responsibility.
    """
    monkeypatch.setattr(circuits_mod, "CausalGraph", _StubGraphFactory)
    monkeypatch.setattr(circuits_mod, "DoCalculusEngine", _StubDoCalculusEngine)

    client, http_client = _circuits_client()
    escalator = NetBoxCircuitEscalator(client)

    async def run_one() -> list[dict[str, Any]]:
        return await escalator.evaluate_and_escalate(
            conn=FakeConn(),
            namespace_id=uuid.uuid4(),
            telemetry_degradations={"node-leaf-1": 0.85},
        )

    try:
        results = await asyncio.gather(*(run_one() for _ in range(20)))
    finally:
        await http_client.aclose()

    assert all(len(tickets) == 1 for tickets in results)
    ticket_ids = {tickets[0]["ticket_id"] for tickets in results}
    assert len(ticket_ids) == 20  # no shared mutable ticket state — and no dedup (D4)
    assert {tickets[0]["circuit_id"] for tickets in results} == {"CKT-001"}


# ---------------------------------------------------------------------------
# MTBF — anomaly aggregation + hardware wear-out synthesis
# ---------------------------------------------------------------------------


def _mtbf_forecaster(
    devices: list[dict[str, Any]],
) -> tuple[NetBoxMTBFForecaster, httpx.AsyncClient]:
    payload = {"data": {"device_list": devices}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = NetBoxGraphQLClient(NETBOX_URL, TOKEN, client=http_client)
    return NetBoxMTBFForecaster(client), http_client


@pytest.mark.asyncio
async def test_fetch_anomaly_counts_binds_rls_context_before_query() -> None:
    forecaster, http_client = _mtbf_forecaster([])
    ns = uuid.uuid4()
    conn = FakeConn(
        event_rows=[
            {"event_type": "store_memory_rolled_back", "params": {"device_id": "core-sw-1"}},
            {
                "event_type": "saga_recovered",
                "params": {"entities": [{"label": "core-sw-1"}, "edge-rtr-2"]},
            },
            # Same incident references the device twice: counted once.
            {
                "event_type": "ingest_failed",
                "params": {"device_id": "core-sw-1", "entities": ["core-sw-1"]},
            },
        ],
    )
    try:
        counts = await forecaster.fetch_anomaly_counts(conn, ns, window_days=30.0)
    finally:
        await http_client.aclose()

    # RLS context must be bound before the event_log scan executes.
    set_config_idx = next(
        i for i, (sql, _) in enumerate(conn.calls) if "set_config('nce.namespace_id'" in sql
    )
    fetch_idx = next(i for i, (sql, _) in enumerate(conn.calls) if "FROM event_log" in sql)
    assert set_config_idx < fetch_idx
    assert conn.namespace_context_values() == [str(ns)]

    assert counts == {"core-sw-1": 3, "edge-rtr-2": 1}


@pytest.mark.asyncio
async def test_mtbf_forecast_ranks_aged_and_anomalous_devices_highest() -> None:
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=180)).strftime("%Y-%m-%d")
    ancient = (now - timedelta(days=int(10 * 365.25))).strftime("%Y-%m-%d")
    devices = [
        {
            "id": "1",
            "name": "fresh-sw",
            "serial": "SER-FRESH",
            "custom_fields": {"provisioning_date": fresh, "hardware_lifespan_years": 5},
        },
        {
            "id": "2",
            "name": "ancient-sw",
            "serial": "SER-OLD",
            "custom_fields": {"provisioning_date": ancient, "hardware_lifespan_years": 5},
        },
        {
            "id": "3",
            "name": "anomalous-sw",
            "serial": "SER-ANOM",
            "custom_fields": {"provisioning_date": fresh, "hardware_lifespan_years": 5},
        },
    ]
    forecaster, http_client = _mtbf_forecaster(devices)
    conn = FakeConn(
        event_rows=[
            {"event_type": "hw_error", "params": {"device_id": "anomalous-sw"}} for _ in range(48)
        ],
    )
    try:
        matrix = await forecaster.evaluate_forecast(
            conn,
            uuid.uuid4(),
            forecast_window_days=30.0,
            observation_window_days=30.0,
            baseline_mtbf_years=10.0,
        )
    finally:
        await http_client.aclose()

    by_name = {row["device_name"]: row for row in matrix}
    assert set(by_name) == {"fresh-sw", "ancient-sw", "anomalous-sw"}

    # Wear-out: a device twice past its lifespan must outrank a fresh one.
    assert by_name["ancient-sw"]["failure_probability"] > by_name["fresh-sw"]["failure_probability"]
    # Empirical: 48 anomalies in 30 days dominate the age term for a fresh device.
    assert (
        by_name["anomalous-sw"]["failure_probability"] > by_name["fresh-sw"]["failure_probability"]
    )
    assert by_name["anomalous-sw"]["anomaly_count"] == 48

    # Matrix is sorted by failure probability, highest risk first.
    probs = [row["failure_probability"] for row in matrix]
    assert probs == sorted(probs, reverse=True)

    # Probabilities are valid and bounded.
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_mtbf_hardware_parsing_defaults_are_defensive() -> None:
    forecaster, _client = _mtbf_forecaster([])
    prov, lifespan, serial = forecaster._parse_device_hardware(
        {"name": "blank-device", "custom_fields": {}},
    )
    # Missing provisioning date falls back to ~3 years ago; lifespan to 5y.
    age_days = (datetime.now(timezone.utc) - prov).days
    assert 3 * 365 - 5 <= age_days <= 3 * 366 + 5
    assert lifespan == 5.0
    assert serial == "UNKNOWN"

    prov_iso, _, _ = forecaster._parse_device_hardware(
        {"name": "iso", "custom_fields": {"provisioning_date": "2024-06-01T00:00:00Z"}},
    )
    assert prov_iso == datetime(2024, 6, 1, tzinfo=timezone.utc)
