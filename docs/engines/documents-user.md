# Documents Engine User Guide

## 1. Purpose

The Documents engine is NCE's C14 master document register: a single, cross-engine
place to record metadata and storage references for files attached to any node in
the graph (a quote, a project, a service ticket, an asset, and so on). It does not
store file bytes itself; `sha256` and `file_size_bytes` are recorded for integrity
and dedup checks against wherever the bytes actually live (object storage), and
`document_ref`/`source_kind` point at that location.

Wave A-4 also added generic document-attachment routes
(`nce/vertical_modules/documents/service.py::link_document`/`unlink_document`/
`list_entity_documents`) directly into the shared C12 REST framework
(`nce/resource_surface/rest.py`), so **every** C12 resource — not just this
engine — gets `GET`/`POST .../{id}/documents` and `DELETE
.../{id}/documents/{doc_id}` routes for free, all backed by this engine's
`documents` table. That is why this guide covers two things: this engine's own
resource surface (§2), and the cross-cutting attachment mechanism every other
engine relies on (also §2.4).

## 2. Resource Surface (C12, Wave A-4)

Documents is generated entirely from one declarative `ResourceSpec`
(`nce/vertical_modules/documents/resources.py::DOCUMENT_SPEC`) — there is no
hand-written business logic in this engine. Every REST route, MCP tool, filter,
search field, and tier redaction below is produced by the shared C12 framework
(`nce/resource_surface/`), not written per-engine.

### 2.1 Fields

| Field | Role |
|---|---|
| `title`, `document_ref` | Identity / display |
| `source_kind`, `document_kind`, `file_name`, `mime_type` | Classification |
| `file_size_bytes`, `sha256` | Integrity / dedup against the object store |
| `tags`, `metadata` | Caller-defined, unstructured |
| `archived` | Soft-delete flag (`soft_delete_field`) |

Filterable: `document_kind`, `source_kind`, `mime_type`, `archived`.
Full-text searchable (`?q=`): `title`, `file_name`, `document_ref`.

### 2.2 Principal Tier Redaction

| Tier | Visible fields |
|---|---|
| `contractor` | Everything except `source_kind`'s raw storage detail beyond `document_ref` — i.e. all fields except internal-only ones. |
| `external-customer` | `id`, `title`, `document_kind`, `file_name`, `mime_type`, `created_at` only. |
| Everyone else (internal/employee) | Full record. |

### 2.3 MCP Tools (4)

| Tool Name | Cacheable | Mutation | Description |
|---|:---:|:---:|---|
| `documents_list_documents` | ✔ | ✘ | List/query documents for the caller's namespace, with the filters above. |
| `documents_get_documents` | ✔ | ✘ | Fetch a single document by ID. |
| `documents_upsert_documents` | ✘ | ✔ | Create or update a document record (metadata only, not the file bytes). |
| `documents_archive_documents` | ✘ | ✔ | Soft-archive a document record. |

None of the four are `admin_only` — any authenticated principal within tier
redaction rules may call them.

### 2.4 REST Routes (16)

All mounted under `/api/documents/documents` by `nce.resource_surface.rest`.
The last three (`.../documents`, `.../documents/{doc_id}`) are the generic C12
document-attachment routes (Wave A-4) that every resource surface gets, not
specific to this engine — here they let a document register entry itself be
attached to other documents, which is unusual but not prevented.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/documents/documents` | List |
| `POST` | `/api/documents/documents` | Create |
| `POST` | `/api/documents/documents/bulk` | Bulk create |
| `GET` | `/api/documents/documents/{id}` | Get |
| `PATCH` | `/api/documents/documents/{id}` | Update |
| `POST` | `/api/documents/documents/{id}/archive` | Archive |
| `POST` | `/api/documents/documents/{id}/restore` | Restore |
| `GET` | `/api/documents/documents/{id}/events` | Event history |
| `GET` / `POST` | `/api/documents/documents/{id}/comments` | List / add comment |
| `GET` / `POST` | `/api/documents/documents/{id}/tags` | List / add tag |
| `DELETE` | `/api/documents/documents/{id}/tags/{tag}` | Remove tag |
| `GET` / `POST` | `/api/documents/documents/{id}/documents` | List / attach a document |
| `DELETE` | `/api/documents/documents/{id}/documents/{doc_id}` | Detach a document |

### 2.5 Storage and Tenancy

`table_name="documents"`, a tenant-scoped table (`tenant_scope == "tenant"`,
derived from `EXPECTED_TENANT_RLS_TABLES` in `nce/event_log.py`) — every read and
write is isolated to the caller's `namespace_id` by Postgres RLS, enforced the
same way as every other C12 resource.

## 3. What this engine deliberately does not do

It does not manage file upload/download itself (no presigned-URL minting, no
storage-provider integration) — that is out of scope for C14 as specified; this
engine is the metadata register other engines and the host point at, not a file
service.
