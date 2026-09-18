> **Status:** shipped · **Verified-against:** ca6a732 (mlv16f/f12-geodata-n50) · **Last-audited:** 2026-09-19

# Geodata FEED Module User Guide

> **Status:** shipped · **Verified-against:** ca6a732 (mlv16f/f12-geodata-n50) · **Last-audited:** 2026-09-19

The **Geodata FEED module** (`nce/vertical_modules/geodata/`) provides local, queryable stores for public geospatial reference data that describes the physical world rather than a tenant's own business data. It is a **feed module, not a tenant vertical engine**: every table it owns is GLOBAL (no `namespace_id`, RLS disabled) and every tool it mounts carries no `engine=` opt-in gate, the same reasoning already applied to `product_catalog` (migration 064).

This module currently owns **two** local stores, each its own batch-import + bounding-box-query pair, sharing the same box+GiST shape:

* **OpenStreetMap elements** (Lane F Wave F-11) — OSM nodes/ways/relations for Norway. §§2–4.
* **N50 land-cover** (Lane F Wave F-12) — Kartverket's land-cover classification for Norway (forest, bog, water, etc.). §§5–7.

---

## 1. Surface of Truth & Network Exposure

The Geodata module operates strictly through **4 MCP Tools** and **0 REST Routes**:

### 1.1 Mounted MCP Tools (4 Tools)
| MCP Tool | Cacheable | Mutation | Admin Only | AI-Role | Description |
|---|:---:|:---:|:---:|---|---|
| `geodata_import_osm_elements` | ✘ (`False`) | ✔ (`True`) | ✔ (`True`) | Operator | Upsert a batch of already-parsed OSM elements (nodes/ways/relations) into the local store. |
| `geodata_query_osm_elements` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Read every OSM element whose stored bounding box intersects a requested viewport. |
| `geodata_import_n50_land_cover` | ✘ (`False`) | ✔ (`True`) | ✔ (`True`) | Operator | Upsert a batch of already-parsed N50 land-cover features into the local store. |
| `geodata_query_n50_land_cover` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Read every N50 land-cover feature whose stored bounding box intersects a requested viewport. |

> [!NOTE]
> There are currently **0 REST routes** mounted for Geodata in `nce/admin_app.py`. All interactions take place via the MCP tools above.

---

## 2. What the OSM Store Does NOT Do

`geodata_import_osm_elements` accepts elements **already parsed into Overpass's own shape** (`{"type", "id", "tags"?, "geometry"?, "lat"?, "lon"?}`). Norway's real OSM extract is a country-scale `.osm.pbf` binary file (8.6M elements, hours of runtime, tens of GB) that requires a dedicated parser (`osmium`), a dependency this codebase does not carry — adding one is a bigger decision than this wave makes unilaterally. A caller feeding this tool a raw `.osm.pbf` file without first extracting it into Overpass's element shape will see every element silently counted under `skipped_no_geometry`, not an error.

---

## 3. Importing an OSM Batch (`geodata_import_osm_elements`)

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

## 4. Querying an OSM Viewport (`geodata_query_osm_elements`)

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
* Results are ordered by `osm_type, osm_id` (no area-based ranking — unlike the N50 land-cover store below, OSM elements carry no comparable size figure).
* `limit` defaults to and is capped at **500**. `truncated: true` means more elements intersected the viewport than were returned.
* Returned `elements` are in Overpass's own shape — a caller already speaking that shape needs no translation.

`geodata_osm_elements` (migration `086_geodata_osm_elements.sql`) is keyed on `(osm_type, osm_id)`; see §7 for storage/indexing shared by both stores.

---

## 5. What the N50 Store Does NOT Do

`geodata_import_n50_land_cover` accepts features **already parsed**: `rings` must already be `[lon, lat]` coordinate pairs in degrees, and `area_m2` (when present) must already be computed on the source's own projected coordinates. Kartverket's real N50 distribution is a `pg_dump`-shaped text file with EWKB-hex polygon geometry in UTM33 (EPSG:25833); parsing that raw format (byte-level EWKB decoding, plus a UTM→degrees coordinate transform) is a separate, out-of-band step this module deliberately does not implement. A caller feeding this tool a raw N50 extract without first decoding it will see every feature silently counted under `skipped_no_geometry`, not an error.

An accurate square-metre `area_m2` cannot be recovered from degree coordinates alone (they are not equal-area) — callers that omit `area_m2` get `null` back for that field rather than an approximation.

---

## 6. Importing and Querying N50 Land Cover

### 6.1 Importing a Batch (`geodata_import_n50_land_cover`)
```json
{
  "source_file": "n50-2026-09-oslo.sos",
  "features": [
    {
      "klasse": "Skog",
      "objid": 100234567,
      "objtype": "Løvskog",
      "rings": [[10.71, 59.91], [10.75, 59.91], [10.75, 59.95], [10.71, 59.95]],
      "area_m2": 1284000.0
    }
  ]
}
```
**Response:**
```json
{
  "ok": true,
  "source_file": "n50-2026-09-oslo.sos",
  "received": 1,
  "written": 1,
  "skipped_no_geometry": 0
}
```
Re-importing the same `(klasse, objid)` pair **updates** the existing row (`ON CONFLICT ... DO UPDATE`) rather than creating a duplicate — a later batch for the same feature always wins.

### 6.2 Querying a Viewport (`geodata_query_n50_land_cover`)
```json
{
  "min_lon": 10.70,
  "min_lat": 59.90,
  "max_lon": 10.80,
  "max_lat": 59.96,
  "klasse": "Skog",
  "limit": 100
}
```
**Response:**
```json
{
  "ok": true,
  "features": [
    {
      "klasse": "Skog",
      "objid": 100234567,
      "objtype": "Løvskog",
      "rings": [[10.71, 59.91], [10.75, 59.91], [10.75, 59.95], [10.71, 59.95]],
      "area_m2": 1284000.0
    }
  ],
  "count": 1,
  "truncated": false
}
```
* `klasse` is an optional exact-match filter; omit it to read every class in the viewport.
* Results are ordered **largest area first** (`area_m2 DESC NULLS LAST`) — when a read is capped, large features survive the cut, never the reverse. (The OSM store above has no equivalent ranking — it carries no comparable size figure.)
* `limit` defaults to and is capped at **500**. `truncated: true` means more features intersected the viewport than were returned.

`geodata_n50_land_cover` (migration `089_geodata_n50_land_cover.sql`) is keyed on `(klasse, objid)`.

---

## 7. Storage & Indexing (both stores)

Both `geodata_osm_elements` and `geodata_n50_land_cover` store a Postgres-native `box` column for their bounding box, read with the `&&` intersection operator against a **GiST** index — no PostGIS extension is present in this deployment's Postgres image, and none is needed for a bounding-box read.

Being GLOBAL tables, both are read and written through `nce.db_utils.unmanaged_pg_connection` rather than a tenant-scoped RLS session — sites `geodata.osm.import`/`geodata.osm.bbox_query` and `geodata.n50.import`/`geodata.n50.bbox_query` respectively, all four pre-registered in `UNMANAGED_PG_AUDITED_SITES`.

---

## 8. Attribution

* **OpenStreetMap** data is public and licensed **ODbL**.
* **Kartverket's N50** data is public and licensed **CC BY 4.0**.

A caller that displays either dataset to end users must show the corresponding attribution — this module stores and serves the data but does not own or enforce that attribution on any particular display.

---

> **Verified-against: ca6a732**
