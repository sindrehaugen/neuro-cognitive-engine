> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for NCE / TriMCP-1. Each ADR documents a significant, load-bearing design decision: the context that drove it, the decision taken, and the consequences — positive and negative.

ADRs are retroactive where the decision has already shipped. In-flight or planned items are marked explicitly inline.

## Index

| Number | Title | Status |
|--------|-------|--------|
| [0000](0000-template.md) | ADR Template | — |
| [0001](0001-worm-event-log.md) | WORM immutability for event_log via prevent_mutation trigger | Shipped |
| [0002](0002-quad-db-stack.md) | Quad-DB stack: asyncpg + Motor + Redis + MinIO | Shipped |
| [0003](0003-forced-rls-scoped-session.md) | Forced RLS + scoped_pg_session for tenant isolation | Shipped |
| [0004](0004-cryptographic-signing-v2.md) | Cryptographic signing v2: Argon2id key wrap + ML-DSA-44 + Merkle chain hash | Shipped |
| [0005](0005-shadow-column-reembedding.md) | Shadow-column re-embedding migration (embedding_v2) | Shipped |
| [0006](0006-env-only-nce-master-key.md) | Environment-only NCE_MASTER_KEY (_ENV_ONLY_SECRETS) | Shipped |
| [0007](0007-snapshot-export-import-replay.md) | Snapshot export/import + observational/forked replay | Shipped |

## Format

Each ADR follows the template in `0000-template.md`. New ADRs are numbered sequentially. Once accepted, the status progresses: **Proposed → Accepted → Shipped → Superseded**.

## Adding a new ADR

1. Copy `0000-template.md` to the next number, e.g. `0008-my-decision.md`.
2. Fill every section. Leave no placeholders.
3. Verify every claim against `git show main:<path>` before marking Shipped.
4. Add a row to the index table above.
5. Do **not** edit `docs/_sidebar.md` — the orchestrator places navigation.
