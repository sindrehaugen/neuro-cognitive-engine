> **Status:** shipped · **Verified-against:** 236e4dc (main) · **Last-audited:** 2026-11-19

# System Migrations, Plugin Ledgers & Shadow-Column Re-embedding

NCE manages relational schema evolution through deterministic SQL migration ledgers (`applied_migrations`) and handles embedding-model upgrades through a dual-column ("shadow column") re-embedding pipeline (`nce/reembedding_migration.py`).

---

## 1. Zero-Downtime Shadow-Column Re-embedding

When switching dense embedding models (for example, changing model family or revision while preserving dimension, or migrating vector representations), existing vectors in `memories` must be recomputed without taking read traffic offline.

```mermaid
sequenceDiagram
    participant Admin as "Operator"
    participant Orch as "MigrationOrchestrator"
    participant Worker as "ReembeddingWorker (RQ / Cron)"
    participant DB as "PostgreSQL (memories / reembedding_runs)"
    participant Embed as "Target Embedding Backend"

    Admin->>Orch: start_migration(target_model_v2)
    Orch->>DB: INSERT reembedding_runs (phase='backfilling')

    loop Keyset Cursor Batches
        Worker->>DB: Fetch batch via (cursor_created_at, cursor_id)
        Worker->>Embed: Compute target vectors
        Embed-->>Worker: Batch embeddings
        Worker->>DB: Write shadow column (embedding_v2) + advance cursor
    end

    Admin->>Orch: validate_migration()
    Orch->>Orch: Compute Jaccard top-k neighbor overlap

    alt Neighbor Overlap >= 0.70
        Admin->>Orch: commit_migration()
        Orch->>DB: Swap primary column to embedding_v2
    else Overlap < 0.70
        Admin->>Orch: abort_migration()
        Orch->>DB: Clear embedding_v2 shadow column; retain embedding_v1
    end
```

### Recall Quality Gate (`neighbor_overlap_fraction`)

- `neighbor_overlap_fraction()` in `nce/reembedding_migration.py` evaluates top-$k$ nearest-neighbor sets on a sample query set under both the current and candidate models, computing Jaccard similarity $\frac{|A \cap B|}{|A \cup B|}$.
- By default, `commit_primary_to_v2()` requires overlap `>= 0.70` (`70%`) and raises `RuntimeError` if recall diverges past the threshold.
- `abort_and_clear_pending_v2()` clears `embedding_v2` and marks the run `ABORTED` while leaving active reads untouched.
- Progress checkpoints (`cursor_created_at`, `cursor_id`) are persisted in `reembedding_runs` so interrupted workers resume cleanly without rescanning completed partitions.

---

## 2. Isolated Core & Plugin SQL Migration Ledgers

Schema DDL lives in `nce/schema.sql` (split into one-time `nce/schema_bootstrap.sql` and idempotent per-boot `nce/schema_runtime.sql` via `scripts/split_schema.py`) plus sequential SQL migrations in `nce/migrations/` (`001_enable_rls.sql` through `041_app_connections_vault.sql`, plus `optional/010_citus_sharding.sql`).

To prevent cross-repository migration filename collisions across decoupled packages:

1. **Core Ledger Scope:** Core migrations record bare filenames in `applied_migrations.name` (e.g., `041_app_connections_vault.sql`).
2. **Plugin Ledger Scopes (`nce.plugins`):** External plugin packages (`nce_verticals`, `nce_apps`, or private downstream plugins) return their own migration directory (`get_migration_dir()`) and scope prefix (`get_migration_scope()`), recording entries as `<plugin_scope>:<filename>` (e.g., `nce_apps:001_shared_cad_tables.sql`, `nce_verticals:001_shared_spine.sql`).
3. **Startup Catalog Verification:** After applying migrations, `verify_rls_catalog_consistency()` checks `pg_class` and `pg_policies` across all Core (`39`) and plugin-registered forced-RLS tables (`get_all_plugin_tenant_rls_tables()`), aborting startup on any unforced table or tenant partition.

See [docs/database_architecture.md](database_architecture.md) §9b for the complete 42-file Core migration table. Key structural milestones include:

| Migration | Engineering Milestone |
| :--- | :--- |
| `001_enable_rls.sql` | Creates `nce_app` and `nce_gc` roles; enables `ROW LEVEL SECURITY` and `tenant_isolation_policy` across baseline tenant tables. |
| `007_v3_cognitive_ledger.sql` | Creates `v3_cognitive_ledger` (`vector(6)` optional telemetry table under forced RLS; workplace telemetry hooks remain disabled by default unless `NCE_ENABLE_WORKPLACE_TELEMETRY=true`). |
| `015_memories_envelope_dek.sql` & `035_mcp_gateway_and_envelope_crypto.sql` | Adds `wrapped_dek`, `dek_key_id`, `encryption_deks`, and `mcp_client_tokens` for `AES-256-GCM` KEK/DEK envelope encryption and `/mcp` bearer sessions. |
| `016_halfvec_embeddings.sql` | Converts 768-d embeddings to `halfvec(768)` (`fp16`) HNSW indexes, halving index footprint. |
| `036_hybrid_rrf_procedural_and_worm_lock.sql` | Adds `procedural_heuristics`, FTS GIN sync triggers, and per-namespace `acquire_event_log_worm_lock()` advisory serialization. |
| `038_v351_hotpath_rls_and_indexes.sql` | Marks `get_nce_namespace()` `STABLE PARALLEL SAFE` for `InitPlan` RLS evaluation and adds trigram/partial hot-path indexes. |
| `039_nce_app_runtime_role_grants.sql` & `040_partition_rls_and_key_confinement.sql` | Binds non-superuser `nce_app` runtime grants, enforces forced RLS across all tenant child partitions, and confines `signing_keys` behind `SECURITY DEFINER` helpers. |
| `041_app_connections_vault.sql` | Creates the forced-RLS `app_connections` encrypted credential vault backing `TenantConnectionVault` in `nce.connectors`. |

---

## 3. WORM Ledger Immutability During Schema Evolution

Both `event_log` and `event_parents` enforce append-only immutability across migrations:

- **Role Revocations:** `nce_app` holds `SELECT, INSERT` only; `UPDATE`, `DELETE`, and `TRUNCATE` are revoked on parent and child partitions (`028_worm_truncate_guard.sql`, `039`, `040`).
- **Trigger Guard (`prevent_mutation`):** `trg_event_log_worm` and `trg_event_parents_worm` reject `UPDATE` and `DELETE` unconditionally even when connected as a table owner or superuser.
- **Cross-Database Restore Caution:** Applying schema migrations or restoring backups must keep PostgreSQL `memories.payload_ref` pointers aligned with MongoDB `episodes` and MinIO buckets; see [docs/database_architecture.md](database_architecture.md) for cross-store backup skew considerations.
