# Contributing to NCE Core

Thank you for contributing to the **Neuro-Cognitive Engine (NCE)**! This document provides instructions for setting up a local development environment, adhering to code quality gates, generating schema artifacts, and respecting core architectural invariants.

---

## The Cardinal Rule: Domain-Agnostic Core

> [!IMPORTANT]
> **Never hardcode vertical engine slugs or industry-specific domain logic in NCE Core.**

NCE Core is strictly a domain-agnostic cognitive memory, reasoning, and multi-tenant substrate.
- Specific business domains (such as Sales, Procurement, System Design, Economy, Odoo, and Salesforce) belong exclusively in external plugin packages such as [`sindrehaugen/nce-vertical-engines`](https://github.com/sindrehaugen/nce-vertical-engines).
- Core interacts with domain extensions solely through declared entry-point interfaces (`nce.plugins`), generic resource surfaces (`nce/resource_surface/`), and standard events (`nce/events.py`).
- Pull requests introducing domain-specific table names, business logic, or vertical module imports into Core will be rejected.

---

## Development Setup

NCE requires **Python 3.10+** (Python 3.12 recommended for runtime alignment) and access to local or containerized backing services (PostgreSQL with `pgvector`, MongoDB, Redis, and MinIO).

### 1. Clone & Create Virtual Environment

```bash
git clone https://github.com/sindrehaugen/neuro-cognitive-engine.git
cd neuro-cognitive-engine

python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Development Dependencies

Install Core in editable mode with development tooling:

```bash
pip install -e ".[dev]"
# Or install dependencies from requirements:
pip install -r requirements.txt -r requirements-dev.txt
```

---

## Code Quality Gates

Every pull request must pass the automated CI quality gates before merge review:

### 1. Linting & Formatting (Ruff)

We use [Ruff](https://astral.sh/ruff) for rapid linting and code formatting:

```bash
# Check code style and rules
ruff check .

# Verify formatting without mutating files
ruff format --check .

# Apply formatting fixes
ruff format .
```

### 2. Static Type Checking (Mypy)

Core enforces strict type hints across the entire codebase:

```bash
mypy nce/
```

### 3. Test Suites (Pytest)

Run unit tests, Row-Level Security checks, and mock-backed orchestrator suites:

```bash
# Run unit and mock tests
pytest tests/

# Run integration tests against running local services
pytest -m integration
```

---

## Schema & Surface Generators

NCE enforces schema drift detection and documentation synchronicity via mechanical code generators. If you modify database migrations, MCP tool definitions, or API route signatures, you must run the corresponding generator scripts:

```bash
# 1. Verify that database migrations match the declarative schema definitions
python scripts/check_schema_drift.py

# 2. Regenerate OpenAPI specifications
python scripts/gen_openapi.py

# 3. Regenerate surface documentation and markdown indexes
python scripts/gen_api_docs.py

# 4. Verify relative markdown links and code fence balance in docs
python scripts/verify_docs_links_and_syntax.py
```

---

## Pull Request Guidelines

1. **Focused Scope:** Keep PRs targeted to a single issue or capability.
2. **Deterministic Tests:** Include automated unit tests covering happy paths, edge cases, and tenant RLS isolation.
3. **Commit Messages:** Follow conventional commit formatting (`feat:`, `fix:`, `refactor:`, `docs:`).
4. **Documentation:** Update relevant markdown files under `docs/` and run `scripts/verify_docs_links_and_syntax.py` before submitting.
