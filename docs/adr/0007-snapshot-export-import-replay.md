> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# ADR-0007: Snapshot Export/Import + Observational/Forked Replay

## Status

Shipped

## Context

NCE must support three related but distinct time-travel and portability operations:

1. **Snapshot export/import** — capture a point-in-time view of a namespace (memories + metadata) as a portable NDJSON file and restore it into a different namespace. Required for backup, migration, and disaster recovery.
2. **Observational replay** — stream `event_log` rows for a namespace/sequence range back to a caller without touching engine state. Required for audit, debugging, and compliance inspection.
3. **Forked replay** — replay source events into an isolated target namespace up to a `fork_seq` point. Required for "what-if" analysis: a fork can diverge by serving LLM responses from the original MinIO payload cache (deterministic mode) or by calling the LLM provider fresh (re-execute mode).

All three operations may involve millions of rows. Loading them into Python heap is not viable.

## Decision

**Snapshot export/import (`nce/snapshot_mcp_handlers.py`, `nce/snapshot_serializer.py`)**

`stream_snapshot_export()` is an async generator that yields NDJSON lines. It uses a server-side asyncpg cursor with a batch size of 500 rows (`_STREAM_BATCH_SIZE = 500`), capped at 1,000,000 rows (`_MAX_EXPORT_ROWS = 1_000_000`). A progress line is emitted every 1,000 rows. The full export is never materialised in Python heap.

`handle_import_snapshot()` / `restore_namespace()` parses the NDJSON line by line, routing rows of `type == "memory"` through the Saga write path (`StoreMemoryRequest`) so each imported memory is written through the same transactional guarantees as a live write.

**Note (planned seam):** The import currently generates fresh UUIDs and signatures rather than deterministically remapping source UUIDs. The code comment states: "Reusing deterministic remap once Phase H lands." Until Phase H ships, restored snapshots cannot be cryptographically linked to their source.

**Observational replay (`nce/replay.py` — `ObservationalReplay`)**

`ObservationalReplay.execute()` is an async generator. It opens a server-side asyncpg cursor over `event_log` rows for the requested namespace and sequence range, yielding each row as a dict without writing to any store.

**Forked replay (`nce/replay.py` — `ForkedReplay`)**

`ForkedReplay` replays events into an isolated target namespace. For each source event:
- A new `event_log` entry is written in the target namespace via `append_event()`.
- `parent_event_id` is set to the source event's UUID (alternate causal provenance).
- A new HMAC-SHA256 / ML-DSA signature is computed over the fork's own fields.
- In `deterministic` mode, LLM responses are fetched from MinIO (`event_log.llm_payload_uri`) so the fork is byte-identical to the source run up to the divergence point.
- In `re-execute` mode, the LLM provider is called fresh with optional `config_overrides`.

Fork runs are tracked in a `replay_runs` table. The handler registry validates that every `event_type` in `EventType` has a registered handler at `ForkedReplay.__init__` time, preventing silent skip of unknown event types.

**Source citations** (verified via `git show main:<path>`):
- `nce/snapshot_mcp_handlers.py:1-16` — module docstring: server-side cursor, NDJSON, GB-scale
- `nce/snapshot_mcp_handlers.py:46` — `_STREAM_BATCH_SIZE: int = 500`
- `nce/snapshot_mcp_handlers.py:47` — `_STREAM_PROGRESS_INTERVAL: int = 1000`
- `nce/snapshot_mcp_handlers.py:48` — `_MAX_EXPORT_ROWS: int = 1_000_000`
- `nce/snapshot_mcp_handlers.py:346` — `handle_import_snapshot` — thin adapter calling `restore_namespace`
- `nce/snapshot_mcp_handlers.py:354-358` — `restore_namespace` — NDJSON parse + Saga write path
- `nce/snapshot_mcp_handlers.py:361` — `restore_namespace` docstring: "Reusing deterministic remap once Phase H lands. Until then, this performs a non-verifiable restore" — planned seam
- `nce/replay.py:1-37` — module docstring: Observational (server-side cursor, no heap load) and Forked (target namespace, causal provenance, deterministic/re-execute modes)
- `nce/replay.py:53` — `from minio import Minio` — LLM payload fetch from MinIO for deterministic mode
- `nce/admin_handlers/replay.py:7-70` — `api_replay_observe` — HTTP adapter for observational replay
- `nce/admin_handlers/replay.py:96-154` — `api_snapshot_export` — HTTP adapter for snapshot export
- `nce/admin_handlers/replay.py:156-265` — `api_replay_fork` — HTTP adapter returning `run_id` immediately; poll for status

## Consequences

### Positive

- Server-side cursor streaming means exports of any size complete without OOM; memory usage is bounded to one batch (500 rows) at a time.
- Forked replay in deterministic mode provides byte-identical re-runs without calling the LLM provider, enabling cost-free debugging and regression testing.
- Import via the Saga write path means restored memories carry valid signatures and are subject to the same RLS guarantees as live writes.
- The handler registry at `ForkedReplay.__init__` prevents silent no-ops when new `EventType` values are added without corresponding handlers.

### Negative / Trade-offs

- `_MAX_EXPORT_ROWS = 1_000_000` is a hard cap; tenants with more rows require a batched multi-export workflow.
- Fork runs are long-running operations; the HTTP layer returns a `run_id` immediately and requires polling, which increases client complexity.
- In re-execute mode, forked results are non-deterministic and depend on LLM provider state at fork time.

### Seams (planned/in-flight)

- [planned] Deterministic UUID remapping on import (Phase H): until this lands, imported snapshots generate fresh UUIDs and signatures and cannot be cryptographically linked to the source namespace. Tracked in `nce/snapshot_mcp_handlers.py:361` (`restore_namespace` docstring).
