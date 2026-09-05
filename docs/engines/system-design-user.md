> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# System Design Engine User Guide (Doc 69)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **System Design Engine** (`nce/vertical_modules/system_design/`) is NCE's Revenue↔Delivery bridge: it turns a room brief or a Sales quote into a versioned, room-centric Bill of Materials (BOM) and a Statement of Work (SoW). It is the "AI Solution Agent" — an embedding-recall loop over past `DESIGN`/`PROJECT` memories, not a rules engine — and it is strictly **propose-only**: every line it produces carries `validated: False` until a human explicitly accepts or overrides it.

> [!NOTE]
> **External surface: COMPLETE as of Module 6 W12a-W20 (2026-08-30).**
> This block previously warned that the engine exposed only 2 MCP tools and that interactive topological
> design "CANNOT be invoked over MCP or REST endpoints today". **That is no longer true** — and the warning
> is replaced rather than amended, because it was cited downstream as evidence of the gap.
>
> **7 MCP tools**, all advertised in `tools/list` and asserted equal to the registry in both directions by
> `tests/unit/test_system_design_toolcount.py`: `system_design_ping` · `system_design_publish_design_docs` ·
> `system_design_get_topology` · `system_design_author_topology` ·
> `system_design_author_functional_location` · `system_design_validate_design_graph` ·
> `system_design_delete_planned`
>
> **5 REST routes:** `POST /api/system-design/publish-design-docs` · `GET|POST /api/system-design/topology` ·
> `POST /api/system-design/functional-location` · `POST /api/system-design/validate` ·
> `DELETE /api/system-design/planned`
>
> Also live: canvas geometry and the per-design optimistic-concurrency token (`expected_version`, W14),
> per-node lifecycle `status`/`revision`/`salience` with a live `statuses` read filter (W16/W16b), and a
> retire path that soft-retires by default (W17).
>
> **One caveat that is still true and matters to a client:**
> 1. MCP is stdio-only. Server-to-server REST with HMAC is the transport for a browser-facing client; there
>    is no browser path to MCP.
>
> `system_design_delete_planned` is `admin_only=True`; `nce/auth.py`'s MCP dispatch gates on the registry's
> `admin_only` flag in addition to its hardcoded `MCP_ADMIN_TOOL_NAMES`, so the tool answers over MCP with a
> valid `admin_api_key` — no `NCE_ADMIN_OVERRIDE` needed.

> [!IMPORTANT]
> Phase-1a of this engine (the value core: propose → gap-fill → SoW → freeze) ships with **zero external systems**. The functions in this guide read/write only the knowledge graph (`kg_nodes`/`kg_edges`) and the `memories` table. NetBox, SharePoint, and Lucid are independent Phase-1b adapters — see the [Admin Guide](system-design-admin.md).

---

## 1. What you can actually call

The System Design engine exposes **7 individually-registered MCP tools**:

| MCP tool | cacheable | admin_only | mutation |
|---|---|---|---|
| `system_design_ping` | `True` | `False` | `False` |
| `system_design_publish_design_docs` | `False` | `False` | `True` |
| `system_design_get_topology` | `False` | `False` | `False` |
| `system_design_author_topology` | `False` | `False` | `True` |
| `system_design_author_functional_location` | `False` | `False` | `True` |
| `system_design_validate_design_graph` | `False` | `False` | `False` |
| `system_design_delete_planned` | `False` | `True` | `True` |

Other functions — `do_propose_design`, `do_design_from_quote`, `do_design_to_quote`, `do_validate_design`, `do_generate_sow`, `do_enrich_design_lines` — are **plain async Python functions**, invoked in-process by another engine's flow code (A2A by direct call, not by MCP dispatch). The clearest example is Sales's `do_initiate_quote_flow`, which calls `do_propose_design` directly (`nce/vertical_modules/sales/commission.py:189-196`).

