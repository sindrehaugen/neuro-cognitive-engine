r"""nce.vertical_modules.system_design.resources -- Resource definitions for
System Design Engine's graph-primary node types.

Lane E Wave E-11 (unblocked by Lane H's Wave 3(b), PR #307, which built
kg_nodes-primary ResourceSpec support -- spec.py's ``storage_kind == "kg_nodes"
or table_name is None`` branch and the real list/get/create/patch paths in
mcp.py/rest.py -- but deliberately did not declare the real specs, since
``filterable_fields``/``writable_fields``/``tier_allowlists`` are content
decisions for this lane, not Lane H's build).

Registers C12 ResourceSpec instances for four of System Design's owned node
types, all graph-primary (``table_name=None``, identity row in ``kg_nodes``)
with satellite tables reached via ``secondary_tables``:
  - DEVICE (system_design_device_capabilities + system_design_node_state)
  - PORT   (system_design_device_capabilities only)
  - RACK   (system_design_device_capabilities + system_design_node_state)
  - CABLE  (system_design_node_state only)

kg_nodes has no attribute column of its own (confirmed by reading every
existing writer, same finding Lane H's #307 docstring cites: case_study.py's
INSERT lists only label/entity_type/namespace_id; fl_tree.py parses name/kind
out of the label rather than reading a column) -- so the only kg_nodes-side
writable field for all four specs is ``change_origin``. Every other field
comes from a satellite table's real DDL (nce/schema.sql), never invented.

FIELD SPLIT BETWEEN DEVICE/PORT/RACK, DERIVED NOT GUESSED
-----------------------------------------------------------
system_design_device_capabilities carries NO per-node-type CHECK constraint
(unlike system_design_node_state's explicit DEVICE|RACK|CABLE-only CHECK) --
the DB itself does not restrict which node type may set which column. The
split below is not invented from domain intuition; it is read directly off
the two real, currently-shipping call sites that populate this table:

1. ``nce.vertical_modules.system_design.devices.author_device_topology``
   (its own docstring, devices.py:917-1004) documents the payload shape
   verbatim: a device's/rack's ``capability`` dict takes exactly
   ``device_category, manufacturer, model_number, power_draw_watts,
   heat_btu_hr, redundancy_role, extra`` -- and states RACK uses "same AVIXA
   param shape" as DEVICE, explicitly. A port's nested ``capability`` dict
   takes exactly ``signal_format, signal_version, port_direction, poe_class,
   poe_watts, dante_rx_channels, dante_tx_channels``. Ports never carry
   ``status``/``revision``/``salience`` "in any casing or prefix, at the top
   level or nested" -- the docstring's own words -- confirming PORT gets no
   ``system_design_node_state`` SecondaryTable, independently of the CHECK
   constraint reaching the same answer.
2. ``nce.vertical_modules.system_design.capability_sync._extract_capabilities_from_etim``
   (the ETIM/product-catalog ingestion path, a second real caller of the same
   ``_upsert_capability``) additionally writes ``poe_class``/``poe_watts``/
   ``dante_rx_channels``/``dante_tx_channels`` at the DEVICE level (a device's
   own aggregate PoE/Dante capacity, distinct from a specific port's), and
   ``manufacturer``/``model_number`` at the PORT level (denormalized from the
   parent device for a port row read standalone).

DEVICE's field list is the union of both real sources (11 device_capabilities
columns); RACK is documented as sharing DEVICE's shape but has no ETIM/PoE
ingestion path of its own anywhere in the codebase, so it gets exactly the
7-column ``author_device_topology`` set, not the union -- extending it further
would be inventing usage no real caller has. PORT's field list is the union
of its own two sources (9 columns) -- ``device_category``/``power_draw_watts``/
``heat_btu_hr``/``redundancy_role``/``extra`` never appear for a port in
either real caller, so PORT does not get them.

``devices.py:_upsert_capability`` itself enforces none of this split at the
SQL level (it accepts any of the 14 columns for any node_label) -- the split
here mirrors what the two real, shipping callers actually send, not a
DB-level guarantee. RACK legitimately writing to this table at all
(``devices.py:1108-1110``, ``rack.get("capability", {})``) contradicts a
simpler "RACK/CABLE are node_state-only" assumption floated before this
wave was built; verified directly against the real call sites rather than
carried over.

CABLE is the one node type with zero real callers of ``_upsert_capability``
anywhere in the codebase (grep confirmed) -- it gets no
``system_design_device_capabilities`` SecondaryTable at all, only
``system_design_node_state`` (the ``cable_status``/``cable_revision``/
``cable_salience``-prefixed keys in ``author_device_topology``'s
``connections`` parameter document this exact table for the CABLE node).

EXCLUDED, ON PURPOSE
---------------------
``system_design_geometry`` is not wired as a SecondaryTable for any of the
four (rack_position, cable_length_m, etc.). Its writer,
``nce.vertical_modules.system_design.geometry.validate_geometry()``, enforces
half-U rack_position granularity, an exact-precision float-max bound, and
rack_face/cable_type vocabularies no DB CHECK captures, and states outright:
validation lives in exactly one place "so a caller that reaches this function
directly ... cannot route around it. One place, not two." SecondaryTable has
no validation hook (its own docstring documents this restriction); adding
geometry fields here would be a second, unvalidated path into that table.

Archive/restore/bulk-create stay refused for all four (Lane H's #307,
mcp.py/rest.py): no generic soft-delete column exists anywhere in this
chain, and bulk-creating a kg_nodes identity row plus N secondary rows per
item raises a partial-failure question with no precedent in this generator.

NO enabled_guard: system_design has no ``_guard.py`` anywhere in
nce/vertical_modules/system_design/ (confirmed) -- no per-namespace opt-in
check exists for this engine, so #296's engine-guard ratchet does not apply.

tier_allowlists EXPLICITLY EMPTY, NOT OMITTED
-----------------------------------------------
Historical note, corrected: this section originally described a live
fail-open path (a caller-supplied ``X-NCE-Principal-Tier``/``X-NCE-Principal-
Kind`` header, read before ``request.state`` and trusted, plus an absent
``tier_allowlists`` key silently meaning "unfiltered" in ``redact_item``).
Both halves of that path are fixed as of the principal-tier-hardening wave:
``resolve_principal_tier()`` (resource_surface/rest.py) now reads only
``request.state.principal_kind`` and never a header, and ``redact_item``
treats an absent tier key as an explicit empty allowlist (deny), the same
as declaring it empty here. The explicit ``()`` entries below are therefore
now redundant with the dataclass default rather than a fix for it -- kept as
documentation that this is a considered "undecided, therefore nothing" for
these four node types, not an oversight.

REST ROUTE COLLISIONS: none. Grepped admin_app.py for
/api/system-design/{devices,ports,racks,cables} -- no hand-written route at
any of these paths; the existing system_design surface (topology authoring,
signal-flow inspection, capability sync, FL tree, designs, room specs) all do
more than generic CRUD and target different paths entirely.

FILTERABLE_FIELDS/SEARCHABLE_FIELDS FIX (dispatched separately, closing this
wave's own defect)
------------------------------------------------------------------------------
The four specs below originally declared filterable_fields/searchable_fields
naming secondary-table columns (device_category, manufacturer, model_number,
signal_format, port_direction, redundancy_role, status) that do not exist on
kg_nodes. The generated graph-primary list/search handler queries kg_nodes
directly and never joins secondary tables for filtering (rest.py's own
comment states the contract: "Filters and search apply to kg_nodes' own real
columns ... not the secondary tables") -- so a caller who actually used one
of those filters against a live Postgres-backed deployment would hit an
undefined-column SQL error, not a working filter. Invisible in CI because
the memory-store fallback (no pg_pool configured) does `.get(field, "")` and
silently fails to match instead of erroring, so no test ever ran a
graph-primary filter against real Postgres.

Trimmed to kg_nodes' own real columns (change_origin only -- the same set
CONTACT (sales/resources.py, #314) already uses correctly) rather than
adding a join, because the "no join" behaviour is the documented design
(rest.py:387-388), not an oversight: the graph get-by-id path already merges
every secondary row on read (rest.py's static get-by-id SQL), so satellite
data IS reachable -- filtering and searching on it, specifically, is not.
Declaring that gap correctly (an empty/minimal filter surface) is not the
same as silently deleting a working feature; each spec below states outright
that satellite fields are readable via get-by-id but not filterable, so a
future reader does not "fix" this by re-adding the same bad declarations.

spec.py's ``__post_init__`` now enforces this for every graph-scoped spec
(``tenant_scope == "graph"``), the same place table_name typos are already
caught -- a spec naming an unqueryable field fails at import, not at a
caller's first live filter.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec, SecondaryTable

# ---------------------------------------------------------------------------
# 1. DEVICE
# ---------------------------------------------------------------------------
DEVICE_SPEC = ResourceSpec(
    engine="system_design",
    entity="devices",
    node_type="DEVICE",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    # Trimmed to kg_nodes' own real columns -- see the module docstring's
    # "FILTERABLE_FIELDS/SEARCHABLE_FIELDS FIX" section. device_category,
    # manufacturer, redundancy_role, status and model_number are all
    # system_design_device_capabilities / system_design_node_state columns,
    # not kg_nodes columns; they were reachable through get-by-id (still
    # are) but not through a working list filter or search. change_origin
    # is the only kg_nodes column this spec has anything meaningful to
    # filter on.
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=(
        "change_origin",
        "device_category",
        "manufacturer",
        "model_number",
        "power_draw_watts",
        "heat_btu_hr",
        "redundancy_role",
        "poe_class",
        "poe_watts",
        "dante_rx_channels",
        "dante_tx_channels",
        "extra",
        "status",
        "revision",
        "salience",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_device_capabilities",
            join_field="node_label",
            fields=(
                "device_category",
                "manufacturer",
                "model_number",
                "power_draw_watts",
                "heat_btu_hr",
                "redundancy_role",
                "poe_class",
                "poe_watts",
                "dante_rx_channels",
                "dante_tx_channels",
                "extra",
            ),
        ),
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision", "salience"),
        ),
    ),
    # Explicitly empty, not omitted -- see the module docstring's
    # "tier_allowlists EXPLICITLY EMPTY" section: an absent entry is
    # fail-open on redact_item's live X-NCE-Principal-Tier header path.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description="C12 AV device: graph identity plus capability and lifecycle state.",
)
register_resource(DEVICE_SPEC)

# ---------------------------------------------------------------------------
# 2. PORT
# ---------------------------------------------------------------------------
PORT_SPEC = ResourceSpec(
    engine="system_design",
    entity="ports",
    node_type="PORT",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    # Trimmed to kg_nodes' own real columns -- see the module docstring's
    # "FILTERABLE_FIELDS/SEARCHABLE_FIELDS FIX" section. signal_format and
    # port_direction are system_design_device_capabilities columns, not
    # kg_nodes columns -- reachable through get-by-id, not through a
    # working list filter.
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=(
        "change_origin",
        "signal_format",
        "signal_version",
        "port_direction",
        "poe_class",
        "poe_watts",
        "dante_rx_channels",
        "dante_tx_channels",
        "manufacturer",
        "model_number",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_device_capabilities",
            join_field="node_label",
            fields=(
                "signal_format",
                "signal_version",
                "port_direction",
                "poe_class",
                "poe_watts",
                "dante_rx_channels",
                "dante_tx_channels",
                "manufacturer",
                "model_number",
            ),
        ),
        # No system_design_node_state SecondaryTable -- its own CHECK
        # constraint (system_design_node_state_status_per_node_type) refuses
        # PORT rows structurally, and author_device_topology's own docstring
        # states a port carrying a lifecycle key "is REFUSED rather than
        # silently ignored."
    ),
    # Explicitly empty, not omitted -- see DEVICE_SPEC's comment above.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description="C12 AV signal port: graph identity plus per-port capability, no lifecycle state.",
)
register_resource(PORT_SPEC)

# ---------------------------------------------------------------------------
# 3. RACK
# ---------------------------------------------------------------------------
RACK_SPEC = ResourceSpec(
    engine="system_design",
    entity="racks",
    node_type="RACK",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    # Trimmed to kg_nodes' own real columns -- see the module docstring's
    # "FILTERABLE_FIELDS/SEARCHABLE_FIELDS FIX" section. device_category,
    # manufacturer, status and model_number are all
    # system_design_device_capabilities / system_design_node_state
    # columns, not kg_nodes columns -- reachable through get-by-id, not
    # through a working list filter or search.
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=(
        "change_origin",
        "device_category",
        "manufacturer",
        "model_number",
        "power_draw_watts",
        "heat_btu_hr",
        "redundancy_role",
        "extra",
        "status",
        "revision",
        "salience",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_device_capabilities",
            join_field="node_label",
            fields=(
                "device_category",
                "manufacturer",
                "model_number",
                "power_draw_watts",
                "heat_btu_hr",
                "redundancy_role",
                "extra",
            ),
        ),
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision", "salience"),
        ),
    ),
    # Explicitly empty, not omitted -- see DEVICE_SPEC's comment above.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description="C12 rack enclosure: graph identity plus AVIXA capability and lifecycle state.",
)
register_resource(RACK_SPEC)

# ---------------------------------------------------------------------------
# 4. CABLE
# ---------------------------------------------------------------------------
CABLE_SPEC = ResourceSpec(
    engine="system_design",
    entity="cables",
    node_type="CABLE",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    # Trimmed to kg_nodes' own real columns -- see the module docstring's
    # "FILTERABLE_FIELDS/SEARCHABLE_FIELDS FIX" section. status is a
    # system_design_node_state column, not a kg_nodes column -- reachable
    # through get-by-id, not through a working list filter.
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=(
        "change_origin",
        "status",
        "revision",
        "salience",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_node_state",
            join_field="node_label",
            fields=("node_type", "status", "revision", "salience"),
        ),
        # No system_design_device_capabilities SecondaryTable -- zero real
        # callers anywhere in the codebase write a CABLE label through
        # devices.py's _upsert_capability (grep confirmed); AV capability
        # columns describe devices/ports/racks, never a cable run.
    ),
    # Explicitly empty, not omitted -- see DEVICE_SPEC's comment above.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description="C12 cable run: graph identity plus lifecycle state, no AV capability data.",
)
register_resource(CABLE_SPEC)

# ---------------------------------------------------------------------------
# 5. FUNCTIONAL_LOCATION
# ---------------------------------------------------------------------------
# Charter's C-1 asks for "kinds site|building|floor|room|desk|vessel" and an
# "as-built vs intent flag." Neither is a stored column, and neither can be
# routed through a SecondaryTable -- fl_tree.py's own module docstring
# ("Backed strictly by kg_nodes and kg_edges (no synthetic attribute tables
# invented). Kind derivation ... inferred from depth and naming.") and its
# actual code (fl_tree.py:100-104) confirm both are DERIVED at read time:
# `kind = row.get("kind") or derive_fl_kind(label, depth)` (label parsing +
# tree depth, not a column -- nothing populates a stored `kind` today, so it
# always falls to the derivation), and `as_built = change_origin ==
# "operator"`, a pure function of the one real column below. Declaring
# either as a satellite field would give the same fact a second, silently-
# divergent home -- the identical failure shape system_design_geometry's
# SecondaryTable exclusion already guards against ("cannot route around it.
# One place, not two."). A real functional_locations-equivalent attribute
# table (CONTACT's #314 pattern) does not exist under any name (grepped
# schema.sql and every migration) -- but that is not why this spec is thin;
# even if such a table existed, kind/as_built would still need exactly one
# home, and that home is fl_tree.py's own derivation, not a duplicate column.
#
# Second, independent blocker, now closed: entity="functional-locations"
# generates MCP tool system_design_list_functional_locations, which already
# exists as a hand-written tool (tree listing with recursion,
# admin_handlers/system_design.py). excluded_verbs={"list"} closes this the
# same way RESOURCE_SPEC does -- only list collides (hand-written get is
# singular system_design_get_functional_location).
#
# The existing hand-written tree-op routes (children/ancestors/path/move/
# merge) already serve kind/as_built correctly via fl_tree.py's real
# derivation logic and are NOT replaced or duplicated by this spec.
FUNCTIONAL_LOCATION_SPEC = ResourceSpec(
    engine="system_design",
    entity="functional-locations",
    node_type="FUNCTIONAL_LOCATION",
    table_name=None,
    id_field="label",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=("change_origin",),
    excluded_verbs=frozenset({"list"}),
    # Explicitly empty, not omitted -- see DEVICE_SPEC's comment above.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description=(
        "C12 functional location: graph identity only (label, entity_type, "
        "change_origin, timestamps). kind and as_built are derived at read "
        "time by fl_tree.py, not stored, and are not exposed here -- see "
        "the comment above this spec for why. list is excluded -- the "
        "existing hand-written system_design_list_functional_locations tool "
        "(tree-aware recursive listing) stays authoritative; this spec adds "
        "get/upsert/archive only. Tree navigation (children/ancestors/path/"
        "move/merge) stays on its own hand-written routes."
    ),
)
register_resource(FUNCTIONAL_LOCATION_SPEC)

# ---------------------------------------------------------------------------
# 6. DESIGN
# ---------------------------------------------------------------------------
# Charter's C-3 asks for "DESIGN spec listing per FL with is_active,
# POST /{id}/set-active." ``is_active`` IS real, stored data -- confirmed by
# reading design_versions.py directly, not assumed -- but it lives on
# ``system_design_geometry.meta`` (a JSONB column, e.g.
# ``jsonb_set(meta, '{is_active}', 'false'::jsonb)`` in
# ``do_set_active_design``). That is the SAME table ``SecondaryTable``'s own
# docstring forbids routing through: ``geometry.py``'s ``validate_geometry()``
# is the sole, mandatory choke point for every write to that table ("cannot
# route around it. One place, not two."), and it has no validation hook for
# a generic SecondaryTable write. Exposing ``is_active`` here would be a
# second, unvalidated path into ``system_design_geometry`` -- the identical
# reasoning that already excluded DEVICE/RACK/CABLE's ``rack_position``/
# ``cable_length_m`` above, now confirmed to also block DESIGN's one real
# field. Not this lane's to fix (would need a validation hook added to
# SecondaryTable itself, named as a separate wave, not absorbed here).
#
# So: identity only, same PROJECT_PROJECT/FUNCTIONAL_LOCATION precedent.
# ``is_active``/``set-active``/per-FL listing stay on the existing
# hand-written routes (``nce/admin_handlers/project.py``'s C-3 handlers,
# backed by ``design_versions.py``), which correctly enforce the
# single-active-per-FL invariant through ``validate_geometry()``'s own path
# -- not replaced or duplicated here.
#
# Second, independent blocker, also present: entity="designs" generates MCP
# tool system_design_list_designs, which already exists as a hand-written
# tool (nce/mcp_stdio_tools.py). get/upsert/archive do not collide
# (hand-written get is singular system_design_get_design; create/update are
# named differently from upsert; no hand-written archive exists at all).
# excluded_verbs={"list"} closes this the same way RESOURCE_SPEC/
# FUNCTIONAL_LOCATION_SPEC do.
DESIGN_SPEC = ResourceSpec(
    engine="system_design",
    entity="designs",
    node_type="DESIGN",
    table_name=None,
    id_field="label",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=("change_origin",),
    excluded_verbs=frozenset({"list"}),
    # Explicitly empty, not omitted -- see DEVICE_SPEC's comment above.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description=(
        "C12 solution design: graph identity only (label, entity_type, "
        "change_origin, timestamps). is_active is real, stored data (on "
        "system_design_geometry.meta) but is not exposed here -- see the "
        "comment above this spec for why (the sole validation choke point "
        "for that table has no SecondaryTable hook yet). Per-FL design "
        "listing and set-active stay on their own hand-written routes."
    ),
)
register_resource(DESIGN_SPEC)

# ---------------------------------------------------------------------------
# 7. DESIGN_REQUEST
# ---------------------------------------------------------------------------
# Charter's C-4 asks for DESIGN_REQUEST (SD-owned) with owner+status.
# Migration 104 (2026-09-20) gave this node type what CONTACT/097 already
# established for exactly this shape: kg_nodes identity (label, entity_type,
# change_origin, timestamps) plus a satellite table
# (system_design_design_requests) for the real fields, joined by node_label.
# Previously ran entirely on system_design_geometry's meta JSONB column --
# NOT routed through SecondaryTable (which has no validation hook, same
# restriction DEVICE/RACK/CABLE and DESIGN both hit above), and had no
# kg_nodes row at all. design_requests.py's own six functions (create/get/
# list/update/assign/complete) were rewritten in the same PR to read/write
# the new table instead -- confirmed by grep to be the ONLY code anywhere
# touching DESIGN_REQUEST storage, so no reader was left pointing at the old
# location. Existing rows (if any) are backfilled by migration 104 itself,
# not silently orphaned.
#
# Tool-name collision: entity="design_requests" generates
# system_design_list_design_requests, which already exists as a hand-written
# tool (nce/tool_registry.py) -- same exact-match-on-list shape as RESOURCE/
# FUNCTIONAL_LOCATION/DESIGN. get/upsert/archive do not collide (hand-written
# get is singular system_design_get_design_request; no hand-written upsert
# or archive tool exists for this node type). excluded_verbs={"list"} closes
# this the same way.
DESIGN_REQUEST_SPEC = ResourceSpec(
    engine="system_design",
    entity="design_requests",
    node_type="DESIGN_REQUEST",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    # Trimmed to kg_nodes' own real columns, same constraint DEVICE_SPEC's
    # docstring documents: a graph-primary spec's list/search handler queries
    # kg_nodes only and never joins secondary_tables, so a satellite-table
    # field here would be a runtime SQL error, not a working filter. Moot in
    # practice anyway -- list is excluded below, and the hand-written
    # system_design_list_design_requests tool already supports filtering on
    # status/priority/owner_id/quote_id/functional_location_id via real SQL
    # against the actual table.
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=(
        "change_origin",
        "title",
        "description",
        "quote_id",
        "functional_location_id",
        "status",
        "priority",
        "owner_id",
        "design_id",
        "room_spec",
        "metadata",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="system_design_design_requests",
            join_field="node_label",
            fields=(
                "title",
                "description",
                "quote_id",
                "functional_location_id",
                "status",
                "priority",
                "owner_id",
                "design_id",
                "room_spec",
                "metadata",
            ),
        ),
    ),
    excluded_verbs=frozenset({"list"}),
    # Explicitly empty, not omitted -- an absent key is fail-OPEN on
    # redact_item's live X-NCE-Principal-Tier header path (see DEVICE_SPEC's
    # comment for the full mechanism). This is an internal System Design
    # intake queue, not customer- or contractor-facing.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description=(
        "C12 solution design intake queue request: graph identity (label, "
        "entity_type, change_origin, timestamps) plus title/description/"
        "quote_id/functional_location_id/status/priority/owner_id/design_id/"
        "room_spec/metadata on the system_design_design_requests satellite "
        "table. list is excluded -- the existing hand-written "
        "system_design_list_design_requests tool stays authoritative; this "
        "spec adds get/upsert/archive only. Bulk create is refused, same as "
        "every other kg_nodes-primary spec in this module: identity-plus-"
        "satellite partial-failure semantics have no precedent here."
    ),
)
register_resource(DESIGN_REQUEST_SPEC)

__all__ = [
    "DEVICE_SPEC",
    "PORT_SPEC",
    "RACK_SPEC",
    "CABLE_SPEC",
    "FUNCTIONAL_LOCATION_SPEC",
    "DESIGN_SPEC",
    "DESIGN_REQUEST_SPEC",
]
