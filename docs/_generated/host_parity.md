<!-- GENERATED FILE. Do not hand-edit. -->
<!-- Source: docs/host_parity_seed.yaml -->
<!-- Regenerate: python scripts/gen_portal_parity.py -->

# Host-parity table

Every host-portal route family the v1.6 analysis found, generalised to a generic business capability (never a host product, company, or module/schema name), with its disposition and — for MOVE/MOVE+ rows — the NCE route or tool that replaces it. A family with no disposition is a RED finding, listed first.

## Status: no RED findings — every family has a disposition

## Summary

Total families: 51

| Disposition | Count |
|---|---|
| FEED | 3 |
| HOST | 4 |
| MOVE | 22 |
| MOVE+ | 15 |
| NEW | 5 |
| RETIRE | 2 |

## Families by category

### Delivery — Project/Procurement/Field/Resources

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F23 | `project_register_and_bom_lines` | Project register, bill-of-materials lines per project, project events, and building-project orders | MOVE | PROJECT resource layer; GET /api/project/{id}/bom-lines; events via C12 | Existing: convert_signed_quote, phase gates, my-day, capacity; building-project 'orders' route to Sales or Procurement decided per row |
| F24 | `time_entry_tracking` | Time entries per project/employee and milestone markers | MOVE | GET /api/field-tech/time-entries?project=... + approval, feeding Economy labour cost | Existing: field_tech_log_time (POST), time_entries table |
| F25 | `purchase_orders_and_distributor_status` | Purchase orders (list/detail/lines) and distributor order-status import | MOVE+ | PURCHASE_ORDER resource layer; distributor order status as a Procurement FEED | Existing: procurement_generate_po/submit_po, PO_LINE model, bid resolution |
| F26 | `work_order_lines` | Work-order update, close, and line-item reads | MOVE | WORK_ORDER resource layer | Existing: 10 tools / 12 routes with dual RLS |
| F27 | `responsible_person_assignment` | Responsible-person-per-location and employee-link edges | MOVE | RESPONSIBLE_FOR edge (HR/Sales) with a read route | Existing: HR employees, Resources allocations |

### Operations — Assets/Support/Inventory

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F28 | `installed_asset_register` | Installed-equipment register: per-location assets, duplicate merge, catalog linking, move/dismantle, lifecycle, service history, sub-components, placeholder ('shell') assets, person-assigned equipment | MOVE+ | ASSET resource layer + assets_move, assets_merge (C1 queue), assets_link_product (C1 confirm); EMPLOYEE-uses->ASSET; ASSET-part_of->ASSET; GET /api/assets/{id}/service-history | Existing: assets_get/list/advance_lifecycle/seed_from_bom/compute_health/check_warranty_eol/sync/generate_qr/record_failure_pattern/attach_sla, 14-state lifecycle |
| F29 | `asset_catalog_match_queue` | Asset-to-catalog matching bridge: candidates, proposals, auto-link, confirm, rebuild | MOVE | existing merge-queue machinery (C1), filtered by node type | No new logic required, only a filtered queue view |
| F30 | `device_telemetry_and_space_links` | Read-only hardware-vendor fleet telemetry (multiple vendors), plus external-space-to-location confirmed links | MOVE | per-vendor read-only telemetry adapters (Lane F) on the existing telemetry contract and cron; EXTERNAL_SPACE confirmed-link node | The external systems have working read-only clients today; NCE has the adapter socket and cron already |
| F31 | `support_case_management` | Support case handling, operations tickets, case-to-agreement/location links, on-call routing, per-step action log with outcomes | MOVE+ | TICKET_ACTION (append-only outcome log); support_summarise_ticket; on-call as a Resources allocation with role=on_call | Existing: Support engine, 13 tools / 15 routes |
| F32 | `notifications_and_reminders` | Cross-engine notifications and user-set reminders for events such as renewals, SLA breaches, and expirations | NEW | NOTIFICATION + REMINDER (shared core C13), produced by existing event selectors | No engine currently notifies a person of its own emitted events |
| F33 | `inventory_stock_reads` | Stock item list/detail reads | MOVE | resource-layer reads via C12 over the existing complete Inventory engine | Inventory engine itself is already complete (17/17) |

