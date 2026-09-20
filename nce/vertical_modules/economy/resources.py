"""nce.vertical_modules.economy.resources — Resource definitions for Economy.

Lane E Wave E-7:
Registers the C12 ResourceSpec for Economy's owned node type in
node-ownership.json that has a real, tenant-scoped, single-table DDL:
  - POSTING (economy_postings table)

INVOICE, PERIOD, and MARGIN stay exempted. INVOICE and PERIOD are
kg_nodes-only stubs (no dedicated attribute table exists today -- see the
corrected exemptions.py entries; INVOICE's prior exemption text claimed a
non-existent "economy_invoices" table). MARGIN is a per-dimension node
(the margin-trinity pattern): Economy owns only the 'actual' transition, so
it is transition-split like BOM_LINE, not a whole-node Economy resource.

Economy resource-surface registration (2026-09-20; unrelated to the
struck B-14 warehouse-mirror wave -- see
_internal/work-docs/mlv16-orchestration/ECONOMY_SURFACE_DECISION.md):
Registers BILLING_RUN, BILLING_CANDIDATE, CUSTOMER_INVOICE, and the new
CONTRACT node type, all read-only (excluded_verbs={"upsert", "archive"}).
All four have real governed/documented sole writers
(do_generate_billing_run billing_runs.py:306, do_propose_customer_invoice
customer_invoices.py:162, do_upsert_contract contracts.py:381) that must
stay the only write path -- ResourceSpec.excluded_verbs reason (3)
("a governed actor already owns the write path"), not reason (2)
("storage itself permanently forbids the verb") the way economy_postings'
WORM grant will be once fixed; the DB grants on these four tables allow
the full verb set. Read-only registration closes a real visibility gap
(nothing could list/get any of these four via any surface before this) without
opening a write path the exemption comments these replace already said must
stay bespoke. economy_billing_candidate_lines and economy_bom_actual_costs
were investigated and declined/kept bespoke -- see the decision doc for the
per-table consumer evidence.
"""

from __future__ import annotations

from nce.resource_surface import register_resource
from nce.resource_surface.spec import ResourceSpec
from nce.vertical_modules.economy._guard import require_economy_enabled

# 1. POSTING
#
# excluded_verbs, found by this lane and independently confirmed by H's
# estate-wide GRANT sweep the same night -- three separate mechanisms, cited
# together rather than left for the next reader to rediscover:
#
# 1. "upsert" (bundles create+patch+bulk) -- reason (2): schema.sql:1899-1914
#    REVOKEs ALL then GRANTs only SELECT, INSERT to nce_app on
#    economy_postings ("Corrections must instead go through compensating
#    reversal postings... enforced structurally", schema.sql:1904-1911), so
#    patch (UPDATE) is permanently, mechanically forbidden -- the reason (2)
#    test is satisfied by that exact GRANT line, not an intention. Per
#    SIGNED_BASELINE_SPEC's own precedent (this field's docstring), "upsert"
#    can only be excluded as a whole even though only patch is grant-forbidden;
#    that is acceptable here for the same reason it was there -- create
#    (INSERT) alone has no named caller either: persist_financial_event
#    (graph.py:521) is the sole writer and nothing in the current tree calls
#    it, so excluding create alongside patch does not remove a working path.
# 2. The generic bulk-create route (rest.py's handle_bulk) issues one
#    single-row INSERT per item, not one multi-row statement, but
#    economy_postings' balance trigger (trg_economy_postings_assert_balanced,
#    schema.sql, AFTER INSERT ... FOR EACH STATEMENT) checks that an entire
#    statement's postings sum to zero. A real double-entry posting (a debit
#    line + a credit line for one event) submitted via generic bulk-create
#    would have the trigger fire once per line, each checking that one line's
#    amount alone sums to zero -- which it structurally cannot. This is
#    subsumed by excluding "upsert" above (upsert's REST mapping covers
#    create+patch+bulk together), not a separate exclusion, but recorded here
#    because it is an independent reason "upsert" must stay excluded even if
#    the grant were ever loosened.
# 3. "archive" -- fits none of the three documented reasons, stated plainly
#    rather than forced into one: soft_delete_field=None here, and
#    rest.py's handle_archive falls back to a literal "is_archived" column
#    (rest.py:524/1174/1270) that economy_postings does not have. Archiving
#    a leg of a balanced posting would also retroactively break the balance
#    invariant the trigger enforces, so there is no reasonable column to add
#    either. An operation with no target, not a permission question.
POSTING_SPEC = ResourceSpec(
    engine="economy",
    entity="postings",
    node_type="POSTING",
    table_name="economy_postings",
    id_field="id",
    version_field=None,
    soft_delete_field=None,
    filterable_fields=("event_id", "event_type", "account", "period_id"),
    searchable_fields=("event_id", "account"),
    writable_fields=(),
    excluded_verbs=frozenset({"upsert", "archive"}),
    description=(
        "Read-only view of balanced general-ledger posting lines behind the POSTING "
        "node; append-only WORM ledger (schema.sql:1899-1914 revokes UPDATE/DELETE), "
        "corrections go through compensating reversal postings, never PATCH or archive."
    ),
    enabled_guard=require_economy_enabled,
)
register_resource(POSTING_SPEC)