> [!NOTE]
> *(planned — not yet implemented)* The design spec (`docs/vertical_engines/06-system-design-engine.md:136-148`) describes a full MCP tool set including `system_design_propose_design`, `system_design_design_from_quote`, `system_design_generate_sow`, `system_design_design_to_quote`, and `system_design_validate_design`. None of those specific flow tools exist in code today. If you are integrating against this engine for those flows, call the Python functions directly (as Sales does) or wait for those tools to ship.

### `system_design_ping`
Liveness probe / ping stub. Requires `namespace_id`. Returns:
```json
{"ok": true, "engine": "system_design"}
```
Source: `nce/vertical_modules/system_design/mcp_handlers.py:36-45`.

### `system_design_get_topology`
Reads the functional location tree and node states for a design. 

> [!WARNING]
> **REST vs. MCP filter asymmetry (`statuses`)**
> When filtering by node status, an empty filter behaves oppositely depending on the transport:
> - **MCP:** Sending `statuses: []` is falsy and means **no filter** (returns all nodes).
> - **REST:** Sending `?statuses=` results in `[""]` which is truthy, so one empty-string status is forwarded. Because no node has an empty-string status, it **returns nothing**. 
> This is a known contract divergence.

### `system_design_publish_design_docs`
Exports a `DESIGN` to Lucid (see §7). Requires `namespace_id` and `design_id`. Also reachable over REST as `POST /api/system-design/publish-design-docs`.

---

## 2. The functional-location spine

Every design and every quote line hangs off a **functional-location tree**: `SITE > BUILDING > FLOOR > ROOM > POSITION`, modeled as `kg_nodes` with `entity_type='FUNCTIONAL_LOCATION'` (`nce/vertical_modules/system_design/graph.py:60`). Labels are deterministic and upper-cased: `FL:<namespace_slug>:<SITE>:<BUILDING>:<FLOOR>:<ROOM>:<POSITION>` (`graph.py:79-86`).

Design intent is authored **before** the physical room exists — NetBox/Assets confirm it as as-built only after install (see Admin Guide §3). The design-intent node is never overwritten, only linked.

`do_author_functional_location(conn, namespace_id, *, namespace_slug, design_id, site_name, buildings, design_lines=None, source_id=None)` (`graph.py:264-481`) writes the whole tree plus a `DESIGN` node and any `DESIGN_LINE` nodes in one pass, producing these edges (confidence lives on edges only, never on nodes — `graph.py:39`):

- `DESIGN -[contains]-> SITE`
- `SITE -[parent_of]-> BUILDING -[parent_of]-> FLOOR -[parent_of]-> ROOM -[parent_of]-> POSITION`
- `DESIGN -[contains]-> DESIGN_LINE`
- `FUNCTIONAL_LOCATION(SITE) -[needs]-> DESIGN_LINE`
- `DESIGN_LINE -[references]-> PRODUCT` (cross-engine, by label only — Product owns the `PRODUCT` node)

`buildings` accepts nested dicts: `{"name": str, "floors": [{"name": str, "rooms": [{"name": str, "positions": [str, ...]}]}]}`. Returns `{"authored": {"nodes": int, "edges": int}}`.

This is a plumbing function, not an MCP tool — you call it directly (with your own `asyncpg.Connection`, already RLS-scoped) when standing up a new design from scratch, exactly as `tests/test_system_design_phase1a.py:176-188` does.

---

## 3. Design-first: propose → SoW → freeze

### 3.1 `do_propose_design(engine, params)` — the recall loop
Source: `nce/vertical_modules/system_design/propose.py:212-311`.

```python
result = await do_propose_design(engine, {
    "namespace_id": "…",       # required
    "room_brief": "AV conference room with ceiling mics and DSP",  # required
})
```