### Platform

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F34 | `hr_employee_lifecycle` | Employee profile, onboarding, absence, compliance, skills, and coaching functions | MOVE | existing HR engine (13 tools / 17 routes) absorbs these; identity/auth stays outside NCE | Pairs with the new principal-mapping capability (F35) for 'mine' reads |
| F35 | `principal_identity_mapping` | Resolving an authenticated caller to their employee/customer/contractor identity once, for every engine's 'mine' reads | NEW | GET /api/me/context (shared core C16), backed by a principal-bindings table | Identity/authentication itself remains outside NCE by design |
| F36 | `business_intelligence_composition` | Profitability, results, customer-analytics, and sales-overview dashboards | MOVE | composed in the Business Insights engine once the Economy read model (F14) exists | Existing: Business Insights engine, 6 tools / 7 routes |
| F37 | `marketing_case_studies` | Case studies and testimonials | MOVE | already covered by the Marketing engine | No gap |
| F38 | `customer_facing_portal_reads` | External customer-portal pages: documents, invoices, SLA status | MOVE | REST twins of the existing MCP-tool-only customer-portal reads | Existing: Customer Portal engine, 9 tools, plus a principal-context endpoint |

### Platform — CRM mirror

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F49 | `crm_system_of_record_mirror` | A large (100+ route) mirrored copy of company, opportunity, case, location, employee, and account records from an external CRM system, plus its own dashboards and sync status | RETIRE | every capability already has an NCE-owned home per families F01-F38; the mirror's tables become the one-time backfill source and are then dropped | Two exceptions stay and MOVE natively rather than retire: sales targets and account-knowledge data, which are authored in the mirror layer, not itself mirrored |

### Platform — decision item

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F51 | `floor_plan_digitisation` | Reading floor plans from raster/vector/document sources into rooms, desks, doors, zones, and fixtures, with training data and human correction | HOST |  | Recommended to remain outside NCE for v1.6, writing into the functional-location and design resource layers once they exist; revisit as a possible dedicated engine once those layers are stable. Must stop writing to any private schema either way |

### Platform — documents

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F39 | `document_register` | Per-location/asset/project/agreement document register backed by external cloud file storage, with tags and page regions | NEW | DOCUMENT node (shared core C14) + about-edges to FL/ASSET/PROJECT/AGREEMENT/TICKET, tags, expiring share tokens | Content stays in the existing external file-storage bridge; NCE owns only the register. Invariant: no code path deletes at the source |

### Platform — host-retained state

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F45 | `personal_ui_state` | Per-user favorites, drafts, spreadsheet drafts, and UI settings | HOST |  | Stays outside NCE for v1.6 (Q-33); revisit once principal mapping (F35) has run for a month |
| F46 | `operational_telemetry_and_spend_metering` | Internal repository/operations analytics and AI-usage spend metering | HOST |  | NCE already exposes usage quotas; the metering UX stays outside NCE, feeding NCE's own numbers |
| F47 | `credential_vault` | Stored credentials for external data-feed integrations | MOVE | the existing connector credential-save capability | Credentials for a FEED belong with the feed, not with the identity layer |
| F48 | `identity_provider_mapping` | Mapping between the external identity provider and application accounts | HOST |  | Identity and authentication remain outside NCE by design |

### Platform — integration proxy

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F50 | `backend_for_frontend_proxy` | A thin backend-for-frontend layer: HR stand-ins, an empty agreements stand-in, product/distributor passthroughs, search, identity context, and a module-library listing | MOVE+ | HR stand-ins -> HR engine; passthroughs -> Product search + Procurement order feed; search -> NCE search extended to entities; identity context -> F35 | After v1.6 this layer is authentication and identity-mapping only, nothing else |

