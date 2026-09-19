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
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec

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
    filterable_fields=("status", "deal_id", "customer_id", "is_archived"),
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

__all__ = [
    "CUSTOMER_SPEC",
    "LEAD_SPEC",
    "DEAL_SPEC",
    "QUOTE_SPEC",
    "SIGNED_BASELINE_SPEC",
]
