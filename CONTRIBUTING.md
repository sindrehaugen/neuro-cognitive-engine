# Contributing to NCE Core

Thank you for your interest in **NCE (`neuro-cognitive-engine`)**—an ongoing May 2026 R&D project exploring multi-database systems architecture, PostgreSQL forced Row-Level Security, envelope cryptography, and grounded MCP workflows. This guide covers local environment setup, CI quality gates, schema/doc generators, and core architectural boundaries.

---

## Core Boundary Rule: Keep Core Domain-Agnostic

> [!IMPORTANT]
> **Never hardcode business-domain table names, vendor connector slugs, or vertical workflow logic inside `neuro-cognitive-engine`.**

`neuro-cognitive-engine` provides the domain-agnostic four-store persistence, retrieval, security, governance, and `nce.connectors` SDK substrate:

- Standalone CAD/BIM/workspace file and API connectors belong in [`sindrehaugen/nce-app-engines`](https://github.com/sindrehaugen/nce-app-engines) (`nce_apps`).
- Business, ERP/CRM, compliance, and infrastructure prototype modules belong in [`sindrehaugen/nce-vertical-engines`](https://github.com/sindrehaugen/nce-vertical-engines) (`nce_verticals`).
- Core interacts with extensions strictly via `nce.plugins` entry points, `nce.connectors` SDK primitives, generic resource surfaces (`nce/resource_surface/`), and standard event contracts.

---

## Local Development Setup

NCE requires **Python 3.10+** (Python 3.12 matches CI container images) and Docker for running local PostgreSQL (`pgvector`), MongoDB, Redis, and MinIO containers.

```bash
git clone https://github.com/sindrehaugen/neuro-cognitive-engine.git
cd neuro-cognitive-engine

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

---

## Quality & Verification Gates

Every pull request must pass linting, formatting, type-checking, documentation integrity, and unit/security test suites:

```bash
# 1. Ruff lint + format check + documentation/link/anti-metaphor gate
make lint

# 2. Static type checking
make typecheck

# 3. Fast unit, security, schema-drift, and doc-ratchet tests
pytest tests/ -m "not integration and not perf and not live"

# 4. Live multi-store integration tests (requires `make local-up` or `make up`)
pytest -m integration
```

---

## Plugin Extension Seam & Isolated SQL Migration Ledgers (`nce.plugins`)

External plugin packages register via `[project.entry-points."nce.plugins"]` (see [`docs/plugin_development.md`](docs/plugin_development.md)):

- **`NCEPlugin` & `PluginCapabilities`:** Plugins declare `tenant_rls_tables`, `global_tables`, and `unmanaged_pg_sites` via `get_capabilities()` so Core's startup `verify_rls_catalog_consistency()` enforces `FORCE ROW LEVEL SECURITY` across both Core (`39` forced-RLS tables out of `42` Core SQL tables) and all installed plugin tables.
- **Scoped Migration Ledgers:** Core migrations (`nce/migrations/001_enable_rls.sql`–`041_app_connections_vault.sql` + `optional/010_citus_sharding.sql`) record bare filenames in `applied_migrations`, whereas plugins record `<plugin_scope>:<filename>` (e.g., `nce_apps:001_shared_cad_tables.sql`, `nce_verticals:001_shared_spine.sql`), avoiding cross-repo numbering collisions.

---

## Schema, Lockfile & Surface Generators

When modifying SQL migrations, dependencies, MCP tool specs, or HTTP routes, run the corresponding verification/generation scripts:

```bash
# Verify declarative schema parity across all 42 Core SQL tables
python scripts/check_schema_drift.py

# Refresh locked transitive dependencies (requirements.lock)
make lockfile

# Regenerate Pure-Core OpenAPI and API surface docs (with no external plugins mounted)
NCE_ENABLED_ENGINES=none python scripts/gen_openapi.py
NCE_ENABLED_ENGINES=none python scripts/gen_api_docs.py

# Verify relative links, #anchors, NCE_* config parity, anti-metaphor rules, and brand wall
python scripts/check_docs_integrity.py
python scripts/verify_docs_links_and_syntax.py
```

> [!NOTE]
> **Generated Files & Post-Merge Sync:** Never hand-edit `docs/_generated/*`, `docs/API.md`, or `openapi.json`. Keep documentation prose grounded in concrete database, cryptographic, and retrieval mechanics rather than biological metaphors or commercial product claims.

---

## Pull Request Guidelines

1. **Focused Scope:** Keep PRs scoped to one cohesive fix or feature.
2. **Deterministic Tests:** Cover happy paths, failure/rollback paths, and tenant RLS boundaries.
3. **Conventional Commits:** Prefix commit titles with `feat:`, `fix:`, `refactor:`, `docs:`, or `chore:`.
4. **Clean Brand Wall:** Public repositories must contain zero references to private downstream organizations or proprietary projects.