### Platform — site & geodata

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F40 | `address_and_cadastre_identity` | Address validation and official building/cadastre identity resolution | NEW | SITE gains a cadastre id, validated address, coordinates (shared core C17); national address registry as a FEED | C1 uses the cadastre id as the functional-location building match key |
| F41 | `building_geometry_feed` | Building footprint, height, roof shape, solar exposure, and 3D rendering inputs | FEED | SITE geometry attributes fed by an external dataset; rendering derives from them |  |
| F42 | `local_gis_datasets` | Local geographic datasets: land cover, coastline, road network, place names, freshwater | FEED | a dedicated geodata FEED module: ingest scripts + bounding-box query routes (not tenant-scoped, public data) |  |
| F43 | `vessel_locations` | Vessels as a moving kind of location, tracked by a live position feed | MOVE+ | FUNCTIONAL_LOCATION of kind vessel, with position from a telemetry-adapter FEED |  |
| F44 | `weather_and_fx_feeds` | Weather conditions and foreign-exchange rates | FEED | a weather read route and an FX feed into pricing (F19) |  |

### Revenue — Agreements/Economy

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F09 | `contract_register` | Contract register mirrored from an external e-signature/CLM source: list/detail, import, verification, status | MOVE+ | AGREEMENT resource layer via C12 + AGREEMENT_PARTY, AGREEMENT_TEMPLATE | Existing: agreements_create/extract/review/upsert, GET /api/agreements[/id]; the external import path becomes a read-only FEED |
| F10 | `contract_ux_parity` | Contract address-book, archive, settings, renewal/notice calendar, templates | MOVE | AGREEMENT resource layer; calendar via a renewal-timeline read (planned) |  |
| F11 | `recurring_revenue_portfolio_management` | MRR portfolio views: leakage/health scoring, price rules, index-linked price adjustment, upsell actions, cleanup actions | MOVE+ | Economy contract-vs-ledger reconciliation (a third divergence pairing); PRICE_RULE + INDEX_SERIES (Agreements-owned) | Existing: Economy snapshot_mrr_arr_churn, compute_recognition_schedule, contract CPI-cap validation; Support health_score |
| F12 | `recurring_billing_run` | Ledger-first recurring billing: coverage lines per location, per-period billing candidates, generation runs, waivers | MOVE+ | Economy BILLING_RUN / BILLING_CANDIDATE (governed, confirm-first); SLA coverage line; WAIVER | Existing: economy_recognize_recurring (cron), compute_recognition_schedule, approve_invoice, generate_ehf |
| F13 | `customer_invoice_lifecycle` | Outbound customer invoice proposals, approval gate, export batch, receipt, VAT handling | MOVE+ | CUSTOMER_INVOICE resource (proposal -> approved -> exported -> paid); accounting system stays the legal system of record | Existing (supplier-side): economy_approve_invoice, match_invoice, generate_ehf, generate_kid |
| F14 | `accounting_read_model` | Accounting reads: profit & loss, KPI, trend, segment, product-margin, customer-from-ledger, group structure | MOVE | Economy read model (ECONOMY_PERIOD_BALANCE, ECONOMY_POSTING_LINE) over the existing ledger reader | Existing: economy_gl_sync_status, get_gl_records, forecast_cashflow, close_narrative |
| F15 | `national_business_registry_lookup` | National business-registry lookup: legal entity, group structure, roles | NEW | LEGAL_ENTITY register (shared core C15); national registry as a read-only FEED | C1 uses this identity as the strongest customer/vendor match key |