What happens:
1. `room_brief` is embedded via `nce.embeddings.embed` (768-dim, L2-normalized) — outside any DB transaction.
2. The engine recalls the top-`NCE_SYSTEM_DESIGN_RECALL_TOP_K` (default `5`) most similar `DESIGN`/`PROJECT` memories from the `memories` table using pgvector cosine distance (`<=>`), scoped to the namespace, restricted to rows with a non-NULL embedding and `valid_to IS NULL` (`propose.py:90-111`).
3. An **outcome-weighting** hook exists but is dormant: when `NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED` is `False` (the default, and the only supported value today), candidates are returned in pure cosine-similarity order, unchanged. Flipping it to `True` currently raises `NotImplementedError` — the discount-by-change-orders/tickets/margin logic has not been written yet (`propose.py:120-148`).
4. Each recalled memory's `metadata` JSONB is read for `product_ref` (or `product_ref_id`) and `qty`/`quantity`; memories without a usable `product_ref` are silently skipped.

Returns:
```json
{
  "proposed_lines": [
    {
      "product_ref": "BIAMP:TESIRAFORTE-CI",
      "qty": 1,
      "confidence": 0.87,
      "validated": false,
      "recall_memory_id": "…"
    }
  ],
  "recall_evidence": [
    {"id": "…", "name": "…", "payload_ref": "…", "node_type": "DESIGN", "similarity": 0.87, "distance": 0.13}
  ],
  "outcome_weighting_applied": false
}
```

> [!IMPORTANT]
> `validated` is **always `False`** here. This is a Python dict field only — it is not a column on any table. Nothing is written to the graph by this call; it is pure read + recall (`propose.py:19-20`).

### 3.2 `do_generate_sow(engine, params)` — the SoW
Source: `nce/vertical_modules/system_design/sow.py:651-751`.

```python
result = await do_generate_sow(engine, {
    "namespace_id": "…",      # required
    "design_id": "DESIGN-…",  # required
    "version_number": 7,      # optional — see freeze-on-issue below
})
```

The generator is split into a pure transform and a graph adapter:
- `generate_sow(sow_input, version_number=1)` (`sow.py:130-389`) is a **zero-DB, deterministic** function lifted near-1:1 from Andreas's `lib/sow/generator.ts:183`. It produces the summary, per-room deliverables, labor-by-category, managed services, invoicing breakdown, timeline, acceptance clauses, and terms sections of the SoW.
- `do_generate_sow` reads the `DESIGN`/`DESIGN_LINE`/`FUNCTIONAL_LOCATION` subgraph in one scoped session, assembles a `SoWInput` dict (`_assemble_sow_input`, `sow.py:562-643`), then calls the pure transform outside the session.

**Freeze-on-issue:** if you don't pass `version_number`, it is derived deterministically from `SHA256(design_label|design_meta.updated_at) % 100_000 + 1` (`sow.py:542-559`). Re-issuing a SoW for a design whose `updated_at` hasn't changed returns the identical version number; any mutation that bumps `updated_at` (e.g. `do_validate_design`) yields a new version. `frozen: true` in the response means you supplied `version_number` explicitly.

Only fields owned by System Design (`project` id/name derived from the design label, `bomLines` — one per `DESIGN_LINE`, `rooms` — one per ROOM-depth `FUNCTIONAL_LOCATION`) are populated from the graph today. Labor, milestones, service contracts, and invoicing schedule are left empty unless the caller supplies them — those belong to Project/Economy/Sales and are not yet wired in.

### 3.3 `do_design_to_quote(engine, params)` — freeze and hand off to Sales
Source: `nce/vertical_modules/system_design/to_quote.py:160-288`.

```python
result = await do_design_to_quote(engine, {
    "namespace_id": "…",
    "design_id": "DESIGN-…",
})
```

