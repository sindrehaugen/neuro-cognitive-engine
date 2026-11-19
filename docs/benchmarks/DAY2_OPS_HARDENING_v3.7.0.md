# NCE Core `v3.7.0` Day-2 Operations & Chaos Hardening Report

> **Benchmark Scope Notice:** Figures in this report were captured on a controlled single-host Docker container testbed against synthetic R&D datasets. They document engineering verification and regression baselines, not commercial multi-tenant SLA guarantees.

**Release:** `neuro-cognitive-engine` `v3.7.0`  
**Certification Date:** `2026-11-19`  
**Scope:** Copy-on-Write (`CoW`) storage immutability, `T_cut` catalog-watermark cross-store backups, two-phase `crypto_shred` tombstones (`shred_tombstones.jsonl`), ephemeral `nce_drill_*` restore drills, deterministic Maintenance Caddy + isolated `ops-elm` fallback, RQ poison-pill quarantine, 5-tab air-gapped `/admin` dual-auth console, durable `runtime_tool_governance`, and live 19-container chaos verification.  
**Prior Baseline Reports:** [PERF_REPORT_v3.5.2.md](PERF_REPORT_v3.5.2.md) · [HARDENING_READINESS_v3.5.3.md](HARDENING_READINESS_v3.5.3.md) · [HARDENING_READINESS_v3.5.4.md](HARDENING_READINESS_v3.5.4.md) · [HARDENING_READINESS_v3.6.0.md](HARDENING_READINESS_v3.6.0.md).

---

## 1. Live 6-Scenario Chaos & Red-Team Scorecard (`v3.7.0`)

Executed inside the live 19-container Compose stack (`19/19` healthy) via `scripts/verify_day2_chaos_hardening.py`:

| Scenario | Verified Invariants | Observed Telemetry | Result |
| :--- | :--- | :--- | :---: |
| **1. `T_cut` Live Skew, CoW History & GDPR Tombstone Survival** | Active write skew during `pg_dump`, CoW history preservation, HMAC-signed `shred_tombstones.jsonl`, zero deleted-DEK resurrection, orphan post-`T_cut` blob sweep, WORM Merkle chain verification, ephemeral drill target drop | Backup **`2.641 s`** (`1,270,042 bytes` archive); Ephemeral drill **`7.497 s`**; Reconcile **`377.52 ms`**; `missing_pre_cut_pointers=0`, `resurrected_dek_count=0`, `swept_post_cut_blobs=4`, `worm_chain_valid=True`, `cleanup_verified=True` | **PASS** |
| **2. Wrong `NCE_MASTER_KEY` Restore Preflight Block** | Restoring under a perturbed `NCE_MASTER_KEY` aborts with `OpsRestorePreflightError` before creating databases or mutating target rows | `preflight_error_raised=True`, `db_mutation_count=0` | **PASS** |
| **3. Disk Headroom Guard & Stale Drill Sweeper** | Low free space raises `OpsDiskHeadroomError` cleanly; confirm-first playbook (`cleanup_expired_leases_and_drills`) deletes expired `backup_leases` and sweeps `nce_drill_*` PG/Mongo/MinIO leftovers | `headroom_error_raised=True`, `stale_lease_swept=True`, `stale_drill_targets_swept=True` | **PASS** |
| **4. Poison-Pill RQ Job Quarantine** | Synthesized malformed RQ job exhausts retries (`max=1`), records trace-sanitized row in `ops_quarantine`, and exits worker cleanly without crash-looping | `quarantine_exit_status=0`, `failure_count=2`, `record_status="quarantined"` | **PASS** |
| **5. `ops-elm` Prompt-Injection & Secret Fencing** | Raw logs scrubbed of secrets (`NCE_MASTER_KEY`, DSNs) before diagnostic prompt assembly; non-allowlisted playbook suggestions rejected; unreachable `ops-elm` falls back to deterministic brief well below `2.0 s` | `elm_fallback_ms=18.601 ms`, `secret_redacted=True`, `unauthorized_playbook_blocked=True` | **PASS** |
| **6. Dual-Auth `/admin`, CSRF, HMAC Replay & Redis `FLUSHALL`** | Unauthenticated admin API blocked (`401`); cookie session mutation without `X-NCE-CSRF-Token` blocked (`403`) and allowed (`200`) with token; duplicate HMAC nonce rejected (`401`); tool/plugin kill-switches re-hydrate from PostgreSQL after `FLUSHALL` | `healthz_p50_ms=1.123 ms` (`n=30` suite) / `0.616 ms` (`n=50` direct probe, `p95=0.981 ms`); `survived_redis_flushall=True` | **PASS** |

---

## 2. Schema Drift, Role Confinement & Runtime Footprint

- **Live Schema Drift Check (`scripts/check_schema_drift.py` against `nce-postgres`):**
  - `41` `nce/schema.sql` tables + `10` migration-created tables = **`51` Core SQL tables**, **`NO DRIFT`** (`exit_code=0`).
  - `39` Core tables enforce `FORCE ROW LEVEL SECURITY` (`38` single-tenant tables + `a2a_grants`), while `10` global system/control-plane tables isolate operational state (`042_day2_ops_backup_and_governance.sql` grants read/write to `nce_gc` and revokes direct table privileges from `nce_app`, exposing narrow `SECURITY DEFINER` helpers `nce_get_disabled_runtime_tools` and `nce_is_backup_lease_active`).
  - Leaf-partition autovacuum tuning (`autovacuum_vacuum_scale_factor = 0.02`, `autovacuum_analyze_scale_factor = 0.02`) applied dynamically across partitioned `memory_salience` and `kg_edges` leaf partitions plus `ops_probe_runs`.
- **Container Memory, Seccomp & Air-Gapped Console Verification (`docker stats --no-stream`):**
  - `nce-cron`: **`150.2 MiB / 512 MiB`** (`29.33%` of limit post-suite; `140.8 MiB` idle baseline), **`Seccomp: 2`** (`Seccomp_filters: 2`).
  - `nce-admin`: `348.5 MiB / 1 GiB`, **`Seccomp: 2`**, serving all **5 tabs** (`overview`, `backups`, `ops`, `tenants`, `plugins`) with **`0` external HTTP/HTTPS CDN URLs** (`admin/index.html` `21,119` bytes, `admin/console.js` `49,739` bytes).
  - `neuro-cognitive-engine-worker-1` / `worker-2`: `54.46 MiB` / `45.9 MiB`, **`Seccomp: 2`**.
  - `nce-mcp-gateway`: `103.5 MiB / 1 GiB`.
  - **Mounted Ecosystem Plugin Census (`/admin/plugins/api`):** **`50` engines discovered** (`25` `stable`, `25` `beta`, `0` `experimental`).