### Revenue — Sales

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F01 | `deal_pipeline` | Deal/opportunity pipeline with parties, participants, product groups, dual pricing, and a two-layer (signature + lifecycle) state model | MOVE+ | DEAL/QUOTE resource layer via C12 (A-1) + DEAL_PARTICIPANT/DEAL_TAG nodes; dealroom via do_open_dealroom | Existing NCE coverage: sales_create_deal, edit_deal, POST /api/sales/deals |
| F02 | `quote_authoring_and_lifecycle` | Quote authoring, cloning, light quotes, line import, AI-assisted drafting, billing method, certificates | MOVE | QUOTE resource layer via C12; sales_clone_quote, sales_import_lines | Existing: add/get_quote_line, draft_quote, freeze_baseline, signed_baseline, request_signature, project_convert_signed_quote |
| F03 | `public_facing_quote_actions` | Customer-facing quote view with accept/decline/comment actions via a signed public token | MOVE+ | POST /public-api/sales/quotes/{id}/{action} -> do_on_signed_callback / do_on_declined_callback | Existing: GET /public-api/sales/quotes/{id} (signed, rate-limited) |
| F04 | `quote_templates_and_personal_drafts` | Quote templates (tenant data) plus per-user spreadsheet drafts and ranking answers (personal UI state) | MOVE | QUOTE_TEMPLATE node via C12 | Templates MOVE; per-user drafts are HOST for v1.6 (Q-33) or a future C12 owner_principal scope |
| F05 | `package_templates_and_favorites` | Reusable equipment package templates, plus per-user favorites | MOVE | PACKAGE node (Product engine) via C12 | Existing: inventory_reserve_kit/release_kit. Favorites are HOST (personal UI state) |
| F06 | `customer_and_contact_master` | Customer and contact master data, customer sites, touchpoints, supplier deal registration | MOVE+ | CONTACT node (C1-resolved) + customer PATCH via C12; DEAL_REGISTRATION (Procurement/Agreements) | Existing: sales_create_customer, GET /api/sales/customers[/id], support_record_touchpoint |
| F07 | `handover_and_design_request_queues` | Sales-to-delivery handover queue and sales-to-design-request queue, plus milestone markers | MOVE+ | HANDOVER (Project-owned intake) and DESIGN_REQUEST (System Design-owned) | Existing: project_convert_signed_quote, system-design from_quote |
| F08 | `sales_analytics_read_model` | Sales dashboards, overview, per-seller and per-manager statistics, targets | RETIRE | existing 22-route Sales read model; feed via native writers, not the mirrored source | The read model itself already MOVEd; only its current single writer (a mirrored feed) retires |

### Revenue/Delivery — Product & Design

| ID | Family | Description | Disposition | Replacing NCE route/tool | Notes |
|---|---|---|---|---|---|
| F16 | `product_catalog_crud` | Product catalog CRUD, search, categories, manufacturers, source list, manual capture, duplicate handling | MOVE | PRODUCT resource layer via C12 + category/manufacturer reads + governed manual-capture endpoint | Existing: product_search/get/quality/golden_record/ingest_spec/enrich, C1 merge queue |
| F17 | `product_enrichment_workflow` | Enrichment coverage/gap analysis and accept/reject review queue | MOVE | review queue surfaced as a resource (accept/reject PATCH) | Existing: product_enrich, GET /api/product/enrichment/review, product_quality |
| F18 | `product_and_brand_media` | Product images, brand and manufacturer logos, image cache | MOVE+ | media links via the shared document register (C14); logo fetch as a FEED | Existing shared primitive: store_media |
| F19 | `pricing_supplier_and_fx` | Supplier prices, bid prices, price locks, foreign-exchange rates | MOVE | pricing reads via C6; PRICE_LOCK on QUOTE; FX as a FEED into pricing | Existing: pricing_resolve (C6), procurement bid-price resolution |
| F20 | `signal_and_standards_library` | Port/connector data, signal-distribution rules, cabling standards, certification and license catalog | MOVE | port data as a Product attribute consumed by System Design; standards/rules as config-as-IP read routes; licenses as a Product kind | Existing: system_design_device_capabilities, inspect_signal_flow, validate_design_graph |
| F21 | `functional_location_tree` | The functional-location hierarchy: address to building to floor to room to desk (and vessel), with duplicate folding, coordinates, per-node documents — the single most-used family in the whole estate | MOVE+ | FUNCTIONAL_LOCATION resource layer with children/ancestors/move/merge tree operations; ROOM_CATEGORY config; documents via C14 | Existing: functional-location authoring endpoint, per-design topology read |
| F22 | `design_versions_and_room_specs` | Per-location design/drawing versions with an active-version flag, saved solutions, room specification metadata | MOVE | DESIGN resource layer (list per location, versions, set-active); ROOM_SPEC as design metadata | Existing: topology + geometry + design-node-state revisions |

