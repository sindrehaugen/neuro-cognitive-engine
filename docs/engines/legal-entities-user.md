# Legal Entities Engine User Guide

## 1. Purpose

The Legal Entities engine is NCE's C15 master legal-entity register: a
single, cross-engine place to record company identities (by `org_nr`), their
group/parent structure, and the roles they hold (e.g. customer, supplier,
partner) across the estate. Other engines reference a legal entity by its
node rather than duplicating company-identity fields locally.

## 2. Resource Surface (C12, Wave A-5)

Most of Legal Entities is generated from one declarative `ResourceSpec`
(`nce/vertical_modules/legal_entities/resources.py::LEGAL_ENTITY_SPEC`).
One hand-written tool exists beyond what the shared C12 framework
(`nce/resource_surface/`) produces — the national business registry feed,
§2.6 below (Lane F Wave F-8).

### 2.1 Fields

| Field | Role |
|---|---|
| `org_nr`, `name` | Identity |
| `group_parent_org_nr` | Parent entity in a corporate group structure, by `org_nr` |
| `roles` | The relationships this entity holds (e.g. customer, supplier) |
| `country` | Jurisdiction |
| `metadata` | Caller-defined, unstructured |
| `archived` | Soft-delete flag (`soft_delete_field`) |

Filterable: `org_nr`, `name`, `country`, `group_parent_org_nr`, `archived`.
Full-text searchable (`?q=`): `name`, `org_nr`, `group_parent_org_nr`.

### 2.2 Principal Tier Redaction

| Tier | Visible fields |
|---|---|
| `contractor` | Everything (identity, group structure, roles, country, metadata). |
| `external-customer` | `id`, `org_nr`, `name`, `country`, `created_at` only — group structure and role assignments are internal. |
| Everyone else (internal/employee) | Full record. |

### 2.3 MCP Tools (5)

| Tool Name | Cacheable | Mutation | Admin Only | Description |
|---|:---:|:---:|:---:|---|
| `legal_entities_list_legal_entities` | ✔ | ✘ | ✘ | List/query legal entities for the caller's namespace, with the filters above. |
| `legal_entities_get_legal_entities` | ✔ | ✘ | ✘ | Fetch a single legal entity by ID. |
| `legal_entities_upsert_legal_entities` | ✘ | ✔ | ✘ | Create or update a legal entity. |
| `legal_entities_archive_legal_entities` | ✘ | ✔ | ✘ | Soft-archive a legal entity. |
| `legal_entities_enrich_from_registry` | ✘ | ✔ | ✔ | Pull one entity's company data from Norway's BRREG registry into its metadata (§2.6). |

The four C12 resource-surface tools are not `admin_only`; the registry-feed tool is (operator/cron pull against an external system, same reasoning as `assets_pull_telemetry`).

### 2.4 REST Routes (16)

All mounted under `/api/legal_entities/legal-entities` by
`nce.resource_surface.rest`, including the generic C12 document-attachment
routes (Wave A-4) every resource surface gets.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/legal_entities/legal-entities` | List |
| `POST` | `/api/legal_entities/legal-entities` | Create |
| `POST` | `/api/legal_entities/legal-entities/bulk` | Bulk create |
| `GET` | `/api/legal_entities/legal-entities/{id}` | Get |
| `PATCH` | `/api/legal_entities/legal-entities/{id}` | Update |
| `POST` | `/api/legal_entities/legal-entities/{id}/archive` | Archive |
| `POST` | `/api/legal_entities/legal-entities/{id}/restore` | Restore |
| `GET` | `/api/legal_entities/legal-entities/{id}/events` | Event history |
| `GET` / `POST` | `/api/legal_entities/legal-entities/{id}/comments` | List / add comment |
| `GET` / `POST` | `/api/legal_entities/legal-entities/{id}/tags` | List / add tag |
| `DELETE` | `/api/legal_entities/legal-entities/{id}/tags/{tag}` | Remove tag |
| `GET` / `POST` | `/api/legal_entities/legal-entities/{id}/documents` | List / attach a document |
| `DELETE` | `/api/legal_entities/legal-entities/{id}/documents/{doc_id}` | Detach a document |

### 2.5 Storage and Tenancy

`table_name="legal_entities"`, a tenant-scoped table (`tenant_scope ==
"tenant"`, derived from `EXPECTED_TENANT_RLS_TABLES` in `nce/event_log.py`)
— every read and write is isolated to the caller's `namespace_id` by
Postgres RLS.

### 2.6 National Business Registry Feed (`legal_entities_enrich_from_registry`, Lane F Wave F-8)

Given an entity that already exists, this tool looks up its own `org_nr` in
Norway's public **Brønnøysundregistrene (BRREG) Enhetsregisteret** (free, no
auth) and merges company-level fields into the entity's `metadata`, under a
`brreg_`-prefixed key set (name, NACE industry code, organisation form,
address, deregistration/bankruptcy flags).

```json
{"namespace_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "entity_id": "7a8b9c0d-1e2f-3a4b-5c6d-7e8f90a1b2c3"}
```
Fails soft (`{"matched": false, "reason": "not_found_in_registry" | "registry_unreachable"}`) rather than erroring when BRREG has no record or is unreachable — `metadata` is left untouched in that case. The lookup is always by the entity's own stored `org_nr`, never a caller-supplied one, so a caller cannot enrich one entity with another's data.

**This tool never touches `roles`.** NCE's `roles` field (§2.1) is a small
tenant-chosen relationship tag (customer/supplier/partner). BRREG's own
"roller" means something entirely different — government-registered
*personal* signatory roles (CEO, board members, sole-proprietor owner),
sourced from a feed that also carries each role-holder's name and
birthdate. This tool never fetches or stores any of that; see
`nce/vertical_modules/legal_entities/brreg_feed.py`'s module docstring for
the full reasoning.

It also does not do a bulk/nightly mirror of the national register (the
host's own integration pattern for this data) — only a single on-demand
`org_nr` lookup per call, the same shape as the read-only vendor telemetry
adapters (Waves F-1..F-7).

## 3. What this engine deliberately does not do

It does not *resolve or deduplicate* legal entities against the registry —
`org_nr` is recorded as given by the caller and is never overwritten or
validated by the feed above; §2.6's enrichment only adds registry data
alongside it, and only into `metadata`, never into `org_nr` itself. It also
does not enforce that `group_parent_org_nr` points at an entity that
actually exists in this register; that referential integrity, if needed, is
the caller's responsibility.
