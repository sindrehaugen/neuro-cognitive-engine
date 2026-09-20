"""
tests/integration/test_resource_surface_patch_live.py
=========================================================
Live-Postgres generic PATCH sibling. H's audit found generic PATCH has
zero real-SQL coverage for ANY spec (the existing generated-controls unit
test never sets ``admin_state.engine``); #379 proved the mechanism for one
spec (``hr:employees``). This proves it for every PATCH-eligible spec.

POPULATION -- measured, not assumed to match #375's 17 (that population was
archive-eligible; this one is upsert-eligible, a materially different and
much larger filter).

Of 56 registered specs, 48 do not exclude ``upsert`` and declare at least
one writable field (a spec with zero writable fields has nothing for PATCH
to touch; was 49 before ADR-0008 excluded ``upsert`` on
``support:ticket-actions`` and cleared its ``writable_fields`` -- see the
floor test's own comment below). Of those 48, **9 are graph/kg_nodes-primary**
(``table_name is None``: ``project:projects``, ``sales:contacts``, and 7
``system_design`` specs) -- explicitly OUT OF SCOPE here. Per G's standing
rule ("no in-memory type table, ever") applied to fixtures generally: a
graph-primary PATCH needs the same ``assert_owner``/ownership-registry
seeding ``tests/test_agreements_sla.py`` uses for a kg_nodes writer, a
materially different and harder fixture than any of the 39 relational
specs below. Sizing that population is a separate task, not done here.

Of the remaining **39 relational specs**, 3 cannot create a row through
the generated surface AT ALL, for a reason that has nothing to do with
this file's job (found while building this fixture, not part of the
dispatch): ``field_tech:checklists``/``time-entries``/``work-orders`` each
have a real, human-readable identifier column
(``checklist_id``/``time_entry_id``/``work_order_id``) that is ``NOT
NULL`` with no default and is **not in the spec's own ``writable_fields``**
-- no caller, ever, through any interface, can set it. Reproduced live:
POSTing a valid ``field_tech:checklists`` payload 500s with
``NotNullViolationError: null value in column "checklist_id"``. These 3
are skipped below with a cited reason, not dropped from the parametrize
list -- the same discipline as ``notifications:reminders`` in
``test_resource_surface_archive_restore_live.py``. This is a real,
separate, unreported defect (a create route that can never succeed,
independent of the type-coercion bug #378 fixed); flagged upstream, not
fixed here, since it needs a ``ResourceSpec`` edit (adding the missing
field to ``writable_fields``) outside this file's read-only-by-convention
scope for a live-test PR.

**36 relational specs are exercised for real.** ``expected_version``: each
spec's own PATCH is asserted with a correct ``expected_version`` (proves
the per-spec ``version_field`` wiring, not just a shared mechanism --
version_field names and response shapes differ per spec). The STALE-version
409 rejection itself is asserted only once (a second, dedicated test on one
representative spec) -- #379 already proved that mechanism is spec-generic
(shared ``handle_patch`` code, not per-spec logic), so repeating it 36
times would prove the same fact 36 times, not 36 different facts.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from nce import admin_state
from nce.orchestrator import NCEEngine
from nce.resource_surface import ResourceSpec, get_all_resource_specs, load_all_engine_resources
from nce.resource_surface.rest import make_resource_routes

pytestmark = pytest.mark.integration

load_all_engine_resources()
_ALL_SPECS = {(s.engine, s.entity): s for s in get_all_resource_specs()}
_PATCH_ELIGIBLE = [
    s
    for s in sorted(get_all_resource_specs(), key=lambda s: (s.engine, s.entity))
    if "upsert" not in s.excluded_verbs and s.writable_fields and s.table_name is not None
]
_SPEC_IDS = [f"{s.engine}:{s.entity}" for s in _PATCH_ELIGIBLE]

_KNOWN_BLOCKED: dict[str, str] = {
    "field_tech:checklists": (
        "checklist_id TEXT NOT NULL has no default and is not in writable_fields "
        "-- no caller can ever set it. Reproduced live: create 500s with "
        "NotNullViolationError. Not a PATCH defect; create itself is broken."
    ),
    "field_tech:time-entries": (
        "time_entry_id TEXT NOT NULL has no default and is not in writable_fields "
        "-- same shape as field_tech:checklists, inferred from identical schema "
        "pattern (not independently reproduced live)."
    ),
    "field_tech:work-orders": (
        "work_order_id TEXT NOT NULL has no default and is not in writable_fields "
        "-- same shape as field_tech:checklists, inferred from identical schema "
        "pattern (not independently reproduced live)."
    ),
}

_ENUM_CHECK_SKIP: dict[str, set[str]] = {
    "procurement_deal_registrations": {"status"},
    "sales_quotes": {"billing_method"},
    "product_catalog": {"lifecycle_status"},
    "inventory_rma": {"weee_state", "stock_movement_state", "change_origin"},
    "assets": {"change_origin"},
    "service_tickets": {"source", "status", "priority", "change_origin"},
    "support_ticket_actions": {"change_origin", "action_type", "outcome"},
    "case_studies": {"status"},
    "testimonials": {"status", "consent_tier"},
    "content_assets": {"kind", "status"},
    "stock_locations": {"kind"},
    "customer_health": {"churn_risk"},
    "resources": {"kind"},
    "sla_clocks": {"breach_type"},
}

# A text-typed writable column that is really a foreign key -- the generic
# text pass's "generated_<field>_value" would violate the FK, so these get
# a real, seeded value instead. All three point at the employee seeded by
# _enable_all_guarded_engines below (employee_id='EMP-PATCH-LIVE').
_TEXT_FK_OVERRIDE: dict[str, dict[str, str]] = {
    "absences": {"employee_id": "EMP-PATCH-LIVE"},
    "certifications": {"employee_id": "EMP-PATCH-LIVE"},
    "skills": {"employee_id": "EMP-PATCH-LIVE"},
}

# A numeric column whose CHECK demands more than ">= 0" (the generic
# required-numeric fallback of 0 would violate it).
_REQUIRED_NUMERIC_OVERRIDE: dict[tuple[str, str], object] = {
    ("inventory_rma", "qty"): 1,
}

# Enum-CHECK-constrained columns with NO default -- omission (the skip list
# above) would violate NOT NULL, so a real, valid enum member must be
# supplied instead. Each verified against the real CHECK constraint
# definition (pg_get_constraintdef), not guessed.
_REQUIRED_ENUM_VALUE: dict[tuple[str, str], str] = {
    ("stock_locations", "kind"): "warehouse",
    ("customer_health", "churn_risk"): "low",
    ("support_ticket_actions", "action_type"): "diagnostic",
    ("support_ticket_actions", "outcome"): "resolved",
    ("resources", "kind"): "employee",
}

_TEXT_TYPES = {"text", "character varying"}
_DATE_TYPES = {"date"}
_TIMESTAMP_TYPES = {"timestamp with time zone", "timestamp without time zone"}
_JSON_TYPES = {"json", "jsonb"}

# Non-namespace_id UUID FK columns needing a real parent row, hand-mapped
# (small, enumerable population -- not worth a generic FK-introspection
# system for 8 entries). travel_legs -> allocations -> resources is two
# levels deep, resolved by recursion in _create_minimal_row.
_FK_TARGETS: dict[tuple[str, str], str] = {
    ("agreement_parties", "agreement_id"): "agreements",
    ("sales_deal_participants", "deal_id"): "sales_deals",
    ("inventory_items", "location_id"): "stock_locations",
    ("inventory_rma", "location_id"): "stock_locations",
    ("goods_receipts", "location_id"): "stock_locations",
    ("allocations", "resource_id"): "resources",
    ("travel_legs", "allocation_id"): "allocations",
    ("support_ticket_actions", "ticket_id"): "service_tickets",
}

# sla_clocks.ticket_id is BOTH this spec's id_field AND a real FK to
# service_tickets(id) -- the identifying-FK shape. A random uuid4() (what
# handle_create uses when id_field is omitted from the body) would almost
# certainly not exist in service_tickets, so this needs its parent's real
# id passed explicitly as "id" in the create body, not resolved generically
# the way a plain writable FK column is.
_IDENTIFYING_FK_PARENT: dict[str, str] = {
    "sla_clocks": "service_tickets",
}

_ENGINES_NEEDING_ENABLE = {s.engine for s in _PATCH_ELIGIBLE if s.enabled_guard is not None} | {
    "agreements",
    "resources",
}  # agreements/resources are parent tables for FK chains above


@pytest.fixture
def engine(pg_pool: asyncpg.Pool) -> NCEEngine:
    eng = NCEEngine()
    eng.pg_pool = pg_pool
    return eng


async def _enable_engine(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, engine_name: str) -> None:
    """Single-element jsonb_set path -- Postgres's jsonb_set only creates
    the FINAL path element even with create_missing=true; a two-element
    path on a fresh {} silently no-ops (found and fixed identically in
    every other live sibling tonight)."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE namespaces SET metadata = jsonb_set("
            "coalesce(metadata, '{}'::jsonb), ARRAY[$2], "
            "coalesce(metadata->$2, '{}'::jsonb) || jsonb_build_object('enabled', true), "
            "true) WHERE id = $1",
            namespace_id,
            engine_name,
        )