This is the design-first exit. It:
1. Reads the `DESIGN` node and all `DESIGN_LINE` children.
2. Derives a frozen `design_version` the same way `do_generate_sow` does.
3. Writes `DESIGN -[becomes]-> QUOTE` with confidence `0.95` (`to_quote.py:69`).
4. Calls the injectable A2A seam `_propose_quote_to_sales(engine, namespace_id, proposal)` — at the time of writing this **always raises `NotImplementedError`** because the Sales engine's receiving side is not built; the function catches it and returns `proposal_sent: false` rather than failing the caller (`to_quote.py:107-111`, `258-268`).

**Contract A (§9.1):** this function never writes or mutates a `QUOTE` kg_nodes row — Sales owns that node. The `QUOTE:<DESIGN_ID>` label appears only as the object of the `becomes` edge.

Returns `{"design_id", "design_label", "quote_label", "design_version", "becomes_edge", "bom_line_count", "proposal_sent"}`.

---

## 4. Quote-first: `do_design_from_quote(engine, params)`

Source: `nce/vertical_modules/system_design/from_quote.py:251-421`.

```python
result = await do_design_from_quote(engine, {
    "namespace_id": "…",
    "quote_id": "Q-1001",       # required — Sales QUOTE identifier
    "design_id": "DESIGN-Q-1001",  # optional, defaults to DESIGN-<quote_id>
    "namespace_slug": "acme",       # optional, used for FL label prefixes
})
```

This is the mirror-image entry point: a Sales quote already has line items tagged to functional locations, and System Design lifts them into a `DESIGN`:

1. Reads quote lines via the injectable seam `_read_quote_lines(engine, namespace_id, quote_id)` — this **also always raises `NotImplementedError`** today (Sales's `sales_get_quote_lines` A2A tool does not exist yet); production callers and tests must patch this function.
2. For each distinct functional-location path seen across the quote lines, runs the §3.1 recall loop (`_gap_fill_for_lines`) to propose missing accessories/infrastructure/labor. Every gap-fill line is `validated: False`.
3. Writes one `DESIGN_LINE` per quote line plus the gap-fill lines via `do_author_functional_location`.
4. Writes `QUOTE -[realized_as]-> DESIGN` with confidence `0.9` (`from_quote.py:75`).

Returns `{"design_id", "quote_label", "design_label", "authored", "quote_lines_realized", "gap_fill_lines", "realized_as_edge"}`.

**Contract A (§9.1)** applies here too: no `QUOTE` kg_nodes row is ever written; the QUOTE label is edge-only.

---

## 5. Human validation gate: `do_validate_design(engine, params)`

Source: `nce/vertical_modules/system_design/validate.py:195-293`.

```python
result = await do_validate_design(engine, {
    "namespace_id": "…",
    "design_id": "DESIGN-…",
    "decisions": [
        {"line_id": "DESIGN_LINE:DESIGN-…:DL-001", "verdict": "accept"},
        {"line_id": "DESIGN_LINE:DESIGN-…:DL-002", "verdict": "override", "reason": "wrong DSP model for room size"},
    ],
})
```

**§9.3 propose-only invariant:** every decision must carry an explicit `"accept"` or `"override"` verdict — there is no confidence threshold that can silently accept a line. A missing or malformed verdict raises `ValueError` rather than being treated as an accept (`validate.py:65-85`).

What happens:
1. Decisions are validated for shape.
2. `passed = (zero override decisions)`; each override's `reason` (or an auto-generated default) is collected into `reasons`.
3. The `DESIGN` node is re-upserted (bumping `updated_at`, which is exactly the signal `do_generate_sow`'s freeze-on-issue logic watches for a new version).
4. The full decision batch is appended as one row to `v3_cognitive_ledger` (`tlx_scores` JSONB payload, zero-vector `empathic_tensor` since validation carries no affective signal) tagged `model_version = "system_design/validate/v1"` — this is the feedback loop that will eventually calibrate the (currently dormant) outcome-weighting in `do_propose_design`.

Returns `{"passed": bool, "reasons": [str], "decisions_recorded": int, "design_version_bumped": true}`.

---

## 6. Phase-2 device/graph model and structural validation

Phase-2 is an **additive** layer on top of the same `DESIGN` node — it does not replace or gate Phase-1a. It models real device topology so the engine can catch design errors, not just propose parts.

### 6.1 Authoring topology: `do_author_device_topology`
Source: `nce/vertical_modules/system_design/devices.py:305-511`. Not an MCP tool — called directly with a connection, same as `do_author_functional_location`.

```python
result = await do_author_device_topology(conn, namespace_id, design_id="DESIGN-…",
    devices=[
        {
            "device_ref": "DISP-1",
            "capability": {"device_category": "Display", "manufacturer": "Samsung", "power_draw_watts": 220},
            "ports": [
                {"port_ref": "HDMI-IN-1", "capability": {"signal_format": "HDMI", "signal_version": "2.0", "port_direction": "input"}},
            ],
            "rack_ref": "RACK-A",
        },
    ],
    connections=[
        {"from_device_ref": "SWITCH-1", "from_port_ref": "OUT-1", "to_device_ref": "DISP-1", "to_port_ref": "HDMI-IN-1", "confidence": 1.0},
    ],
    racks=[{"rack_ref": "RACK-A", "capability": {"device_category": "Rack"}}],
)
```

Node types written (all owned by `system_design` per `nce/config_data/node-ownership.json:13-17`): `DEVICE`, `PORT`, `SIGNAL_CHAIN`, `RACK`, `CABLE`. Edges: `DESIGN -[contains]-> DEVICE`, `DEVICE -[has_port]-> PORT`, `PORT -[connected_to]-> PORT`, `DEVICE -[mounted_in]-> RACK`, `DEVICE -[uses_cable]-> CABLE`, `DESIGN -[has_rack]-> RACK`.

Typed capability attributes (AVIXA Revit Parameter List fields: `signal_format`, `signal_version`, `port_direction`, `poe_class`, `poe_watts`, `dante_rx_channels`, `dante_tx_channels`, `power_draw_watts`, `heat_btu_hr`, `redundancy_role`, `device_category`, `manufacturer`, `model_number`, `extra`) are written to the `system_design_device_capabilities` table (migration 039), **not** to `kg_nodes` — `kg_nodes` has no payload/metadata column (`devices.py:91-106`). Returns `{"authored": {"nodes": int, "edges": int, "capabilities": int}}`.

### 6.2 Structural validation: `validate_design_graph(engine, params)`
Source: `nce/vertical_modules/system_design/validation_queries.py:389-475`. Read-only, propose-only — no auto-fix, no mutation. It is additive to (does not call or alter) Phase-1 `do_validate_design`.

```python
result = await validate_design_graph(engine, {"namespace_id": "…", "design_id": "DESIGN-…"})
# {"passed": bool, "reasons": [str, ...]}
```

Runs five checks and ANDs their `passed` flags (all reasons are concatenated regardless of pass/fail):

1. **`check_signal_flow_continuity`** — every PORT with `port_direction='input'` must have at least one inbound `connected_to` edge; dangling inputs fail.
2. **`check_port_format_compatibility`** — for each `connected_to` edge, compares `signal_format`/`signal_version` of the two ports. Same-format version checks are table-driven (`_HDMI_VERSION_ORDER`, `_DP_VERSION_ORDER`) — e.g. an HDMI 2.1 source into a 2.0 sink fails (source ordinal must be ≥ sink ordinal). Cross-format pairs are allowed only via `_CROSS_FORMAT_COMPAT` (currently just `DANTE ⇄ AES67`). Dante connections additionally fail if the source's `dante_tx_channels` is less than the sink's `dante_rx_channels`.
3. **`check_power_heat_budget`** — sums `power_draw_watts` and `heat_btu_hr` across all devices. **Always passes** — it is informational only; there is no configurable ceiling. Reasons always include the totals.
4. **`check_spof_redundancy`** — any device with `redundancy_role='primary'` must have at least one sibling `redundancy_role='secondary'` in the design; standalone devices are ignored.
5. **`check_avixa_checkpoint_conformance`** — every `DEVICE` must have `device_category` set; every `PORT` must have `signal_format` set and, if present, a `port_direction` in `{input, output, bidirectional}`.

All five underlying `check_*` functions are pure (no DB) and independently unit-testable; the `_fetch_*` helpers do the graph reads, explicitly scoped by `namespace_id` at the SQL level (not relying on RLS alone) to avoid cross-namespace bleed when the same design label exists in two test namespaces.

---

## 7. Design docs export: Lucid (the one real MCP tool with an effect)

Source: `nce/vertical_modules/system_design/lucid.py`. **Export only** — there is no import path; the spec's original idea of parsing Lucid diagrams back into `DESIGN_LINE`s was cut.

Call via the `system_design_publish_design_docs` MCP tool or `POST /api/system-design/publish-design-docs` with `{"namespace_id": "…", "design_id": "DESIGN-…"}`.

Behavior:
- Reads the `DESIGN`, all `DESIGN_LINE`, and all `FUNCTIONAL_LOCATION` nodes linked to the design.
- Maps them to a Lucid diagram payload: one rectangle shape per `DESIGN_LINE`, one ellipse shape per `FUNCTIONAL_LOCATION`.
- POSTs to `{LUCID_BASE_URL}/diagrams` via `httpx` with a 30s timeout and retry/back-off (`request_with_retry`).
- If `NCE_SYSTEM_DESIGN_LUCID_API_KEY` is unset, returns `{"lucid_url": null}` — a clean no-op, never an error.
- On success, returns `{"lucid_url": "<url>"}` (parsed from `editUrl`/`url`/`diagramUrl` in the Lucid response).

---

## 8. Worked example: end-to-end design-first flow

This mirrors the integration test at `tests/test_system_design_phase1a.py`:

```python
# 1. Author the site tree + a starting design line.
async with scoped_pg_session(pg_pool, ns_id) as conn:
    await do_author_functional_location(
        conn, ns_id,
        namespace_slug="acme",
        design_id="DESIGN-001",
        site_name="SiteAlpha",
        buildings=[{"name": "MainBuilding", "floors": [{"name": "Floor1", "rooms": [
            {"name": "ConfRoom101", "positions": ["RACK-A"]}
        ]}]}],
        design_lines=[{"line_ref": "DL-001", "manufacturer": "Biamp", "mfr_part_no": "TesiraFORTE-CI", "confidence": 0.95}],
    )

# 2. Ask the AI Solution Agent for similar-room proposals (propose-only).
propose = await do_propose_design(engine, {
    "namespace_id": str(ns_id),
    "room_brief": "AV conference room with ceiling mics and DSP",
})
assert all(line["validated"] is False for line in propose["proposed_lines"])

# 3. Generate the SoW for review.
sow = await do_generate_sow(engine, {"namespace_id": str(ns_id), "design_id": "DESIGN-001"})

# 4. Human reviews the proposed lines out-of-band, then records the verdicts.
await do_validate_design(engine, {
    "namespace_id": str(ns_id),
    "design_id": "DESIGN-001",
    "decisions": [{"line_id": "DESIGN_LINE:DESIGN-001:DL-001", "verdict": "accept"}],
})

# 5. Freeze the design into a quote proposal for Sales.
result = await do_design_to_quote(engine, {"namespace_id": str(ns_id), "design_id": "DESIGN-001"})
# result["proposal_sent"] is False today — Sales's receiving A2A tool isn't built yet.
```

---

## 9. Related reading
- [System Design Admin Guide](system-design-admin.md) — enablement caveats, NetBox/SharePoint/Lucid adapters, RLS tables, troubleshooting.
- `docs/vertical_engines/06-system-design-engine.md` — original design-intent spec (DISCUSSION doc; several sections describe functionality not yet built — treat as roadmap, not ground truth).
