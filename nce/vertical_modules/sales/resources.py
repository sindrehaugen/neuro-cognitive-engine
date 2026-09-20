"""nce.vertical_modules.sales.resources — Resource definitions for Sales Engine.

Lane B Wave B-1:
Registers C12 ResourceSpec instances for:
  - CUSTOMER (sales_customers table)
  - LEAD (sales_leads table)
  - DEAL (sales_deals table)
  - QUOTE (sales_quotes table)

Lane E (Lane E's C12 registration pass, post-B-1):
  - SIGNED_BASELINE (sales_signed_baselines table) -- the prior exemption text
    said "pending Wave B-4 / 132i-b freeze baseline wiring", but the table and
    its writer (do_freeze_baseline, sales/baseline.py) both already exist
    (grep -n "CREATE TABLE IF NOT EXISTS sales_signed_baselines" nce/schema.sql
    -> line 1680); append-only and immutable at the database grant level (the
    tenant-RLS bulk-grant block in schema.sql grants this table only SELECT,
    INSERT -- no UPDATE/DELETE -- alongside event_log/event_parents/
    divergence_log), so version_field/soft_delete_field are None, matching the
    economy POSTING precedent.

OPPORTUNITY and BOM_LINE stay exempted. OPPORTUNITY is still entity='opportunities'
inside the polymorphic sales_read_model multiplex table (grep -n "WHERE entity=.opportunities."
nce/schema.sql) -- CUSTOMER/LEAD/DEAL/QUOTE moved off that table via B-1, but
OPPORTUNITY has not, and ResourceSpec has no multi-entity-per-table support.
BOM_LINE is the cross-engine transition split, unaffected by any of this.

Lane H Wave B-2 (2026-09-20): CONTACT, a genuinely new node type (not a prior
exemption closed) -- kg_nodes-primary identity plus a new ``sales_contacts``
satellite table (migration 097), following the DEVICE/PORT/RACK/CABLE
multi-table pattern (nce/vertical_modules/system_design/resources.py) rather
than CUSTOMER/LEAD/DEAL/QUOTE's single-relational-table shape, since kg_nodes
has no attribute column of its own (same finding, every prior graph-primary
wave). ``CONTACT -> sales`` added to node-ownership.json under the same
carve-out ML-orch extended from Q-32 (a node-ownership row is a structural
fact about which engine owns a node type, not the tenant configuration Q-5
freezes). Deliberately NO customer_id/sales_customers FK on sales_contacts
(Q-46: nothing anywhere populates sales_customers from a resolved-identity
path yet; inventing that FK here would assert a relationship no caller has
built).

TWO THINGS THIS SPEC DELIBERATELY DOES NOT DO
------------------------------------------------
1. Bulk create. The dispatch describes "5 routes including bulk", but that
   is the HOST's inventory (what the host's contact surface does), not an
   NCE requirement -- the same host-inventory-vs-NCE-gap distinction that
   sank F-8/F-11/F-14/F-10/B-14/B-11 earlier this same session. Bulk create
   for a kg_nodes-primary spec (identity row + N secondary rows per item,
   partial-failure semantics: roll back all or report partial?) has no
   precedent anywhere in this generator (DEVICE/PORT/RACK/CABLE refuse it
   too, same reason, system_design/resources.py's own docstring). Shipping
   this spec closes 4 of the 5 routes the dispatch named; bulk stays
   refused, and this is a real, stated discrepancy against the dispatch's
   own "5 routes" framing -- not silently declared "done". Filed as a
   follow-on question with real stakes (roll-back-all vs. partial-report),
   not a TODO.
2. Create-time entity resolution. "C1-resolved on email+phone" describes a
   PROPERTY of the node type (email+phone are its C1 match fields), not a
   BEHAVIOUR the create path performs -- resolve() (nce/entity_resolution/
   resolver.py) states its own contract outright: "never auto-merges and
   never writes to any table -- read-only" (line 102). A resolve-then-create
   wrapper would not deduplicate anything; the best it could do is score
   candidates and queue them, which is a real, separate decision (reject the
   create? return the existing match? create and queue a merge candidate?
   three defensible answers, no default, and it would touch every
   kg_nodes-primary spec, not just this one) -- not built here. CONTACT is
   C1-*resolvable* (this generic create path does not deduplicate; a future
   caller can run resolve() against email/phone before calling create if it
   wants a duplicate check) rather than C1-resolved-on-write, matching the
   same "state what the surface does and does not do" honesty PROJECT_SPEC's
   description already uses.

filterable_fields/searchable_fields are declared ONLY against kg_nodes' own
real columns (change_origin), not sales_contacts' columns -- the generated
list handler for a graph-primary spec queries kg_nodes directly and never
joins secondary tables (rest.py's own comment: "Filters and search apply to
kg_nodes' own real columns ... not the secondary tables"). DEVICE/PORT/RACK
declare filterable_fields/searchable_fields naming secondary-table columns
(device_category, model_number, etc.) that do not exist on kg_nodes -- a
caller who actually uses those filters against a live list call would hit a
runtime SQL error (undefined column) rather than a working filter. Noticed
while modelling this spec on theirs; not this wave's table to fix (#311
already merged), filed separately rather than carried forward here.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec, SecondaryTable

# ---------------------------------------------------------------------------
# 1. CUSTOMER
# ---------------------------------------------------------------------------
CUSTOMER_SPEC = ResourceSpec(
    engine="sales",
    entity="customers",
    node_type="CUSTOMER",
    table_name="sales_customers",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("tier", "status", "is_archived"),
    searchable_fields=("name", "org_number", "email", "phone"),
    writable_fields=(
        "name",
        "org_number",
        "email",
        "phone",
        "billing_address",
        "shipping_address",
        "tier",
        "status",
        "metadata",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "name",
            "email",
            "phone",
            "shipping_address",
            "status",
            "created_at",
            "updated_at",
        ),
        "external-customer": (
            "id",
            "name",
            "email",
            "phone",
            "billing_address",
            "shipping_address",
            "created_at",
        ),
    },
    description="C12 customer master accounts with billing/shipping metadata and principal tier redaction.",
)
register_resource(CUSTOMER_SPEC)

# ---------------------------------------------------------------------------
# 2. LEAD
# ---------------------------------------------------------------------------
LEAD_SPEC = ResourceSpec(
    engine="sales",
    entity="leads",
    node_type="LEAD",
    table_name="sales_leads",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("source", "status", "customer_id", "is_archived"),
    searchable_fields=("title", "contact_name", "email", "phone", "company"),
    writable_fields=(
        "title",
        "customer_id",
        "contact_name",
        "email",
        "phone",
        "company",
        "source",
        "status",
        "estimated_value",
        "metadata",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "title",
            "contact_name",
            "company",
            "status",
            "created_at",
        ),
        "external-customer": (
            "id",
            "title",
            "status",
            "created_at",
        ),
    },
    description="C12 inbound and campaign sales leads with qualification status and estimated pipeline value.",
)
register_resource(LEAD_SPEC)

# ---------------------------------------------------------------------------
# 3. DEAL
# ---------------------------------------------------------------------------
DEAL_SPEC = ResourceSpec(
    engine="sales",
    entity="deals",
    node_type="DEAL",
    table_name="sales_deals",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("stage", "owner_slug", "customer_id", "lead_id", "is_archived"),
    searchable_fields=("title", "owner_slug"),
    writable_fields=(
        "title",
        "customer_id",
        "lead_id",
        "owner_slug",
        "stage",
        "value",
        "currency",
        "expected_close_date",
        "probability",
        "metadata",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "title",
            "stage",
            "expected_close_date",
            "created_at",
        ),
        "external-customer": (
            "id",
            "title",
            "stage",
            "created_at",
        ),
    },
    description="C12 pipeline deals with stage tracking, probability weighting, and expected close dates.",
)
register_resource(DEAL_SPEC)

# ---------------------------------------------------------------------------
# 4. QUOTE
# ---------------------------------------------------------------------------
QUOTE_SPEC = ResourceSpec(
    engine="sales",
    entity="quotes",
    node_type="QUOTE",
    table_name="sales_quotes",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("status", "deal_id", "customer_id", "is_archived", "billing_method"),
    searchable_fields=("quote_number", "title"),
    writable_fields=(
        "quote_number",
        "deal_id",
        "customer_id",
        "title",
        "version",
        "status",
        "total_ex_vat",
        "total_inc_vat",
        "currency",
        "valid_until",
        "metadata",
        # Wave B-5 (migration 099): billing_method in {"percent", "hours_amount"},
        # nullable -- see that migration's header for why no default is asserted.
        # No caller reads it yet (grep confirmed against commission.py); this is
        # the attribute the charter asked for, not a behavior change.
        "billing_method",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "quote_number",
            "title",
            "status",
            "created_at",
            "valid_until",
        ),
        "external-customer": (
            "id",
            "quote_number",
            "title",
            "status",
            "total_ex_vat",
            "total_inc_vat",
            "currency",
            "valid_until",
            "created_at",
            "billing_method",
        ),
    },
    description="C12 customer quotes with versioning, currency, VAT totals, and validity windows.",
)
register_resource(QUOTE_SPEC)

# ---------------------------------------------------------------------------
# 5. SIGNED_BASELINE
# ---------------------------------------------------------------------------
SIGNED_BASELINE_SPEC = ResourceSpec(
    engine="sales",
    entity="signed_baselines",
    node_type="SIGNED_BASELINE",
    table_name="sales_signed_baselines",
    id_field="id",
    version_field=None,
    soft_delete_field=None,
    filterable_fields=("quote_id",),
    searchable_fields=("quote_id",),
    writable_fields=(
        "quote_id",
        "signed_margin_pct",
        "signed_total_nok",
        "signed_at",
    ),
    description=(
        "Legally signed, immutable quote baseline (margin and total at signing time); "
        "append-only, no version/soft-delete field -- the database grants this table "
        "only SELECT and INSERT."
    ),
)
register_resource(SIGNED_BASELINE_SPEC)

# ---------------------------------------------------------------------------
# 6. CONTACT
# ---------------------------------------------------------------------------
CONTACT_SPEC = ResourceSpec(
    engine="sales",
    entity="contacts",
    node_type="CONTACT",
    table_name=None,
    id_field="node_label",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("change_origin",),
    searchable_fields=(),
    writable_fields=(
        "change_origin",
        "name",
        "email",
        "phone",
    ),
    secondary_tables=(
        SecondaryTable(
            table_name="sales_contacts",
            join_field="node_label",
            fields=("name", "email", "phone"),
        ),
    ),
    # Explicitly empty, not omitted -- an absent key is fail-OPEN on
    # redact_item's live X-NCE-Principal-Tier header path (see DEVICE_SPEC's
    # comment, system_design/resources.py, for the full mechanism). A
    # contact's email/phone are exactly the kind of PII an external-customer
    # or contractor caller must not see by default.
    tier_allowlists={"external-customer": (), "contractor": ()},
    description=(
        "C12 contact: graph identity (label, entity_type, change_origin, "
        "timestamps) plus name/email/phone on the sales_contacts satellite "
        "table. C1-resolvable on email+phone (a property of the node type) "
        "-- this create path does NOT perform create-time resolution or "
        "deduplication; resolve() is read-only and never auto-merges, so a "
        "caller wanting a duplicate check must run it separately before "
        "calling create. Bulk create is refused, same as every other "
        "kg_nodes-primary spec (DEVICE/PORT/RACK/CABLE): identity-plus-"
        "satellite partial-failure semantics have no precedent in this "
        "generator. No customer_id/sales_customers link (Q-46, open)."
    ),
)
register_resource(CONTACT_SPEC)


# Wave B-7 (2026-09-20): QUOTE_TEMPLATE, a genuinely new node type -- grepped
# for any existing quote-template infrastructure first, zero hits anywhere in
# nce/. Plain relational table (sales_quote_templates, migration 101), not
# kg_nodes-primary: a template is a static catalog record, not a graph
# identity. template_lines is JSONB (a draft's line items have no lifecycle
# of their own, unlike a live QUOTE's).
QUOTE_TEMPLATE_SPEC = ResourceSpec(
    engine="sales",
    entity="quote-templates",
    node_type="QUOTE_TEMPLATE",
    table_name="sales_quote_templates",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("is_archived",),
    searchable_fields=("name", "description"),
    writable_fields=("name", "description", "template_lines"),
    description=(
        "Reusable quote starting-point templates -- a named set of default "
        "line items a rep can start a new quote from."
    ),
)
register_resource(QUOTE_TEMPLATE_SPEC)

__all__ = [
    "CUSTOMER_SPEC",
    "LEAD_SPEC",
    "DEAL_SPEC",
    "QUOTE_SPEC",
    "SIGNED_BASELINE_SPEC",
    "CONTACT_SPEC",
    "QUOTE_TEMPLATE_SPEC",
]
