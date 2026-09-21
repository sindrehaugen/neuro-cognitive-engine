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

## 3. Entity Resolution & Functional Locations

A cadastre-ID reconciliation hook was built for this purpose (Wave A-9) but was never wired into
any real request path — nothing in the codebase called it outside its own tests. Removed as dead
code 2026-09-21 (`DEAD_HAND_WRITTEN_SERVICE_LAYERS_2026-09-21.md`). Functional-location buildings
and imported assets do not currently get automatic cadastre-match reconciliation against
`entity_merge_queue`; building this for real needs a live caller, not just the hook function.

## 4. Address Registry Feed (MLV16 Lane F, Wave F-9)

`sites_enrich_address_from_registry` (`nce/vertical_modules/sites/address_registry.py`) validates/geocodes an existing site's address against Kartverket's public Adresse API (Geonorge, against the cadastre — free, no authentication) and merges the structured result into that same site's own `address`/`latitude`/`longitude` fields. **No new table** — this feed writes into `sites` (migration 095, Wave A-9), the same shape as the C15 BRREG feed (Wave F-8) writing into `legal_entities.metadata` rather than owning a store of its own.

```json
{"namespace_id": "...", "site_id": "..."}
```
**Response (matched):**
```json
{
  "ok": true,
  "site_id": "...",
  "matched": true,
  "reason": null,
  "address": {
    "address_key": "0301-12345-1",
    "formatted_address": "Storgata 1",
    "postal_code": "0155",
    "city": "OSLO",
    "municipality": "OSLO",
    "municipality_number": "0301",
    "lat": 59.913,
    "lon": 10.752
  }
}
```

* By default the lookup query is built from the site's own stored `address` fields (`formatted_address`, or `street`/`postal_code`/`city` assembled together). An optional `query` argument overrides this — useful when registering a brand-new site whose address has never been validated yet.
* On a match, the site's `address` is merged (not replaced) with the validated `formatted_address`/`postal_code`/`city` and `is_validated: true`, and `latitude`/`longitude` are set from Kartverket's own coordinate for that address.
* **Fails soft, never fabricates:** a 0-hit search or a Kartverket outage returns `{"matched": false, "reason": "not_found_in_registry"}` and leaves the site's row untouched — never a guessed address or coordinate.
* `admin_only` mutation (operator/cron pull against an external registry), mirroring `legal_entities_enrich_from_registry`'s own reasoning — not an ordinary Actor action, and not gated behind an `engine=` opt-in (same as that tool).
* This module deliberately does not re-implement the host's own freetext-matching complexity (street-abbreviation expansion, house-number-range collapsing, transposed-letter tolerance): that solves matching a messy, decades-old CRM location name against the cadastre, which a `sites` row's own caller-supplied address does not need.
