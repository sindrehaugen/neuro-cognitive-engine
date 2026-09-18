# Legal Entities Engine User Guide

## 1. Purpose

The Legal Entities engine is NCE's C15 master legal-entity register: a
single, cross-engine place to record company identities (by `org_nr`), their
group/parent structure, and the roles they hold (e.g. customer, supplier,
partner) across the estate. Other engines reference a legal entity by its
node rather than duplicating company-identity fields locally.

## 2. Resource Surface (C12, Wave A-5)

Legal Entities is generated entirely from one declarative `ResourceSpec`
(`nce/vertical_modules/legal_entities/resources.py::LEGAL_ENTITY_SPEC`) —
there is no hand-written MCP tool or REST route in this engine beyond what
the shared C12 framework (`nce/resource_surface/`) produces.

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

### 2.3 MCP Tools (4)

| Tool Name | Cacheable | Mutation | Description |
|---|:---:|:---:|---|
| `legal_entities_list_legal_entities` | ✔ | ✘ | List/query legal entities for the caller's namespace, with the filters above. |
| `legal_entities_get_legal_entities` | ✔ | ✘ | Fetch a single legal entity by ID. |
| `legal_entities_upsert_legal_entities` | ✘ | ✔ | Create or update a legal entity. |
| `legal_entities_archive_legal_entities` | ✘ | ✔ | Soft-archive a legal entity. |

None of the four are `admin_only`.

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

## 3. What this engine deliberately does not do

It does not resolve or deduplicate legal entities against an external
company registry (e.g. Brønnøysundregistrene) — `org_nr` is recorded as
given by the caller, not verified or enriched. It also does not enforce that
`group_parent_org_nr` points at an entity that actually exists in this
register; that referential integrity, if needed, is the caller's
responsibility.
