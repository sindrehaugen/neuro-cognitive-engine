# NCE — Neuro-Cognitive Engine

A multi-tenant memory and reasoning substrate for AI agents, with a suite of business engines built on top of it.

Agents that run a business need to remember things, and they need the memory to be correct: scoped to the right tenant, attributable to a source, and impossible to quietly corrupt. NCE is the layer that provides that, plus the vertical engines that use it — procurement, sales, project, economy, inventory and others.

Built in Python on Postgres, MongoDB, Redis and MinIO. Exposed over MCP (JSON-RPC 2.0) and REST.

---

## What is actually here

| | |
|---|---|
| Python in `nce/` | 103,762 lines |
| Test files | 391 |
| Registered MCP tools | 115 |
| Database migrations | 48 |
| Vertical engines with code on main | 11 |
| Documentation pages | 114 — **[read them here](https://sindrehaugen.github.io/neuro-cognitive-engine/)** |
| Architecture decision records | 8 |
| CI jobs per pull request | 5 |

Numbers are counted from the tree, not estimated. The tool count is asserted by a test (`tests/test_tool_registry.py`), so it cannot drift from reality without failing the build.

---

## The parts worth looking at

**Tenant isolation is enforced by the database, not by convention.** Every tenant-scoped table carries `ENABLE` + `FORCE ROW LEVEL SECURITY` with a policy on `namespace_id`. Application code additionally filters explicitly, because a superuser connection bypasses `FORCE RLS` and a test that forgets this proves nothing. `tests/test_rls_catalog.py` fails the build if any tenant table is added without being registered.

**Write authority is deny-by-default.** Engines do not write each other's data. `nce/config_data/node-ownership.json` maps `(node_type, transition) → owning engine`, and `assert_owner` refuses anything unregistered at the write site. Ownership can be per-transition, not just per-node: a purchase order line's `ORDERED` status belongs to Procurement, `DELIVERED` to Warehouse, `INSTALLED` to Field Tech, and the same guard expresses all three.

**The audit log is append-only at the database level.** `event_log` has a WORM trigger; `UPDATE` and `DELETE` are revoked, not merely avoided.

**Autonomy is bounded.** World-writing tools default to human-confirm-only, carry an idempotency key, and record to the audit log. Value and volume ceilings, an allowlist and a kill switch sit in front of them. Replaying an action is a no-op rather than a second write.

**Money and cost never leave the building.** An allow-list field projector (`project(node, surface)`) controls what reaches an external surface. Margin and cost are not on any allow-list.

**Generated text is grounded.** Prose is assembled from facts already in the graph, with each claim linked to the node it came from, rather than produced free-hand and checked afterwards.

**Data can come from two systems at once.** A per-namespace resolver decides whether a given function reads from Dynamics 365, from NCE, or both, and logs divergence when the two disagree — so a migration can be measured before it is committed to.

---

## Vertical engines

Eleven engines have code on `main`. Each owns its own tables, its own node types, and a documented boundary with the others.

`procurement` · `product` · `agreements` · `vendors` · `sales` · `system_design` · `project` · `economy` · `inventory` · `dynamics365` · `diagnostics`

The engine suite is planned at 17. It is being built incrementally and is not finished; see *Status* below.

---

## How it is built

The engine suite is not written by hand in one pass. It is built as a sequence of small units — 229 planned, each one concern, each on its own branch and commit — and every unit passes an independent adversarial review before it is accepted.

The review is done by a different model instance than the one that wrote the code, with the specific instruction to try to break it and to default to rejecting when uncertain. That has repeatedly mattered:

- A stock-movement change shipped with a graph projection written inside the authoritative transaction. It deadlocked on any two-way traffic between the same two locations, at 100% of attempts. Lint passed, types passed, the test suite was green. The review found it and the fix was to order both the row writes and the projection by a canonical key.
- An event-bus change would have marked events as successfully published while silently dropping them, because a handler logged and returned instead of raising. The review rejected it twice before anything shipped.
- A test asserting a decimal-precision rule was found to pass identically whether or not the rule was applied, because its only fixture rounded the same way under both paths.

The recurring lesson is that a green test suite says less than it appears to. The properties that fail are usually the ones a docstring argues for most confidently and no test actually discriminates.

---

## Running it

```bash
cp .env.example .env          # fill in the values
make local-up                 # Postgres, MongoDB, Redis, MinIO via docker compose
make lint typecheck           # ruff + mypy
pytest -m "not integration"   # unit tests, no database required
pytest -m integration         # requires the stack above
```

Integration tests skip rather than fail when no database is reachable. A green exit code with skipped tests is not a passing run — check the counts.

---

## Documentation

The full documentation is published at **[sindrehaugen.github.io/neuro-cognitive-engine](https://sindrehaugen.github.io/neuro-cognitive-engine/)** — architecture, the eight architecture decision records, per-engine admin and user guides, the shared-core reference, and the MCP tool cookbook.

Worth starting with:

- [Architecture](https://sindrehaugen.github.io/neuro-cognitive-engine/#/architecture-v1) — the four-database stack and how the layers sit
- [Shared core](https://sindrehaugen.github.io/neuro-cognitive-engine/#/shared-core/overview) — entity resolution, ownership, autonomy governance, redaction
- [Multi-tenancy](https://sindrehaugen.github.io/neuro-cognitive-engine/#/multi_tenancy) — how `FORCE ROW LEVEL SECURITY` is applied
- [MCP tool cookbook](https://sindrehaugen.github.io/neuro-cognitive-engine/#/mcp_tool_cookbook) — the tool surface, with gating flags
- [ADRs](https://sindrehaugen.github.io/neuro-cognitive-engine/#/adr/README) — decisions and their trade-offs

---

## Layout

```
nce/                    engine core: memory, graph, RLS, autonomy, signing, replay
nce/vertical_modules/   the business engines
nce/migrations/         idempotent SQL, re-applied on boot under an advisory lock
tests/                  391 files; integration tests marked and CI-wired
docs/                   source for the published documentation
go/                     launcher and hardware detection
trimcp-infra/           Terraform for AWS and GCP
deploy/                 compose stacks and container images
```

---

## Status

Under active development. Eleven of seventeen engines have code on `main`; the twelfth is in progress. The core substrate — tenant isolation, ownership, autonomy governance, audit log, signing, replay — is complete and in use by the engines above it.

This repository is published to show the architecture and the engineering approach. It is not a packaged product and there is no support commitment.

---

## Licence

Proprietary. All rights reserved. Published for review; not licensed for reuse.

---

**Sindre Løvlie Haugen** · [github.com/sindrehaugen](https://github.com/sindrehaugen)
