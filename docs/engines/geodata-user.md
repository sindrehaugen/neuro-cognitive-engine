> **Status:** shipped · **Verified-against:** PLACEHOLDER_SHA (mlv16f/f15-fx-weather) · **Last-audited:** 2026-09-19

# Geodata FEED Module User Guide

> **Status:** shipped · **Verified-against:** PLACEHOLDER_SHA (mlv16f/f15-fx-weather) · **Last-audited:** 2026-09-19

The **Geodata FEED module** (`nce/vertical_modules/geodata/`) provides local, queryable stores for public geospatial reference data that describes the physical world rather than a tenant's own business data. It is a **feed module, not a tenant vertical engine**: every table it owns is GLOBAL (no `namespace_id`, RLS disabled) and every tool it mounts carries no `engine=` opt-in gate, the same reasoning already applied to `product_catalog` (migration 064).

This module currently owns **three** local stores plus **one** live-read feed:

* **OpenStreetMap elements** (Lane F Wave F-11) — OSM nodes/ways/relations for Norway; a bounding-box read. §§2–4.
* **N50 land-cover** (Lane F Wave F-12) — Kartverket's land-cover classification for Norway (forest, bog, water, etc.); a bounding-box read, same box+GiST shape as OSM. §§5–7.
* **Place names** (Lane F Wave F-13) — Kartverket's place-name register; a nearest-*point* lookup, a genuinely different read shape from the two bounding-box stores above. §§8–10.
* **Weather** (Lane F Wave F-15) — live cloud cover / fog / precipitation from MET Norway, no local store. §11.

---

## 1. Surface of Truth & Network Exposure

The Geodata module operates strictly through **7 MCP Tools** and **0 REST Routes**:

### 1.1 Mounted MCP Tools (7 Tools)
| MCP Tool | Cacheable | Mutation | Admin Only | AI-Role | Description |
|---|:---:|:---:|:---:|---|---|
| `geodata_import_osm_elements` | ✘ (`False`) | ✔ (`True`) | ✔ (`True`) | Operator | Upsert a batch of already-parsed OSM elements (nodes/ways/relations) into the local store. |
| `geodata_query_osm_elements` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Read every OSM element whose stored bounding box intersects a requested viewport. |
| `geodata_import_n50_land_cover` | ✘ (`False`) | ✔ (`True`) | ✔ (`True`) | Operator | Upsert a batch of already-parsed N50 land-cover features into the local store. |
| `geodata_query_n50_land_cover` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Read every N50 land-cover feature whose stored bounding box intersects a requested viewport. |
| `geodata_import_place_names` | ✘ (`False`) | ✔ (`True`) | ✔ (`True`) | Operator | Upsert a batch of already-resolved place names (one anchor point each) into the local store. |
| `geodata_query_nearest_place_name` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Find the place name(s) nearest a given point. |
| `geodata_get_weather` | ✔ (`True`) | ✘ (`False`) | ✘ (`False`) | Actor | Current cloud cover, fog, and precipitation for a point, live-read from MET Norway (no local store). |

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

`geodata_osm_elements` (migration `086_geodata_osm_elements.sql`) is keyed on `(osm_type, osm_id)`; see §7 for storage/indexing shared by both bounding-box stores.

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

## 7. Storage & Indexing (OSM and N50)

Both `geodata_osm_elements` and `geodata_n50_land_cover` store a Postgres-native `box` column for their bounding box, read with the `&&` intersection operator against a **GiST** index — no PostGIS extension is present in this deployment's Postgres image, and none is needed for a bounding-box read.

Being GLOBAL tables, both are read and written through `nce.db_utils.unmanaged_pg_connection` rather than a tenant-scoped RLS session — sites `geodata.osm.import`/`geodata.osm.bbox_query` and `geodata.n50.import`/`geodata.n50.bbox_query` respectively, all four pre-registered in `UNMANAGED_PG_AUDITED_SITES`.

---

## 8. What the Place-Names Store Does NOT Do

`geodata_place_names` answers a fundamentally different question from the two bounding-box stores above: not "what intersects this viewport?" but **"what named place is closest to this point?"** — a nearest-neighbour lookup. A bounding-box shape is the wrong tool here: the host's own ADR measured that an unfiltered nearest-*box* answer picked a river's bounding box (which happens to cover half of eastern Norway) for a query point in downtown Oslo. This store instead keeps one GiST-indexed anchor **point** per place and orders candidates with the `<->` operator — still core Postgres, no PostGIS.

It also does not store a place's full source geometry (a place can be recorded as a point, a point cluster, a line, or an area) — only a single representative anchor point per place, resolved by the caller before import. Storing every recorded shape verbatim was measured and rejected: tens of millions of rows for a marginally more precise answer nothing here needs.

---

## 9. Importing and Querying Place Names

### 9.1 Importing a Batch (`geodata_import_place_names`)
```json
{
  "source_file": "ssr-2026-09.gml",
  "places": [
    {
      "external_id": "12345",
      "navn": "Svolvær",
      "kategori": "bebyggelse",
      "sprak": "norsk",
      "lon": 14.5683,
      "lat": 68.2341
    }
  ]
}
```
**Response:**
```json
{
  "ok": true,
  "source_file": "ssr-2026-09.gml",
  "received": 1,
  "written": 1,
  "skipped_invalid": 0
}
```
Re-importing the same `external_id` **updates** the existing row (`ON CONFLICT ... DO UPDATE`) rather than creating a duplicate.