# 2. BILLING_RUN (read-only)
BILLING_RUN_SPEC = ResourceSpec(
    engine="economy",
    entity="billing-runs",
    node_type="BILLING_RUN",
    table_name="economy_billing_runs",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("status", "period_start", "period_end"),
    searchable_fields=(),
    writable_fields=(),
    # excluded_verbs reason (3): do_generate_billing_run (billing_runs.py:306)
    # is the already-shipped governed writer; this surface is read-only
    # visibility for rows it produces, not a placeholder for an unbuilt path.
    excluded_verbs=frozenset({"upsert", "archive"}),
    description=(
        "Read-only view of billing runs generated by the governed "
        "do_generate_billing_run core (billing_runs.py:306); write access stays "
        "exclusively through that governed action, never generic PATCH."
    ),
    enabled_guard=require_economy_enabled,
)
register_resource(BILLING_RUN_SPEC)

# 3. BILLING_CANDIDATE (read-only)
BILLING_CANDIDATE_SPEC = ResourceSpec(
    engine="economy",
    entity="billing-candidates",
    node_type="BILLING_CANDIDATE",
    table_name="economy_billing_candidates",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("status", "customer_id", "period_start", "period_end"),
    searchable_fields=(),
    writable_fields=(),
    # excluded_verbs reason (3): do_generate_billing_run (billing_runs.py:306)
    # is the already-shipped governed writer; this surface is read-only
    # visibility for rows it produces, not a placeholder for an unbuilt path.
    excluded_verbs=frozenset({"upsert", "archive"}),
    description=(
        "Read-only view of consolidated billing candidates generated by the "
        "governed do_generate_billing_run core (billing_runs.py:306); write access "
        "stays exclusively through that governed action, never generic PATCH."
    ),
    enabled_guard=require_economy_enabled,
)
register_resource(BILLING_CANDIDATE_SPEC)

# 4. CUSTOMER_INVOICE (read-only)
CUSTOMER_INVOICE_SPEC = ResourceSpec(
    engine="economy",
    entity="customer-invoices",
    node_type="CUSTOMER_INVOICE",
    table_name="economy_customer_invoices",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=(
        "status",
        "customer_id",
        "billing_candidate_id",
        "vat_rate_assumed",
    ),
    searchable_fields=("invoice_number",),
    writable_fields=(),
    # excluded_verbs reason (3): do_propose_customer_invoice
    # (customer_invoices.py:162) is the already-shipped governed writer; this
    # surface is read-only visibility for rows it produces, not a placeholder
    # for an unbuilt path.
    excluded_verbs=frozenset({"upsert", "archive"}),
    description=(
        "Read-only view of customer invoices generated by the governed "
        "do_propose_customer_invoice core (customer_invoices.py:162); write access "
        "stays exclusively through that governed action, never generic PATCH. "
        "vat_rate_assumed is deliberately filterable and always present in the "
        "response so a reviewer can tell an assumed VAT rate from a determined "
        "one -- dropping it from a read surface defeats the reason it exists."
    ),
    enabled_guard=require_economy_enabled,
)
register_resource(CUSTOMER_INVOICE_SPEC)

# 5. CONTRACT (read-only) -- not kg_nodes-primary, unlike 2-4 above; a
# standalone table natural-keyed on (namespace_id, contract_id), same shape
# as POSTING.
CONTRACT_SPEC = ResourceSpec(
    engine="economy",
    entity="contracts",
    node_type="CONTRACT",
    table_name="economy_contracts",
    id_field="id",
    version_field="updated_at",
    soft_delete_field=None,
    filterable_fields=("contract_id", "status", "next_renewal_date"),
    searchable_fields=("contract_id",),
    writable_fields=(),
    # excluded_verbs reason (3): do_upsert_contract (contracts.py:381) is the
    # already-shipped governed writer; this surface is read-only visibility
    # for rows it produces, not a placeholder for an unbuilt path.
    excluded_verbs=frozenset({"upsert", "archive"}),
    description=(
        "Read-only view of recurring-revenue contracts; write access stays "
        "exclusively through the documented sole writer do_upsert_contract "
        "(contracts.py:381), never generic PATCH. Read today only by two internal "
        "cron jobs (do_scan_renewals, the recurring-recognition tick) and the "
        "narrow do_validate_contract MCP tool; this closes the gap where no "
        "surface could list a namespace's contracts."
    ),
    enabled_guard=require_economy_enabled,
)
register_resource(CONTRACT_SPEC)
