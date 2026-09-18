> **Status:** shipped · **Verified-against:** 7f45587 (mlv16f/f11-geodata-osm) · **Last-audited:** 2026-09-18

# Geodata FEED Module User Guide

> **Status:** shipped · **Verified-against:** 7f45587 (mlv16f/f11-geodata-osm) · **Last-audited:** 2026-09-18

The **Geodata FEED module** (`nce/vertical_modules/geodata/`) provides local, queryable stores for public geospatial reference data that describes the physical world rather than a tenant's own business data. It is a **feed module, not a tenant vertical engine**: every table it owns is GLOBAL (no `namespace_id`, RLS disabled) and every tool it mounts carries no `engine=` opt-in gate, the same reasoning already applied to `product_catalog` (migration 064).

This guide covers the **OpenStreetMap element local store** (Lane F Wave F-11) — OSM nodes/ways/relations for Norway, imported as a batch and read back by bounding box.

> [!NOTE]
> Wave F-12 adds a second local store to this same `geodata` engine slug: an N50 land-cover store (`geodata_import_n50_land_cover` / `geodata_query_n50_land_cover`), following the identical box+GiST shape described below. Because `docs/engines/<engine>-user.md` is one file per engine rather than per store, whichever of F-11/F-12 merges second must extend this guide to cover both stores rather than overwrite it.

---

## 1. Surface of Truth & Network Exposure

The Geodata module operates strictly through **2 MCP Tools** and **0 REST Routes** at commit `7f45587`:

### 1.1 Mounted MCP Tools (2 Tools)
| MCP Tool | Cacheable | Mutation | Admin Only | AI-Role | Description |
|---|:---:|:---:|:---:|---|---|
| `geodata_import_osm_elements` | ✘ (`False`) | ✔ (`True`) | ✔ (`True`) | Operator | Upsert a batch of already-parsed OSM elements (nodes/ways/relations) into the local store. |
| `geodata_query_osm_elements` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Read every OSM element whose stored bounding box intersects a requested viewport. |

> [!NOTE]
> There are currently **0 REST routes** mounted for Geodata in `nce/admin_app.py`. All interactions take place via the MCP tools above.

---

## 2. What This Module Does NOT Do

`geodata_import_osm_elements` accepts elements **already parsed into Overpass's own shape** (`{"type", "id", "tags"?, "geometry"?, "lat"?, "lon"?}`). Norway's real OSM extract is a country-scale `.osm.pbf` binary file (8.6M elements, hours of runtime, tens of GB) that requires a dedicated parser (`osmium`), a dependency this codebase does not carry — adding one is a bigger decision than this wave makes unilaterally. A caller feeding this tool a raw `.osm.pbf` file without first extracting it into Overpass's element shape will see every element silently counted under `skipped_no_geometry`, not an error.

---

## 3. Importing a Batch (`geodata_import_osm_elements`)

```json
{
  "source_file": "norway-2026-09.osm.overpass.json",
  "elements": [
    {
      "type": "way",
      "id": 40123456,
      "tags": {"building": "yes", "name": "Oslo S"},
      "geometry": [
        {"lat": 59.9109, "lon": 10.7528},
        {"lat": 59.9112, "lon": 10.7534}
      ]
    }
  ]
}
```
**Response:**
```json
{
  "ok": true,
  "source_file": "norway-2026-09.osm.overpass.json",
  "received": 1,
  "written": 1,
  "skipped_no_geometry": 0
}
```

Re-importing the same `(osm_type, osm_id)` pair **updates** the existing row (`ON CONFLICT ... DO UPDATE`) rather than creating a duplicate — a later batch for the same element always wins.

---

## 4. Querying a Viewport (`geodata_query_osm_elements`)

```json
{
  "min_lon": 10.70,
  "min_lat": 59.90,
  "max_lon": 10.80,
  "max_lat": 59.95,
  "osm_type": "way",
  "limit": 100
}
```
**Response:**
```json
{
  "ok": true,
  "elements": [
    {
      "type": "way",
      "id": 40123456,
      "tags": {"building": "yes", "name": "Oslo S"},
      "geometry": [
        {"lat": 59.9109, "lon": 10.7528},
        {"lat": 59.9112, "lon": 10.7534}
      ]
    }
  ],
  "count": 1,
  "truncated": false
}
```

* `osm_type` is an optional exact-match filter (`node`/`way`/`relation`); omit it to read every type in the viewport.
* Results are ordered by `osm_type, osm_id` (no area-based ranking — unlike the N50 land-cover store, OSM elements carry no comparable size figure).
* `limit` defaults to and is capped at **500**. `truncated: true` means more elements intersected the viewport than were returned.
* Returned `elements` are in Overpass's own shape — a caller already speaking that shape needs no translation.

---

## 5. Storage & Indexing

`geodata_osm_elements` (migration `086_geodata_osm_elements.sql`) is keyed on `(osm_type, osm_id)` and stores a Postgres-native `box` column for its bounding box, read with the `&&` intersection operator against a **GiST** index — no PostGIS extension is present in this deployment's Postgres image, and none is needed for a bounding-box read.

Being a GLOBAL table, it is read and written through `nce.db_utils.unmanaged_pg_connection` (sites `geodata.osm.import` and `geodata.osm.bbox_query`, both pre-registered in `UNMANAGED_PG_AUDITED_SITES`) rather than a tenant-scoped RLS session.

---

## 6. Attribution

OpenStreetMap data is public and licensed **ODbL**. A caller that displays this data to end users must show OpenStreetMap attribution — this module stores and serves the data but does not own or enforce that attribution on any particular display.

---

> **Verified-against: 7f45587**