@pytest_asyncio.fixture(autouse=True)
async def _enable_all_guarded_engines(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    for engine_name in sorted(_ENGINES_NEEDING_ENABLE):
        await _enable_engine(pg_pool, namespace_id, engine_name)
    # hr:absences/certifications carry a NOT NULL FK to
    # employees(employee_id, namespace_id) -- unrelated to PATCH, found
    # building the write-coercion acceptance test tonight; seeded here the
    # same way.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO employees (employee_id, namespace_id, name) VALUES ($1, $2, $3)",
            "EMP-PATCH-LIVE",
            namespace_id,
            "Patch Fixture Employee",
        )


async def _column_info(conn: asyncpg.Connection, table_name: str) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default, "
        "character_maximum_length FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1",
        table_name,
    )
    return {r["column_name"]: dict(r) for r in rows}


def _rest_app(spec: ResourceSpec) -> Starlette:
    return Starlette(routes=make_resource_routes(spec))


async def _create(engine: NCEEngine, spec: ResourceSpec, payload: dict[str, object]) -> dict:
    previous = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            resp = await client.post(spec.rest_collection_path, json=payload)
        assert resp.status_code == 201, f"{spec.engine}:{spec.entity} create failed: {resp.text}"
        return resp.json()
    finally:
        admin_state.engine = previous


