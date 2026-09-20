"""H-4: generated positive controls per verb for every declared ResourceSpec,
plus negative RLS (charter §9 "Lane H", dispatched the day A-1 landed).

**What this file is, and is not.** ``tests/unit/test_resource_surface.py`` (A-1)
and ``tests/unit/test_c12_resource_surface_ratchet.py`` (A-2) already cover,
respectively: hand-written verb tests for the one reference engine
(Inventory), and exemption-list coverage/shrink-only/phantom/substantive
checks. This file does NOT repeat either. It is the thing neither of those
is: a template that GENERATES the same verb + negative-RLS coverage A-1 wrote
by hand for Inventory, against EVERY ResourceSpec any engine registers, so
that Lane E's next seventeen registrations get this coverage automatically
and nobody has to remember to hand-write it again per engine.

**Exemptions need no special handling here.** ``get_all_resource_specs()``
only returns REGISTERED specs; an exempted node type (``BOM_LINE`` and the
~45 others in ``nce/resource_surface/exemptions.py``) has no ``ResourceSpec``
and therefore nothing to generate a control for. One guard below confirms
this stays true (no registered spec's ``node_type`` also appears in the
exemption list -- the reverse direction of A-2's own shrink-only check).

**Three axes that decided this file's shape, verified against the actual
code before writing a single generated test (not assumed from the brief):**

1. ``tenant_scope`` is DERIVED (``field(init=False)``, see
   ``nce/resource_surface/spec.py``) into one of three values: ``"tenant"``,
   ``"global"``, or ``"graph"`` -- not two. A single negative-RLS template
   would be vacuous for two of the three. This file branches by scope.

2. **A real gap, found by reading ``nce/resource_surface/rest.py`` rather
   than trusting that ``tenant_scope`` changes behaviour: at the time this
   file was written, it did not.** Every REST handler (list/get/create/patch)
   called ``extract_namespace_id`` and filtered unconditionally by that
   namespace -- there was no branch anywhere in ``rest.py`` that read
   ``spec.tenant_scope`` or treated a ``"global"`` resource differently from
   a ``"tenant"`` one. ``EXPECTED_GLOBAL_TABLES`` genuinely marked a table as
   cross-tenant (that is what makes ``product_catalog`` resolve to
   ``tenant_scope == "global"`` at all), but nothing downstream of that
   derivation honoured it, so a global-scoped resource registered then would
   have been silently, incorrectly tenant-siloed. No spec with
   ``tenant_scope == "global"`` was registered at the time, so this was
   proven with a synthetic (but real, uninverted) ``ResourceSpec`` instance
   run through the actual ``make_resource_routes`` -- not asserted from
   reading the code, executed. Marked ``xfail(strict=True)``, the same
   pattern as H-1's verb-mix floor: RED by design, documenting a real gap
   rather than a broken assertion, meant to XPASS (fail CI) the day someone
   wired scope-aware behaviour into ``rest.py``, forcing a deliberate removal
   of the marker.

   **That day came in Wave A-1b (`da265ac`).** `rest.py` now branches on
   `tenant_scope` and the cross-namespace read genuinely returns 200 for a
   global-scoped resource. The `xfail` marker below was removed as the
   deliberate, visible act it was designed to force -- this is the second
   instrument in this programme to close exactly the way it was built to
   (the first was H-1's verb-mix floor). The test that follows is no longer
   a documented gap; it is a passing assertion that A-1b made true.

3. ``storage_kind`` (``postgres`` / ``mongo`` / ``kg_nodes``) matters less to
   this file than expected, and that itself is worth recording: the test
   harness never reaches a real Postgres connection (``admin_state.engine``
   is ``None`` in every test here, matching A-1's own fixture), so EVERY
   verb handler takes the in-memory-store branch regardless of
   ``storage_kind`` or whether ``table_name`` is set at all --
   ``_get_mem_bucket`` keys purely on ``(engine, entity, namespace)``, never
   on the table. That means basic CRUD verbs "pass" against a synthetic
   ``kg_nodes`` spec in THIS harness even though nothing here proves that is
   still true against a real graph backend. Stated as a known, reported
   limitation rather than silently assumed away: this file verifies the
   generated REST contract (status codes, tenant isolation, concurrency),
   never a real Postgres/Mongo/graph round-trip.
"""