### 9.2 Finding the Nearest Place (`geodata_query_nearest_place_name`)
```json
{"lon": 14.57, "lat": 68.23, "kategori": "bebyggelse", "limit": 3}
```
**Response:**
```json
{
  "ok": true,
  "places": [
    {
      "external_id": "12345",
      "navn": "Svolvær",
      "kategori": "bebyggelse",
      "sprak": "norsk",
      "lon": 14.5683,
      "lat": 68.2341,
      "distance_m": 187.4
    }
  ],
  "count": 1
}
```
* `kategori` is an optional exact-match filter; omit it to search every category.
* `limit` defaults to **1** and is capped at **20** (a nearest-lookup, not a viewport read — most callers want the single closest place).
* Results are ordered by real distance in **metres**, not the GiST index's own degree-distance ordering: the query pulls up to 50 GiST-nearest candidates, then re-ranks them by great-circle distance before returning the top `limit`. This matters concretely at Norwegian latitudes — a degree of longitude is a shorter real distance the further north you go, so trusting degree-order directly can return the wrong place.

`geodata_place_names` (migration `090_geodata_place_names.sql`) is keyed on `external_id`.

---

## 10. Storage & Indexing (place names)

`geodata_place_names` stores a Postgres-native `point` column (one anchor per place) read with the `<->` nearest-neighbour operator against a **GiST** index.

Being a GLOBAL table, it is read and written through `nce.db_utils.unmanaged_pg_connection` (sites `geodata.place_names.import` and `geodata.place_names.nearest`, both pre-registered in `UNMANAGED_PG_AUDITED_SITES`) rather than a tenant-scoped RLS session.

---

## 11. Weather (`geodata_get_weather`)

Unlike the three stores above, this tool has **no local table and no import step**. Every call is a live read-through to MET Norway's public `locationforecast` API, cached in-process for `NCE_WEATHER_TTL_SECONDS` (default 30 minutes) keyed on a **rounded** coordinate (2 decimals, ~1 km — MET's own guidance, since every distinct coordinate is a separate cache key on their side).

```json
{"lat": 59.9139, "lon": 10.7522}
```
**Response:**
```json
{
  "ok": true,
  "cloud_cover_pct": 62.5,
  "fog_pct": 0.0,
  "precipitation_mm": 0.1,
  "symbol": "cloudy",
  "location": {"lat": 59.91, "lon": 10.75},
  "time": "2026-09-19T00:00:00Z",
  "fetchedAt": "2026-09-19T00:03:12+00:00",
  "stale": false,
  "source": "MET Norway"
}
```

* `force` (optional bool) bypasses the in-process TTL cache for this coordinate.
* An invalid coordinate (out of range, non-numeric, or the `(0, 0)` "null island" many languages produce for an unset value) is **not an error**: it returns `cloud_cover_pct: null, stale: true` rather than raising.
* On a MET outage, the tool degrades to the last successful reading for that coordinate with `stale: true` — never a guessed cloud cover. With no successful reading yet for that coordinate, it returns `cloud_cover_pct: null, stale: true`.
* MET's Terms of Service require an identifying `User-Agent` with a real contact, which a browser's own `fetch` cannot set (a forbidden header name) — this is why the read goes through NCE rather than a caller hitting MET directly.

### 11.1 What This Module Deliberately Does NOT Build

The charter's original wave list named "coastline" and "roads" as siblings of OSM/N50/place names in this module. Both were investigated and **not built** — filed as Q-42 rather than guessed past:

* **Coastline's raw data is already covered by N50 land-cover** (§5–7): the host's own source-of-truth ADR names N50's `havflate` (sea-surface) class as its source, which the generic `klasse`-keyed store above already imports. What the host builds beyond that — a pre-simplified, multi-zoom-level (Douglas-Peucker) tile pyramid for its own world-map rendering layers — is a rendering-pipeline optimisation with no consumer on the NCE side, not an additional FEED-shaped local store.
* **Roads are not an OpenStreetMap `highway=*` question.** The host's real road-network source is a third vendor (NVDB, Norway's national road database), chosen specifically because Kartverket's own road geometry is restricted-license, filtered to a rendering-specific zoom band that deliberately excludes pedestrian paths, private roads, and municipal roads for that product's chosen visual density — a map-UI product decision, not a data shape this module can adopt unilaterally.

---

## 12. Attribution

* **OpenStreetMap** data is public and licensed **ODbL**.
* **Kartverket's N50** data is public and licensed **CC BY 4.0**.
* **Kartverket's place-name register** is public and licensed **NLOD/CC BY 4.0**.
* **MET Norway** forecast data is public sector information, **NLOD**-licensed.

A caller that displays any of this data to end users must show the corresponding attribution — this module stores and serves the data but does not own or enforce that attribution on any particular display.

---

> **Verified-against: PLACEHOLDER_SHA**
