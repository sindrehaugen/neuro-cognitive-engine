> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# NCE Observability — VRAM Metrics (Item 49)

## Overview

The re-embedding background worker (`nce/re_embedder.py`) runs PyTorch/CUDA operations during embedding migrations. After fixing an OOM leak, we now actively monitor VRAM consumption via Prometheus gauges to detect memory pressure before it causes OOM kills.

## Metrics Exposed

| Metric Name | Type | Labels | Description |
|---|---|---|---|
| `nce_reembedder_vram_allocated_bytes` | Gauge | `worker_id` | Current VRAM allocated to PyTorch tensors (`torch.cuda.memory_allocated()`) |
| `nce_reembedder_vram_reserved_bytes` | Gauge | `worker_id` | Current VRAM reserved by the CUDA caching allocator (`torch.cuda.memory_reserved()`) |
| `nce_reembedder_vram_peak_bytes` | Gauge | `worker_id` | Peak VRAM allocated since last measurement reset (`torch.cuda.max_memory_allocated()`) |

## Measurement Cadence

Metrics are recorded after every embedding batch (memories and KG nodes) inside `_release_embedding_batch_memory()`. That function calls `_record_vram_metrics()`, which reads all three CUDA counters and resets the peak allocator stat via `torch.cuda.reset_peak_memory_stats()` — giving independent per-batch peak windows. `_release_embedding_batch_memory()` then runs `gc.collect()` and `torch.cuda.empty_cache()` to return unused blocks to the CUDA allocator.

## Graceful Degradation

- **CPU-only**: When `torch.cuda.is_available()` returns `False`, `_record_vram_metrics()` returns immediately — no metrics emitted.
- **torch missing**: `ImportError` (or `RuntimeError`) is caught silently inside `_record_vram_metrics()`; metrics are skipped.
- **prometheus_client missing**: The `_StubMetric` fallback in `nce/observability.py` handles absent Prometheus — `.set()` is a no-op.

## Alert Thresholds (Recommended)

| Condition | Alert | Severity |
|---|---|---|
| `nce_reembedder_vram_allocated_bytes > 80% GPU total` | Re-embedder nearing OOM | Warning |
| `nce_reembedder_vram_peak_bytes > 90% GPU total` | High-water mark critical | Critical |
| `nce_reembedder_vram_reserved_bytes - nce_reembedder_vram_allocated_bytes > 2GB` | CUDA allocator fragmentation | Warning |

### Example Prometheus Alert Rule

```yaml
groups:
  - name: nce_vram
    rules:
      - alert: TrimcpReembedderHighVRAM
        expr: nce_reembedder_vram_allocated_bytes / 1024 / 1024 / 1024 > 0.8 * nvidia_gpu_memory_total_bytes
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Re-embedder VRAM usage > 80% of GPU total"
```

## Docker GPU Configuration

GPU access for the re-embedder is declared in two compose files:

**`docker-compose.yml` (root, self-hosted stack)** — the `worker` service is gated behind the `gpu` compose profile and carries NVIDIA device reservations:

```yaml
worker:
  profiles:
    - gpu
  deploy:
    resources:
      limits:
        memory: 1G
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```

To start the GPU-enabled worker locally:

```bash
docker compose --profile gpu up worker
```

**`deploy/multiuser/docker-compose.yml`** — the `worker` service carries the same NVIDIA device reservation block without a profile guard (GPU is always enabled in the multiuser deployment):

```yaml
worker:
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```

For both compose files, ensure `nvidia-container-toolkit` is installed and `/etc/docker/daemon.json` registers the NVIDIA runtime:

```json
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  }
}
```

## Grafana Dashboard Panel

Add a timeseries panel with queries:

```
nce_reembedder_vram_allocated_bytes{worker_id=~"$worker"}
nce_reembedder_vram_reserved_bytes{worker_id=~"$worker"}
nce_reembedder_vram_peak_bytes{worker_id=~"$worker"}
```

Display in GiB: divide by `1024*1024*1024`.