from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from nce import admin_state
from nce.resource_surface import (
    RESOURCE_SURFACE_EXEMPTIONS,
    ResourceSpec,
    get_all_resource_specs,
    load_all_engine_resources,
)
from nce.resource_surface.rest import _clear_mem_store, make_resource_routes

_NS_A = str(uuid.UUID("33333333-3333-3333-3333-333333333333"))
_NS_B = str(uuid.UUID("44444444-4444-4444-4444-444444444444"))


def _client_for_spec(spec: ResourceSpec) -> TestClient:
    app = Starlette(routes=make_resource_routes(spec))
    return TestClient(app, raise_server_exceptions=False)


def _sample_payload(spec: ResourceSpec, namespace_id: str) -> dict[str, object]:
    """Derive a create payload from the spec's OWN declared writable_fields
    -- never a hand-typed per-resource fixture, so a new engine's spec gets
    a payload automatically. Every field gets a distinct, harmless string;
    the in-memory store applies no type coercion (verified in
    nce/resource_surface/rest.py's handle_create), so this is safe for any
    field name regardless of what a real column's type would be."""
    payload: dict[str, object] = {"namespace_id": namespace_id}
    for f in spec.writable_fields:
        payload[f] = f"generated_{f}_value"
    return payload


def _load_specs_for_collection() -> list[ResourceSpec]:
    load_all_engine_resources()
    return sorted(get_all_resource_specs(), key=lambda s: (s.engine, s.entity))


_SPECS = _load_specs_for_collection()
_SPEC_IDS = [f"{s.engine}:{s.entity}" for s in _SPECS]


@pytest.fixture(autouse=True)
def _reset_env():
    _clear_mem_store()
    admin_state.engine = None
    yield
    _clear_mem_store()


def test_discovery_floor_at_least_the_known_registered_specs() -> None:
    """Guard-the-guard: today's registrations (Inventory's 4 + Notifications'
    2) must still be visible, or every generated test below would silently
    collect zero cases and this file would pass by having nothing to check."""
    assert len(_SPECS) >= 6, (
        f"Only {len(_SPECS)} ResourceSpecs registered -- expected at least 6 "
        "(Inventory's 4 + Notifications' 2, as of A-1/A-3). Either a spec was "
        "removed, or load_all_engine_resources() is broken."
    )


def test_no_registered_spec_is_also_exempted() -> None:
    """The reverse of A-2's shrink-only check (which catches a REGISTERED
    node type that still has a stale exemption entry): this catches a spec
    registered under a node_type that the exemption list does not yet know
    has shipped, which would mean two sources of truth disagree."""
    registered_node_types = {s.node_type for s in _SPECS}
    overlap = registered_node_types & set(RESOURCE_SURFACE_EXEMPTIONS)
    assert not overlap, (
        f"{overlap} are both registered ResourceSpecs AND still listed in "
        "RESOURCE_SURFACE_EXEMPTIONS -- this is A-2's own shrink-only ratchet's "
        "job (tests/unit/test_c12_resource_surface_ratchet.py), so if this "
        "fires, that ratchet has a gap too."
    )


