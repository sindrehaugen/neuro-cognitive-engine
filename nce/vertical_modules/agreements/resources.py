"""nce.vertical_modules.agreements.resources — Resource definitions for Agreements Engine.

Lane B Wave B-9:
Registers C12 ResourceSpec instances for:
  - AGREEMENT (agreements table)
  - AGREEMENT_PARTY / AGREEMENT_SIGNATURE (agreement_parties table)
  - AGREEMENT_TEMPLATE (agreement_templates table)
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.agreements._guard import require_agreements_enabled

# ---------------------------------------------------------------------------
# 1. AGREEMENT
# ---------------------------------------------------------------------------
AGREEMENT_SPEC = ResourceSpec(
    engine="agreements",
    entity="agreements",
    node_type="AGREEMENT",
    table_name="agreements",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=(
        "status",
        "agreement_type",
        "customer_id",
        "deal_id",
        "auto_renewal",
        "is_archived",
    ),
    searchable_fields=("agreement_number", "title", "oneflow_contract_id"),
    writable_fields=(
        "agreement_number",
        "title",
        "customer_id",
        "deal_id",
        "status",
        "agreement_type",
        "start_date",
        "end_date",
        "auto_renewal",
        "notice_period_days",
        "annual_value",
        "monthly_value",
        "currency",
        "billing_frequency",
        "payment_terms_days",
        "oneflow_contract_id",
        "metadata",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "agreement_number",
            "title",
            "customer_id",
            "status",
            "agreement_type",
            "start_date",
            "end_date",
            "created_at",
            "updated_at",
        ),
        "external-customer": (
            "id",
            "agreement_number",
            "title",
            "status",
            "agreement_type",
            "start_date",
            "end_date",
            "auto_renewal",
            "billing_frequency",
            "payment_terms_days",
            "created_at",
        ),
    },
    description="C12 authoritative contract register with Oneflow mirror linkage, lifecycle state, and principal tier redaction.",
    enabled_guard=require_agreements_enabled,
)
register_resource(AGREEMENT_SPEC)

# ---------------------------------------------------------------------------
# 2. AGREEMENT_PARTY (AGREEMENT_SIGNATURE)
# ---------------------------------------------------------------------------
AGREEMENT_PARTY_SPEC = ResourceSpec(
    engine="agreements",
    entity="parties",
    node_type="AGREEMENT_SIGNATURE",
    table_name="agreement_parties",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=(
        "agreement_id",
        "party_type",
        "signature_status",
        "is_archived",
    ),
    searchable_fields=("party_name", "org_number", "signatory_name", "signatory_email"),
    writable_fields=(
        "agreement_id",
        "party_type",
        "party_name",
        "org_number",
        "signatory_name",
        "signatory_email",
        "signed_at",
        "signature_status",
        "signature_source",
        "role",
        "metadata",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "agreement_id",
            "party_type",
            "party_name",
            "role",
            "signature_status",
            "created_at",
        ),
        "external-customer": (
            "id",
            "agreement_id",
            "party_type",
            "party_name",
            "signatory_name",
            "signature_status",
            "signed_at",
        ),
    },
    description="C12 agreement parties and signatories linking counterparty identities to contract lifecycle.",
    enabled_guard=require_agreements_enabled,
)
register_resource(AGREEMENT_PARTY_SPEC)

# ---------------------------------------------------------------------------
# 3. AGREEMENT_TEMPLATE
# ---------------------------------------------------------------------------
AGREEMENT_TEMPLATE_SPEC = ResourceSpec(
    engine="agreements",
    entity="templates",
    node_type="AGREEMENT_TEMPLATE",
    table_name="agreement_templates",
    id_field="id",
    version_field="updated_at",
    soft_delete_field="is_archived",
    filterable_fields=("category", "is_active", "is_archived"),
    searchable_fields=("name", "code", "description"),
    writable_fields=(
        "name",
        "code",
        "category",
        "description",
        "default_terms",
        "sla_profile",
        "body_template",
        "is_active",
        "metadata",
    ),
    tier_allowlists={
        "contractor": (
            "id",
            "name",
            "code",
            "category",
            "description",
            "is_active",
            "created_at",
        ),
        "external-customer": (
            "id",
            "name",
            "category",
            "description",
            "is_active",
        ),
    },
    description="C12 standardized agreement clause packages, SLA profiles, and contract templates.",
    enabled_guard=require_agreements_enabled,
)
register_resource(AGREEMENT_TEMPLATE_SPEC)