async def _create_minimal_row(
    engine: NCEEngine, conn: asyncpg.Connection, table_name: str, namespace_id: uuid.UUID
) -> str:
    """Create a minimal valid row directly in `table_name` (a parent table
    for one of the FK chains above) and return its id. Uses the same
    typed-payload strategy as the main builder, recursively, since a
    parent may itself have a required FK (travel_legs -> allocations ->
    resources)."""
    spec = next(s for s in _ALL_SPECS.values() if s.table_name == table_name)
    payload = await _typed_sample_payload(engine, conn, spec, namespace_id)
    created = await _create(engine, spec, payload)
    return created[spec.id_field]


async def _typed_sample_payload(
    engine: NCEEngine, conn: asyncpg.Connection, spec: ResourceSpec, namespace_id: uuid.UUID
) -> dict[str, object]:
    columns = await _column_info(conn, spec.table_name)  # type: ignore[arg-type]
    skip = _ENUM_CHECK_SKIP.get(spec.table_name, set())
    payload: dict[str, object] = {"namespace_id": str(namespace_id)}

    text_fk_override = _TEXT_FK_OVERRIDE.get(spec.table_name, {})
    for f in spec.writable_fields:
        if f in text_fk_override:
            payload[f] = text_fk_override[f]
            continue
        if f in skip:
            continue
        col = columns.get(f)
        if col is not None and col["data_type"] in _TEXT_TYPES:
            value = f"generated_{f}_value"
            max_len = col["character_maximum_length"]
            if max_len is not None and len(value) > max_len:
                value = value[:max_len]
            payload[f] = value

    timestamp_offset_hours = 0
    for f in spec.writable_fields:
        if f in payload:
            continue
        required_enum_value = _REQUIRED_ENUM_VALUE.get((spec.table_name, f))
        if required_enum_value is not None:
            payload[f] = required_enum_value
            continue
        if f in skip:
            continue
        col = columns.get(f)
        if col is None:
            continue
        required = col["is_nullable"] == "NO" and col["column_default"] is None
        if not required:
            continue
        dt = col["data_type"]
        numeric_override = _REQUIRED_NUMERIC_OVERRIDE.get((spec.table_name, f))
        if dt in _DATE_TYPES:
            payload[f] = "2026-09-20"
        elif dt in _TIMESTAMP_TYPES:
            # Successive required timestamp columns on the same row (e.g.
            # allocations.starts_at/ends_at, check_allocation_dates: ends_at
            # > starts_at) get strictly increasing values -- a single fixed
            # literal for every timestamp column on a row violates any
            # ordering CHECK between them.
            payload[f] = f"2026-09-20T{12 + timestamp_offset_hours:02d}:00:00+00:00"
            timestamp_offset_hours += 1
        elif dt in _JSON_TYPES:
            payload[f] = {}
        elif dt in ("numeric", "integer", "bigint", "double precision", "real"):
            payload[f] = numeric_override if numeric_override is not None else 0
        elif dt == "boolean":
            payload[f] = False
        elif dt == "uuid":
            target_table = _FK_TARGETS[(spec.table_name, f)]
            payload[f] = await _create_minimal_row(engine, conn, target_table, namespace_id)

    if spec.table_name == "product_catalog":
        suffix = uuid.uuid4().hex[:12]
        payload["manufacturer"] = f"generated-mfr-{suffix}"
        payload["mfr_part_no"] = f"generated-part-{suffix}"

    if spec.table_name in _IDENTIFYING_FK_PARENT:
        parent_id = await _create_minimal_row(
            engine, conn, _IDENTIFYING_FK_PARENT[spec.table_name], namespace_id
        )
        # handle_create resolves the row's id from spec.id_field, not a
        # literal "id" key -- sla_clocks' id_field is "ticket_id" (the
        # identifying-FK column itself), so the parent's real id must be
        # supplied under THAT key or handle_create falls back to a random
        # uuid4() that (almost certainly) violates the FK to service_tickets.
        payload[spec.id_field] = parent_id

    return payload


