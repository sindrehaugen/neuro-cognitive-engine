# Sites Engine User Guide

## 1. Purpose

The Sites engine is NCE's C17 Site Master Data repository: a centralized, multi-tenant
register for physical geographic sites, installations, vessels, and campus locations.
It links physical cadastre identifiers, geolocations, building heights, footprints,
and vessel telemetry streams into the enterprise knowledge graph, serving as the master
anchor for Functional Location (FL) trees (Lane C) and geodata enrichment (Lane F).

## 2. Resource Surface (C12, Wave A-9)

Sites is declared via `ResourceSpec` (`nce/vertical_modules/sites/resources.py::SITE_SPEC`)
and mounted under the shared C12 REST and MCP framework (`nce/resource_surface/`).

### 2.1 Fields

| Field | Role |
|---|---|
| `id`, `cadastre_id`, `name` | Primary identity and physical cadastre identifier |
| `address` | Validated street, postal code, city, country |
| `latitude`, `longitude`, `height_meters` | Spatial coordinates and elevation/height |
| `footprint_geometry` | Polygons conforming to standard GeoJSON geometry |
| `telemetry_stream` | Vessel or live site telemetry configuration |
| `metadata` | Tenant-defined arbitrary attributes |
| `archived` | Soft-delete flag (`soft_delete_field`) |

Filterable: `cadastre_id`, `name`, `archived`.
Full-text searchable (`?q=`): `name`, `cadastre_id`.

### 2.2 Principal Tier Redaction

| Tier | Visible fields |
|---|---|
| `contractor` | `id`, `name`, `address`, `latitude`, `longitude`, `height_meters`, `footprint_geometry`, `created_at` |
| `external-customer` | `id`, `name`, `address`, `created_at` |
| Everyone else (internal / employee) | Full record including telemetry stream and raw metadata |

### 2.3 MCP Tools (4)

| Tool Name | Cacheable | Mutation | Description |
|---|:---:|:---:|---|
| `sites_list_sites` | ✔ | ✘ | List and search sites in the caller's namespace. |
| `sites_get_sites` | ✔ | ✘ | Retrieve a single site record by ID. |
| `sites_upsert_sites` | ✘ | ✔ | Create or update a site master record. |
| `sites_archive_sites` | ✘ | ✔ | Soft-archive a site record. |

None of the tools are `admin_only` — any authenticated principal within tier permissions may call them.

### 2.4 REST Routes (16)

Mounted under `/api/sites/sites` by `nce.resource_surface.rest`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sites/sites` | List sites |
| `POST` | `/api/sites/sites` | Create site |
| `POST` | `/api/sites/sites/bulk` | Bulk create sites |
| `GET` | `/api/sites/sites/{id}` | Get site by ID |
| `PATCH` | `/api/sites/sites/{id}` | Update site fields |
| `POST` | `/api/sites/sites/{id}/archive` | Soft-archive site |
| `POST` | `/api/sites/sites/{id}/restore` | Restore archived site |
| `GET` | `/api/sites/sites/{id}/events` | Site event history |
| `GET` / `POST` | `/api/sites/sites/{id}/comments` | List / add comments |
| `GET` / `POST` | `/api/sites/sites/{id}/tags` | List / add tags |
| `DELETE` | `/api/sites/sites/{id}/tags/{tag}` | Remove tag |
| `GET` / `POST` | `/api/sites/sites/{id}/documents` | List / attach documents |
| `DELETE` | `/api/sites/sites/{id}/documents/{doc_id}` | Detach document |

### 2.5 Storage and Tenancy

Backed by the `sites` table in PostgreSQL with multi-tenant row-level security (`tenant_isolation_policy`).
`sites` is registered in `EXPECTED_TENANT_RLS_TABLES` (`nce/event_log.py`) and strictly partitioned by `namespace_id`.

## 3. Entity Resolution & Functional Locations (C1 Hook)

When functional location buildings or imported assets report cadastre identifiers,
the C1 entity resolution hook (`nce/entity_resolution/site_hook.py::reconcile_fl_building_with_site`)
verifies cadastre matches. In accordance with safety policies, when two buildings share the same
cadastre ID, they are enqueued into `entity_merge_queue` with status `'pending'` for human review
and are never automatically merged.
