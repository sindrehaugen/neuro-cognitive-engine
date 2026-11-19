> **Version:** `v3.7.0` · **Status:** shipped · **Last-audited:** 2026-11-19

# Day-2 Operations, Maintenance Caddy & `/admin` Control Plane (`v3.7.0`)

NCE Core `v3.7.0` ships a deterministic maintenance scheduler (**Maintenance Caddy**), optional isolated diagnostic sidecar (`ops-elm`), RQ poison-pill quarantine, and an air-gapped 5-tab `/admin` engineering console backed by PostgreSQL control-plane tables (`042_day2_ops_backup_and_governance.sql`).

---

## 1. Zero-`docker.sock` Deterministic Maintenance Caddy (`nce/ops/caddy.py`, `nce/cron.py`)

Maintenance Caddy runs on scheduled intervals inside `nce-cron` (`Seccomp: 2`, `NCE_GC_DSN` / `nce_gc` role, **zero** Docker daemon socket mount):

| Cadence | Checks / Actions | Persistent Destination |
| :--- | :--- | :--- |
| **Every 5 min** | PostgreSQL connection pool headroom, RQ queue depth (`nce-tasks`), salience buffer length (`NCE_SALIENCE_BUFFER_MAX_LEN=50000`, `24h` TTL), disk headroom (`NCE_OPS_MIN_FREE_BYTES`), open `ops_quarantine` count, database health, and backup freshness (`NCE_BACKUP_MAX_AGE_HOURS=26`) | `ops_probe_runs` & deduplicated `ops_incidents` |
| **Nightly** | Sweep expired `backup_leases`, remove leftover `nce_drill_*` databases/buckets, drain `drain_deferred_shred_gc()`, and verify manifest freshness | `ops_probe_runs` / `backup_manifests` |
| **Weekly (or `NCE_BACKUP_AUTO_DAILY`)** | Trigger `create_watermark_backup` + `run_ephemeral_restore_drill` | `backup_manifests` & WORM `_system` `event_log` |

When an incident fingerprint opens or escalates, Maintenance Caddy invokes `nce/ops/elm.py`:
- **Secret & Injection Fencing:** Scrubs credentials/DSNs via `redact_secrets_in_text()` and fences raw log snippets inside explicit data delimiters before diagnosis.
- **Strict Pydantic Brief & Playbook Allowlist:** Outputs validate against `ELMDiagnosticBrief`; any `recommended_playbook_id` outside `PLAYBOOK_REGISTRY` is stripped to `None`.
- **Optional Sidecar (`--profile ops-elm`) & Deterministic Fallback:** Supports `NCE_OPS_ELM_MODE=local` (`qwen2.5:1.5b-instruct-q4_K_M`, `OLLAMA_KEEP_ALIVE=2m`, rate-capped by `NCE_OPS_ELM_MAX_CALLS_PER_HOUR=4`), `provider`, or `deterministic`. If `ops-elm` is disabled or unreachable, synthesis falls back cleanly in `< 2.0 s` without disrupting `nce-cron` RSS (`< 180 MiB`).

---

## 2. `@governed` Repair Playbooks & RQ Poison-Pill Quarantine

All repair playbooks (`nce/ops/playbooks.py`) enforce **two-step confirm-first** execution (`preview_playbook` -> signed single-use confirmation token -> `execute_playbook` -> `ops_playbook_executions` audit row):

1. `drain_salience_buffer_or_trim_redis` — flush buffered salience metrics cleanly to PostgreSQL and clamp Redis buffer/TTL bounds.
2. `reap_stuck_saga_and_reclaim_rq_orphans` — recover stalled writes and orphan RQ workers via `SKIP LOCKED`.
3. `cleanup_expired_leases_and_drills` — expire stale `backup_leases`, flush buffers, and sweep stale `nce_drill_*` targets across PG/Mongo/MinIO/disk.
4. `quarantine_or_requeue_job` — requeue or dismiss rows in `ops_quarantine`.
5. `run_immediate_backup_or_drill` — run headroom-checked immediate `T_cut` backup or ephemeral restore drill.

**Poison-Pill Worker Quarantine (`nce/ops/quarantine.py`, `start_worker.py`):** `RecoveringWorker` captures repeated failing RQ jobs (`>= 2` attempts) and worker SIGSEGV/OOM inflight deaths, scrubs tracebacks, persists deduplicated records into `ops_quarantine`, and lets the worker container exit or advance without infinite poison-pill crash-loops.

---

## 3. Air-Gapped 5-Tab `/admin` Console & Pure-ASGI Dual-Auth

Served by `admin_server.py` (`admin/index.html`, `admin/plugins.html`, `admin/console.js`, `admin/console.css`, **0** external CDN links, **0** `innerHTML`):

1. **Tab 1 — Overview:** Live Quad-Store probes, `FORCE RLS` catalog count, disk headroom gauge, RQ queue/salience metrics, and cognitive/`ops-elm` status.
2. **Tab 2 — Backups & Drills:** Manifest history, `T_cut`/SHA-256/`master_key_hmac_fp` inspection, last drill parity metrics, and confirm-first backup/drill triggers.
3. **Tab 3 — Ops Caddy & Quarantine:** Probe runs (`ops_probe_runs`), deduplicated incidents (`ops_incidents`), `ELMDiagnosticBrief` evidence cards, `@governed` playbook previews/executions, and RQ quarantine requeue/dismiss actions.
4. **Tab 4 — Tenants & MCP Onboarding:** Namespace creation, scoped `/mcp` token minting, and ready-to-copy client config snippets.
5. **Tab 5 — Plugins, Tools & Connections:** Full `v3.6.0` `Stable` / `Beta` / `Experimental` maturity switcher, encrypted `TenantConnectionVault` credentials modal, and runtime MCP tool/A2A skill/plugin kill-switches persisted to PostgreSQL `runtime_tool_governance` (automatically re-hydrating Redis after `FLUSHALL`).

**Dual-Auth Spine (`nce/admin_ops.py::AdminDualAuthMiddleware`):**
- **Browser Session Mode:** `POST /admin/session/login` (`NCE_ADMIN_API_KEY` or username + `$pbkdf2$`/`Argon2id` password) sets an `HttpOnly`, `SameSite=Strict` signed `nce_admin_session` cookie plus `nce_admin_csrf` cookie, requiring matching `X-NCE-CSRF-Token` headers on mutating routes (`POST`/`PUT`/`PATCH`/`DELETE`).
- **Programmatic API / Entra Mode:** Preserves 3-header `HMAC-SHA256` + Redis nonce validation (`X-NCE-Timestamp`, `X-NCE-Nonce`, `Authorization: HMAC-SHA256 ...`) and strict Entra OIDC Bearer JWT verification (`NCE_ENTRA_ENFORCE_MODE=strict`), keeping `/healthz` latency sub-millisecond.

See also: [Backup, CoW Storage & Ephemeral Restore Drills](backup_and_recovery.md) · [Configuration Reference](configuration_reference.md) · [Hardening Report v3.7.0](benchmarks/DAY2_OPS_HARDENING_v3.7.0.md).