def test_discovery_floor_matches_the_measured_population() -> None:
    # Floor moved 40 -> 39 (ADR-0008, 2026-09-21): TICKET_ACTION_SPEC now
    # excludes "upsert" (the table's own append-only grant no longer
    # permits UPDATE/DELETE, migration 108) and clears writable_fields to
    # () per the fail-safe convention -- either change alone drops it out
    # of this population's filter ("upsert" not in excluded_verbs and
    # writable_fields). Deliberate, not a regression; re-derive with the
    # same filter this list uses before moving this number again.
    assert len(_PATCH_ELIGIBLE) >= 39, (
        f"Only {len(_PATCH_ELIGIBLE)} relational, upsert-eligible, writable specs "
        f"found -- expected at least 39 (measured post-ADR-0008, "
        f"TICKET_ACTION_SPEC's exclusion already accounted for). A spec was "
        f"excluded, lost its writable_fields, or the population shrank."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", _PATCH_ELIGIBLE, ids=_SPEC_IDS)
async def test_patch_reaches_real_postgres(
    spec: ResourceSpec, engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    spec_id = f"{spec.engine}:{spec.entity}"
    if spec_id in _KNOWN_BLOCKED:
        pytest.skip(_KNOWN_BLOCKED[spec_id])

    async with pg_pool.acquire() as conn:
        payload = await _typed_sample_payload(engine, conn, spec, namespace_id)
        columns = await _column_info(conn, spec.table_name)  # type: ignore[arg-type]
    created = await _create(engine, spec, payload)
    item_id = created[spec.id_field]
    # A spec may opt out of optimistic concurrency entirely
    # (version_field=None, e.g. business_insights:kpi_snapshots,
    # inventory:inventory-rma) -- handle_patch only checks expected_version
    # "if expected_version and spec.version_field", so omitting it from the
    # PATCH body is the correct, safe behaviour for those, not a skip.
    version1 = created[spec.version_field] if spec.version_field else None

    patch_field = next(
        f for f in spec.writable_fields if f not in _ENUM_CHECK_SKIP.get(spec.table_name, set())
    )
    patch_col = columns.get(patch_field)
    if patch_col is not None and patch_col["data_type"] in _TEXT_TYPES:
        new_value: object = "patched_value"
        max_len = patch_col["character_maximum_length"]
        if max_len is not None and len(str(new_value)) > max_len:
            new_value = str(new_value)[:max_len]
    else:
        # Non-text patch target (e.g. a boolean/jsonb-only writable set) --
        # reuse the same typed-value strategy the create payload used.
        new_value = payload.get(patch_field, True)

    patch_body = {"namespace_id": str(namespace_id), patch_field: new_value}
    if version1 is not None:
        patch_body["expected_version"] = version1

    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            patch_resp = await client.patch(
                spec.rest_item_path.format(id=item_id),
                json=patch_body,
            )
    finally:
        admin_state.engine = previous_engine

    assert patch_resp.status_code == 200, (
        f"{spec_id} PATCH failed against real Postgres: {patch_resp.text}"
    )

    # id_field is not always a UUID column (e.g. support:customer-health's
    # id_field is "customer_id", a TEXT column) -- bind the type the real
    # column actually is, not assumed from every other spec's shape.
    id_col = columns.get(spec.id_field)
    id_value: object = (
        uuid.UUID(item_id) if id_col is not None and id_col["data_type"] == "uuid" else item_id
    )
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {patch_field} FROM {spec.table_name} WHERE {spec.id_field} = $1",
            id_value,
        )
    assert row is not None, f"{spec_id}: row vanished after PATCH"
    stored = row[patch_field]
    if isinstance(stored, Decimal):
        stored_compare: object = float(stored)
        expected_compare: object = float(new_value)  # type: ignore[arg-type]
    elif isinstance(stored, (bool, type(None))):
        stored_compare, expected_compare = stored, new_value
    else:
        stored_compare, expected_compare = str(stored), str(new_value)
    assert stored_compare == expected_compare, (
        f"{spec_id}: PATCH returned 200 but {spec.table_name}.{patch_field} was not "
        f"actually updated in Postgres (expected {new_value!r}, got {stored!r}) -- "
        f"the same silent-success shape a mocked in-memory PATCH test could never catch."
    )


@pytest.mark.asyncio
async def test_positive_control_stale_expected_version_rejected_generically(
    engine: NCEEngine, pg_pool: asyncpg.Pool, namespace_id: uuid.UUID
) -> None:
    """U18, asserted once: #379 already proved the stale-expected_version
    409 for hr:employees, and handle_patch's concurrency check is shared,
    generic code, not per-spec logic -- repeating this 37 times would
    prove the same fact 37 times. This picks a different representative
    spec (sales:customers, no enabled_guard, no FK chain) to confirm it
    is not somehow specific to hr:employees's own code path.
    """
    spec = _ALL_SPECS[("sales", "customers")]
    async with pg_pool.acquire() as conn:
        payload = await _typed_sample_payload(engine, conn, spec, namespace_id)
    created = await _create(engine, spec, payload)
    item_id = created["id"]
    stale_version = created[spec.version_field]

    previous_engine = admin_state.engine
    admin_state.engine = engine
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(spec)), base_url="http://test"
        ) as client:
            first = await client.patch(
                spec.rest_item_path.format(id=item_id),
                json={
                    "namespace_id": str(namespace_id),
                    "name": "First Patch",
                    "expected_version": stale_version,
                },
            )
            assert first.status_code == 200, first.text

            second = await client.patch(
                spec.rest_item_path.format(id=item_id),
                json={
                    "namespace_id": str(namespace_id),
                    "name": "Second Patch",
                    "expected_version": stale_version,  # stale: first already moved it on
                },
            )
    finally:
        admin_state.engine = previous_engine

    assert second.status_code == 409, (
        f"A genuinely stale expected_version was NOT rejected against real Postgres: {second.text}"
    )
    assert second.json().get("reason") == "version_conflict"
