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

NO tier_allowlists: admin_app.py (the ONLY place ``build_all_resource_routes()``
is mounted -- confirmed, single call site) is documented HMAC+mTLS,
employee/agent-only ("External principals (contractor, external-customer)
are never authenticated here", admin_app.py:55-57). ``ADMIN_PRINCIPAL_KIND``
is declared "employee" but never actually assigned to ``request.state``
anywhere in admin_app.py, and admin_app never imports nce.jwt_auth (the only
module that ever constructs a non-"employee" ``principal_kind``, used by the
separate a2a/portal-facing servers). ``resolve_principal_tier()``
(resource_surface/rest.py) therefore always falls through to its "employee"
default for every request reaching these routes -- a ``tier_allowlists``
entry for "contractor"/"external-customer" would be inert configuration for
a code path no legitimate request can reach, not a real redaction boundary.
The existing hand-written system_design surface (admin_handlers/
system_design.py, 2756 lines, grepped for tier/role/redact/external-customer/
contractor: zero matches) has no precedent for tier-based field redaction
either, consistent with this reading. Left empty rather than guessed.

REST ROUTE COLLISIONS: none. Grepped admin_app.py for
/api/system-design/{devices,ports,racks,cables} -- no hand-written route at
any of these paths; the existing system_design surface (topology authoring,
signal-flow inspection, capability sync, FL tree, designs, room specs) all do
more than generic CRUD and target different paths entirely.
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
    filterable_fields=("device_category", "manufacturer", "redundancy_role", "status"),
    searchable_fields=("model_number",),
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
    filterable_fields=("signal_format", "port_direction"),
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
    filterable_fields=("device_category", "manufacturer", "status"),
    searchable_fields=("model_number",),
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
    filterable_fields=("status",),
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
    description="C12 cable run: graph identity plus lifecycle state, no AV capability data.",
)
register_resource(CABLE_SPEC)

__all__ = [
    "DEVICE_SPEC",
    "PORT_SPEC",
    "RACK_SPEC",
    "CABLE_SPEC",
]