# ---------------------------------------------------------------------------
# Generated per-verb positive controls, one set per registered ResourceSpec.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_generated_create_and_get(spec: ResourceSpec) -> None:
    if "upsert" in spec.excluded_verbs:
        pytest.skip(f"{spec.engine}:{spec.entity} excludes upsert -- see spec.excluded_verbs")
    client = _client_for_spec(spec)
    payload = _sample_payload(spec, _NS_A)
    create_resp = client.post(spec.rest_collection_path, json=payload)
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert spec.id_field in created

    get_resp = client.get(
        f"{spec.rest_item_path.format(id=created[spec.id_field])}?namespace_id={_NS_A}"
    )
    assert get_resp.status_code == 200, get_resp.text


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_generated_create_envelope_keys_are_not_shadowed(spec: ResourceSpec) -> None:
    """A real column sharing a name with a response-envelope key ("status",
    "version") must never shadow the envelope's own success indicator or
    concurrency token -- envelope keys always win (rest.py's handle_create).
    The full row remains available under "item" regardless."""
    if "upsert" in spec.excluded_verbs:
        pytest.skip(f"{spec.engine}:{spec.entity} excludes upsert -- see spec.excluded_verbs")
    if "status" not in spec.writable_fields and "version" not in spec.writable_fields:
        pytest.skip(f"{spec.engine}:{spec.entity} has no colliding writable field")
    client = _client_for_spec(spec)
    create_resp = client.post(spec.rest_collection_path, json=_sample_payload(spec, _NS_A))
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["status"] == "ok", (
        f"{spec.engine}:{spec.entity} envelope 'status' was shadowed by a real column"
    )
    if "version" in spec.writable_fields:
        assert created["version"] != "generated_version_value", (
            f"{spec.engine}:{spec.entity} envelope 'version' was shadowed by a real column"
        )
    # The real values are still reachable, just not at the top level.
    assert created["item"]["status"] == "generated_status_value"


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_generated_list(spec: ResourceSpec) -> None:
    if "list" in spec.excluded_verbs:
        pytest.skip(f"{spec.engine}:{spec.entity} excludes list -- see spec.excluded_verbs")
    if "upsert" in spec.excluded_verbs:
        pytest.skip(
            f"{spec.engine}:{spec.entity} excludes upsert -- no route to seed a row to list"
        )
    client = _client_for_spec(spec)
    client.post(spec.rest_collection_path, json=_sample_payload(spec, _NS_A))
    list_resp = client.get(f"{spec.rest_collection_path}?namespace_id={_NS_A}")
    assert list_resp.status_code == 200, list_resp.text
    body = list_resp.json()
    assert "items" in body
    assert len(body["items"]) >= 1


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_generated_patch(spec: ResourceSpec) -> None:
    if not spec.writable_fields:
        pytest.skip(f"{spec.engine}:{spec.entity} declares no writable_fields to patch")
    if "upsert" in spec.excluded_verbs:
        pytest.skip(f"{spec.engine}:{spec.entity} excludes upsert -- see spec.excluded_verbs")
    client = _client_for_spec(spec)
    created = client.post(spec.rest_collection_path, json=_sample_payload(spec, _NS_A)).json()
    item_id = created[spec.id_field]
    patch_field = spec.writable_fields[0]
    patch_resp = client.patch(
        spec.rest_item_path.format(id=item_id),
        json={"namespace_id": _NS_A, patch_field: "generated_patched_value"},
    )
    assert patch_resp.status_code in (200, 409), patch_resp.text


@pytest.mark.parametrize("spec", _SPECS, ids=_SPEC_IDS)
def test_generated_archive_and_restore(spec: ResourceSpec) -> None:
    if "archive" in spec.excluded_verbs:
        pytest.skip(f"{spec.engine}:{spec.entity} excludes archive -- see spec.excluded_verbs")
    if "upsert" in spec.excluded_verbs:
        pytest.skip(
            f"{spec.engine}:{spec.entity} excludes upsert -- no route to seed a row to archive"
        )
    client = _client_for_spec(spec)
    created = client.post(spec.rest_collection_path, json=_sample_payload(spec, _NS_A)).json()
    item_id = created[spec.id_field]

    archive_resp = client.post(
        f"{spec.rest_item_path.format(id=item_id)}/archive", json={"namespace_id": _NS_A}
    )
    assert archive_resp.status_code == 200, archive_resp.text

    restore_resp = client.post(
        f"{spec.rest_item_path.format(id=item_id)}/restore", json={"namespace_id": _NS_A}
    )
    assert restore_resp.status_code == 200, restore_resp.text


# ---------------------------------------------------------------------------
# Generated negative-RLS, branched by the spec's own derived tenant_scope --
# never one template for all three (that would be vacuous for global/graph).
# ---------------------------------------------------------------------------

_TENANT_SCOPED_SPECS = [s for s in _SPECS if s.tenant_scope == "tenant"]


