# System Design vertical module

Phase-1 authors `DESIGN` / `FUNCTIONAL_LOCATION` / `DESIGN_LINE`. Phase-2 (`devices.py`)
adds the topology layer — `DEVICE`, `PORT`, `RACK`, `CABLE` nodes hung off the same
`DESIGN` node — and `validation_queries.py` runs five design-quality checks over it.

## Decision record: `SIGNAL_CHAIN` is retired (Batch 067i, 2026-08-28)

**A signal chain is a `connected_to` walk over `PORT` nodes. It is not a node type.**

### What was there

`SIGNAL_CHAIN` was declared as a Phase-2 node type in three places and written in none:

| Site | State before | State now |
| --- | --- | --- |
| `devices.py` `_NODE_TYPE_SIGNAL_CHAIN` / `NODE_TYPE_SIGNAL_CHAIN` | Declared, never passed to `_upsert_node` | Removed |
| `devices.py` `signal_chain_label()` | Defined, **zero call sites repo-wide** | Removed |
| `nce/config_data/node-ownership.json` | `SIGNAL_CHAIN` → `system_design`, `transition: null` | **Left inert** (see below) |

No module in `nce/` ever inserted a `SIGNAL_CHAIN` row into `kg_nodes`. The other four
Phase-2 constants (`_NODE_TYPE_DEVICE`, `_PORT`, `_RACK`, `_CABLE`) *are* passed to
`_upsert_node`; `_NODE_TYPE_SIGNAL_CHAIN` was the one with no writer behind it.

### Why no writer was built

Because the graph already answers the question a `SIGNAL_CHAIN` node would answer.

A chain — source → matrix/switch → display, or an audio DSP path — is fully described by
the `PORT -[connected_to]-> PORT` edges that `do_author_device_topology` already writes.
Materialising the same path as a node buys nothing and costs consistency: every
re-cabling would have to update the edges *and* rebuild the chain nodes, and the two
representations would drift the moment one write path forgot the other. The chain-as-node
would be a cache of the edges with no invalidation story.

The consumers bear this out — `validation_queries.py`'s `signal_flow_continuity` and
`port_format_compatibility` checks both walk `connected_to` edges directly and never
consult a chain node. Inventing a writer to populate a node type nobody reads would have
been speculative generality (needless complexity), so it was not built.

**To traverse a chain:** walk `connected_to` from the source `PORT` node. To validate one,
call `validate_design_graph`.

### Why the `node-ownership.json` row was left inert

The row is kept deliberately. Removing it was considered and rejected:

- **Nothing runtime depends on it, but three shipped docs do.**
  `docs/engines/system-design-admin.md`, `docs/engines/system-design-user.md` (which cites
  the literal line range `node-ownership.json:13-17`) and `docs/shared-core/entity-resolution.md`
  all list `SIGNAL_CHAIN` as a `system_design`-owned type. None of those files is in this
  wave's scope, so deleting the row would have silently falsified three documents this
  wave is not allowed to repair.
- **The row is a name reservation, and that has value.** `assert_owner` is deny-by-default.
  While the row stands, no *other* engine can claim the `SIGNAL_CHAIN` type; drop it and the
  name is unowned. Downstream specs still reference the concept — Engine 19's blast-radius
  gate (`docs/vertical_engines/NCE_remote_access_rmm/19b-smart-features.md`) is written
  against `ASSET → PORT/SIGNAL_CHAIN → SPOF`.
- **Keeping it is inert, not dead.** The seeder (`ownership_seed.py`) inserts every row from
  the JSON; the extra row grants an ownership nobody exercises. It cannot cause a write,
  because no code constructs a `SIGNAL_CHAIN` label any more.

Retiring the *type* while keeping the *reservation* is the combination that ends the
ambiguity without breaking documents out of scope.

### Ratchet

`tests/unit/test_system_design_signal_chain_retired.py` asserts `signal_chain_label` and the
`NODE_TYPE_SIGNAL_CHAIN` constant stay gone, so the phantom cannot silently return. If a
future wave genuinely needs materialised chains, it must delete that test on purpose and
say why — starting from this decision record, not from an undeclared assumption.
