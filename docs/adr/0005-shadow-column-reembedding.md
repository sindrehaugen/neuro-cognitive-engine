> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# ADR-0005: Shadow-Column Re-embedding Migration (embedding_v2)

## Status

Shipped

## Context

NCE's semantic search depends on vector embeddings stored alongside memories. When an embedding model is upgraded (e.g. a new OpenAI model or a dimension change), all stored embeddings must be recomputed. The naive approach — truncate and re-embed — creates a window where semantic search returns no results or degraded results.

A zero-downtime model migration requires:
1. The old embeddings remain the serving vector while the new ones are computed in the background.
2. A quality gate verifies the new embeddings are semantically equivalent before the cutover.
3. The cutover is atomic: no query ever observes a half-migrated state.
4. The migration is resumable: if the worker is killed mid-run, it continues from where it left off.

## Decision

**Strategy A: shadow-column fill, then atomic logical swap.**

`nce/reembedding_migration.py` implements an in-memory store (`InMemoryEmbeddingStore`) and a worker (`EmbeddingMigrationWorker`) with the following phases:

1. **Fill phase** — the worker reads `memories` rows whose `embedding_model_id` differs from the current model version (keyset cursor: `created_at ASC, id ASC`) and writes new embeddings into the `embedding_v2` shadow slot via `write_embedding_v2()`. Search/recall reads continue to use `embedding_v1` (the `active_embedding()` method always returns `embedding_v1`).

2. **Quality gate** — before committing, `commit_primary_to_v2()` calls an optional `quality_gate_fn`. The default gate computes Jaccard similarity (`neighbor_overlap_fraction`) between old and new neighbour-ID sets. A threshold of 0.7 is required; failure raises `RuntimeError` and blocks the commit.

3. **Atomic logical swap** — `commit_primary_to_v2()` iterates all rows and promotes `embedding_v2` into `embedding_v1`, clearing `embedding_v2`. After this point search reads the new vectors. The operation is a Python-layer atomic swap; no query observes a `None` embedding during the swap.

4. **Resumability** — migration progress is checkpointed in the `reembedding_runs` table (created by migration `012_reembedding_runs.sql`). The cursor position (`cursor_created_at`, `cursor_id`) is saved after every batch. Re-runs skip already-updated rows via the `embedding_model_id != current` WHERE clause.

The cron scheduler (`nce/cron.py`) runs `_reembedding_tick` as `phase_2_1_reembedding` on a configurable interval.

**Source citations** (verified via `git show main:<path>`):
- `nce/reembedding_migration.py:2` — "Strategy A, dimension-compatible" in module docstring
- `nce/reembedding_migration.py:108-109` — `embedding_v2: list[float] | None` and `embedding_v2_target_model_id` fields on `MemoryEmbeddingRow`
- `nce/reembedding_migration.py:126` — `write_embedding_v2()` — fills shadow slot
- `nce/reembedding_migration.py:319` — docstring: "embedding_v2 in the shadow slot; after commit_primary_to_v2, the promoted vectors live in embedding_v1 again — reads never observe a hole"
- `nce/reembedding_migration.py:295-313` — `InMemoryReembeddingStore.write_embedding_v2` — shadow fill implementation
- `nce/reembedding_migration.py:340-370` — `commit_primary_to_v2` — quality gate check and atomic logical swap
- `nce/migrations/012_reembedding_runs.sql:1-35` — `reembedding_runs` table: `cursor_created_at`, `cursor_id` checkpointing columns
- `nce/cron.py:801` — APScheduler job `id="phase_2_1_reembedding"` — cron registration
- `nce/reembedding_migration.py:492` — `unmanaged_pg_connection(self.pool, site="reembedding.aspects.backfill")` — audited RLS bypass for background worker

## Consequences

### Positive

- Semantic search never observes a missing or stale embedding during migration; `embedding_v1` is always valid.
- The quality gate (Jaccard neighbour overlap >= 0.7) prevents silent semantic drift from being committed.
- Resumable via keyset cursor; large tenants can be migrated across multiple worker restarts.
- `reembedding_runs` provides an audit trail of every migration with start/completion times and row counts.

### Negative / Trade-offs

- The shadow-column approach requires holding both `embedding_v1` and `embedding_v2` simultaneously, doubling vector storage per row during migration.
- The in-memory store (`InMemoryEmbeddingStore`) is used for the migration orchestration logic; the actual Postgres write path (`InMemoryReembeddingStore`) must be kept in sync with it.
- The quality gate threshold (0.7) is a fixed default; operators cannot currently adjust it at runtime without code changes.
- The `active_embedding()` method always returns `embedding_v1`; a per-row flag would be needed to support partially-committed states in the future.
