> **Version:** `v3.7.0` · **Status:** shipped · **Last-audited:** 2026-11-19

# Backup, CoW Storage & Ephemeral Restore Drills (`v3.7.0`)

NCE coordinates state across PostgreSQL 16 (`pgvector`), MongoDB 7.0, Redis 7.4, and MinIO. Rather than claiming distributed atomic cross-store locks, **NCE Core `v3.7.0`** enforces crash-consistent **`T_cut` catalog-watermark backups** backed by Copy-on-Write (`CoW`) object immutability, signed GDPR erasure tombstones, and isolated single-host ephemeral restore drills (`nce_drill_*`).

---

## 1. `T_cut` Catalog-Watermark Backup Protocol

Executed via `nce/ops/backup.py::create_watermark_backup`, `nce-ops backup`, `make backup`, or weekly `nce/cron.py`:

1. **Disk Headroom Preflight:** Verifies free space on `NCE_BACKUP_DIR` satisfies `free >= max(NCE_OPS_MIN_FREE_BYTES, 3 * estimated_dataset_bytes)`; fails fast with `OpsDiskHeadroomError` before staging archives.
2. **TTL-Bounded Lease (`backup_leases`):** Acquires lease `t_cut_backup` with a hard `<= 30 min` TTL (`expires_at <= now()` auto-expires on host crash) under `nce_gc` / `NCE_MIGRATION_PG_DSN`. Active leases pause physical ciphertext GC (`drain_deferred_shred_gc`) so in-flight backups cannot lose pre-`T_cut` payloads referenced before compaction.
3. **PostgreSQL `T_cut` Snapshot Capture:** Opens a `REPEATABLE READ READ ONLY` transaction under `NCE_MIGRATION_PG_DSN` (`mcp_user`), captures `T_cut = transaction_timestamp()`, `schema_heads` (`applied_migrations`), `worm_heads` (per-namespace `max(event_seq)`, `chain_hash` hex, and `merkle_valid_at_cut` status), and runs `pg_dump -Fc -Z zstd:3`.
4. **Sequential Blob Export (`PG -> MongoDB -> MinIO`):** Exports `memory_archive` collections (indexed on `(created_at ASC)` and stamped with `saga_id`) and MinIO buckets (`x-amz-meta-created-at`, `x-amz-meta-saga-id`, excluding transient `nce-drill-*` buckets).
5. **Dual-Check & Signed Packaging:** Packages `pg.dump`, `mongo/`, `minio/`, active `shred_tombstones.jsonl`, and an RFC 8785 (JCS) `HMAC-SHA256` signed manifest (`master_key_hmac_fp`, `archive_sha256`, `file_digests`) into `nce-backup-<timestamp>-<backup_id>.tar.zst` (or `.tar.gz`), records the row in `public.backup_manifests`, and prunes old archives beyond `NCE_BACKUP_RETENTION_COUNT` only after digest verification succeeds.

---

## 2. Copy-on-Write (`CoW`) Storage & Two-Phase `crypto_shred`

- **CoW Payload Immutability:** Logical memory revisions mint fresh PostgreSQL temporal rows (`valid_from` / `valid_to`), fresh MongoDB `ObjectId`s (never mutating `episodes.raw_data` in place), and versioned SHA-256 MinIO keys (`build_versioned_cow_object_key`). Point-in-time `as_of` reads and `T_cut` backups always observe immutable pre-cut blobs.
- **Two-Phase `crypto_shred` & Zero Resurrection:** Calling `forget_memory` / `shred_memory`:
  1. Immediately nulls `wrapped_dek`, `dek_key_id`, `content_fts`, and `embedding` inside the PostgreSQL transaction (`encryption_deks.wrapped_key = 0x00*32`) and sets `valid_to = now()`.
  2. Atomically appends (`fsync`) a zero-PII HMAC-signed line (`namespace_id`, `dek_id`, `mongo_ids`, `minio_keys`, `shredded_at`, `hmac_sha256`) to `<NCE_BACKUP_DIR>/shred_tombstones.jsonl`.
  3. Checks `nce_is_backup_lease_active('t_cut_backup')`: if active, enqueues physical MongoDB/MinIO ciphertext deletion into Redis `nce:ops:deferred_shreds` (fallback `deferred_shreds.jsonl`) for `drain_deferred_shred_gc()`; otherwise deletes ciphertext immediately.

---

## 3. Preflight Validation & Ephemeral Restore Drills (`nce_drill_*`)

`restore_watermark_backup` and `run_ephemeral_restore_drill` (`nce-ops drill`, `nce-ops restore-drill`, `make restore-drill`):

- **Fail-Closed Preflight (`OpsRestorePreflightError`):** Rejects archive SHA-256 mismatches, invalid manifest/member digests, `NCE_MASTER_KEY` fingerprint mismatches (`compute_master_key_hmac_fp()`), or incompatible schema migration heads **before** mutating target databases.
- **Tombstone Replay & Bounded Reconciliation:** Replays `<NCE_BACKUP_DIR>/shred_tombstones.jsonl` + archived tombstones after loading PG/Mongo/MinIO so DEKs erased *after* `T_cut` cannot resurrect on restore (`resurrected_dek_count == 0`). Sweeps strictly orphan post-`T_cut` blobs (`created_at > T_cut` without a committed `memories` row) while preserving all `created_at <= T_cut` CoW history, and verifies WORM `event_log` Merkle chains via `verify_merkle_chain()`.
- **Isolated Single-Host Drill Targets:** Creates ephemeral PostgreSQL `nce_drill_<12hex>` (using `NCE_MIGRATION_PG_DSN`), MongoDB `nce_drill_<12hex>`, and underscore-sanitized MinIO `nce-drill-<12hex>-*` buckets (stripping non-user `x-amz-object-lock-*` response headers so ephemeral buckets drop cleanly), updates `backup_manifests`, appends a signed WORM `config_changed` audit event on `_system`, and tears down all drill targets inside `finally` (with `sweep_stale_drill_targets` cleanup on startup/nightly runs).

---

## 4. Operator CLI & Failure-Domain Boundaries

```bash
make backup                         # Runs nce-ops backup inside nce-cron (or host fallback)
make restore-drill                  # Runs isolated ephemeral restore drill and drops nce_drill_*
nce-ops backup --json               # Direct CLI JSON output
nce-ops drill --backup-id <uuid>    # Drill a specific manifest UUID (`restore-drill` alias supported)
```

> [!NOTE]
> **Single-Host Trade-Offs:** `T_cut` watermark backups tolerate live writes with deterministic crash consistency across single-host services, not synchronous cross-datacenter consensus snapshots. Backups stored solely on the host volume protect against logical corruption and PostgreSQL/Mongo/MinIO bugs; copy archives off-host for hardware/disk loss resilience, and store `<NCE_BACKUP_DIR>` access credentials separately from `NCE_MASTER_KEY`.

See also: [Day-2 Operations & Maintenance Caddy](day2_operations_caddy.md) · [Database Architecture](database_architecture.md) · [Hardening Report v3.7.0](benchmarks/DAY2_OPS_HARDENING_v3.7.0.md).