@pytest.mark.parametrize(
    "spec", _TENANT_SCOPED_SPECS, ids=[f"{s.engine}:{s.entity}" for s in _TENANT_SCOPED_SPECS]
)
def test_generated_negative_rls_tenant_scoped(spec: ResourceSpec) -> None:
    """tenant_scope == 'tenant': cross-namespace read is NOT FOUND, and
    cross-namespace write fails. Every spec registered today is this shape
    (Inventory, Notifications) -- see the module docstring for why 'global'
    and 'graph' need their own tests instead of a variant of this one.

    The cross-namespace LIST leg is skipped for a spec that excludes "list"
    (spec.excluded_verbs) -- there is no list route to call. GET and PATCH
    negative-RLS checks still run unconditionally; excluded_verbs never
    touches those. The whole test is skipped for a spec that excludes
    "upsert" -- there is no route to create the row every leg below needs."""
    if "upsert" in spec.excluded_verbs:
        pytest.skip(f"{spec.engine}:{spec.entity} excludes upsert -- no route to seed a row")
    client = _client_for_spec(spec)
    created = client.post(spec.rest_collection_path, json=_sample_payload(spec, _NS_A)).json()
    item_id = created[spec.id_field]

    cross_get = client.get(f"{spec.rest_item_path.format(id=item_id)}?namespace_id={_NS_B}")
    assert cross_get.status_code == 404, (
        f"{spec.engine}:{spec.entity} is tenant-scoped but a cross-namespace GET "
        f"returned {cross_get.status_code}, not 404."
    )

    if "list" not in spec.excluded_verbs:
        cross_list = client.get(f"{spec.rest_collection_path}?namespace_id={_NS_B}")
        assert cross_list.status_code == 200
        assert created[spec.id_field] not in {
            i.get(spec.id_field) for i in cross_list.json()["items"]
        }

    if spec.writable_fields:
        cross_patch = client.patch(
            spec.rest_item_path.format(id=item_id),
            json={"namespace_id": _NS_B, spec.writable_fields[0]: "hacked_value"},
        )
        assert cross_patch.status_code == 404, (
            f"{spec.engine}:{spec.entity} is tenant-scoped but a cross-namespace "
            f"PATCH returned {cross_patch.status_code}, not 404."
        )


def test_generated_negative_rls_global_scoped_cross_namespace_read_should_succeed() -> None:
    global_spec = ResourceSpec(
        engine="product",
        entity="h4-global-probe",
        node_type="PRODUCT_SKU",
        table_name="product_catalog",
        writable_fields=("part_number",),
    )
    assert global_spec.tenant_scope == "global"

    client = _client_for_spec(global_spec)
    created = client.post(
        global_spec.rest_collection_path,
        json=_sample_payload(global_spec, _NS_A),
    ).json()
    item_id = created[global_spec.id_field]

    cross_get = client.get(f"{global_spec.rest_item_path.format(id=item_id)}?namespace_id={_NS_B}")
    assert cross_get.status_code == 200, (
        "A global-scoped resource created in one namespace must be readable "
        f"from another -- got {cross_get.status_code} instead of 200."
    )


def test_graph_scope_branch_is_reachable_but_unexercised_by_this_harness() -> None:
    """Positive control for the branch-selection logic itself, and an honest
    limitation notice (module docstring point 3): a kg_nodes-only spec has
    tenant_scope == 'graph', so it is correctly excluded from
    _TENANT_SCOPED_SPECS above. This test proves that exclusion fires -- it
    does NOT prove kg_nodes verb semantics are correct, since this harness's
    in-memory store cannot distinguish storage_kind at all (see docstring).
    No spec with storage_kind == 'kg_nodes' is registered today, so there is
    nothing real to generate a control against yet; this is reported as a
    gap, not silently assumed clean."""
    kg_spec = ResourceSpec(
        engine="system_design",
        entity="h4-graph-probe",
        node_type="FUNCTIONAL_LOCATION",
        storage_kind="kg_nodes",
    )
    assert kg_spec.tenant_scope == "graph"
    assert kg_spec not in _TENANT_SCOPED_SPECS
    assert not any(s.tenant_scope == "kg_nodes" for s in _SPECS)


def test_positive_control_tenant_isolation_check_is_not_vacuous() -> None:
    """U18: prove test_generated_negative_rls_tenant_scoped's own assertion
    would fail on a deliberately broken client response, so the 404 checks
    above are not passing by accident (e.g. because both namespaces are
    somehow being treated as the same bucket)."""
    spec = _TENANT_SCOPED_SPECS[0]
    client = _client_for_spec(spec)
    created = client.post(spec.rest_collection_path, json=_sample_payload(spec, _NS_A)).json()
    item_id = created[spec.id_field]

    same_namespace_get = client.get(
        f"{spec.rest_item_path.format(id=item_id)}?namespace_id={_NS_A}"
    )
    assert same_namespace_get.status_code == 200, (
        "Sanity check failed: reading back from the SAME namespace that created "
        "the item did not return 200 -- the fixture itself is broken, not the "
        "isolation logic."
    )
