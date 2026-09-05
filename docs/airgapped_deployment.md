> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Airgapped and Edge Deployment

NCE is designed for high-sovereignty environments where data must never leave the local network. It supports full offline operation and hardware-accelerated local inference.

## The Local Inference Stack

In airgapped mode, NCE replaces external API dependencies with local equivalents:

1. **Local Embeddings**: Uses the `sentence-transformers/all-mpnet-base-v2` model running locally via SentenceTransformer (768-dim, no `trust_remote_code` required) — **stage this model** when preparing an airgapped bundle. The code-specialised `jinaai/jina-embeddings-v2-base-code` remains available through an exported OpenVINO IR bundle; it cannot be used in-process, because its custom modeling code does not load on the pinned `transformers` (see the exporter section below).
2. **Local Cognitive Model**: Uses models like Llama 3 or Mistral running via an OpenAI-compatible HTTP sidecar (e.g. Ollama or `llama.cpp`), pointed to by `NCE_COGNITIVE_BASE_URL`.
3. **Local Databases**: All data persists in locally-hosted PostgreSQL, MongoDB, and MinIO instances.

## Hardware Acceleration: Intel OpenVINO

To achieve production-grade performance on edge hardware without discrete GPUs, NCE integrates with the **Intel OpenVINO** toolkit. This allows embedding models to run on the **Integrated GPU** or **Intel NPU** via pre-exported static-shape IR artifacts.

### Model Export Signal Flow

Before deployment to an airgapped system, export the model from a host that has internet access. The resulting artifacts are then transferred to the airgapped node.

```mermaid
sequenceDiagram
    participant Admin as Administrator
    participant Exporter as nce.openvino_npu_export
    participant HF as Hugging Face Hub
    participant Artifacts as Local Storage

    Admin->>Exporter: export_jina_to_openvino_npu(out_dir, local_files_only=False)
    Exporter->>HF: Download model weights (one-time, internet required)
    Exporter->>Exporter: Reshape to static batch=1, seq=512
    Exporter->>Exporter: Optional compile() for NPU hardware
    Exporter->>Artifacts: Save IR (.xml + .bin) + tokenizer + manifest
    Note over Artifacts: Transfer artifacts to airgapped host; set NCE_OPENVINO_MODEL_DIR.
```

The export function signature (from `nce/openvino_npu_export.py`):

```python
export_jina_to_openvino_npu(
    output_dir,
    *,
    model_id_or_path="jinaai/jina-embeddings-v2-base-code",
    batch_size=1,
    sequence_length=512,
    compile_for_npu=False,
    local_files_only=False,
)
```

Pass `local_files_only=True` when running on the airgapped host itself (prevents any hub calls). The function raises `RuntimeError` if `output_dir` is non-empty, preventing accidental overwrites.

> **Supply-chain note:** `NCE_OPENVINO_MODEL_REVISION` must be set to a commit SHA before running any export. The guard is unconditional: after `model.save_pretrained()` completes (weights already fetched), the exporter raises `RuntimeError` if the revision is unset — regardless of whether a `trust_remote_code` path is involved.

## Configuration for Airgapped Mode

The table below lists the env-vars that govern offline/edge operation. All are read from `nce/config.py` on the main branch.

| Variable | Default | Description |
|---|---|---|
| `NCE_BACKEND` | _(empty, auto-detect)_ | Force an embedding backend. Accepted values: `cpu`, `cuda`, `rocm`, `xpu`, `openvino_npu`, `openvino`, `mps`. `openvino` and `openvino_npu` both route to `OpenVINONPUBackend`. |
| `NCE_OPENVINO_MODEL_DIR` | _(empty)_ | Absolute path to the exported OpenVINO IR directory. Required when using `openvino_npu` / `openvino` backend. |
| `NCE_OPENVINO_SEQ_LEN` | `512` | Token sequence length used at inference time. Must match the `sequence_length` used during export. |
| `NCE_COGNITIVE_BASE_URL` | _(empty)_ | Base URL of an OpenAI-compatible embeddings sidecar (e.g. `http://localhost:11435`). When set and `NCE_BACKEND` is not forced, embeddings route to `POST {base}/v1/embeddings`. |
| `NCE_EMBEDDING_MODEL_ID` | `sentence-transformers/all-mpnet-base-v2` | Hugging Face model ID used by the CPU/CUDA/ROCm/MPS backends. The OpenVINO **exporter** keeps its own default of `jinaai/jina-embeddings-v2-base-code`. |
| `NCE_EMBEDDING_MODEL_REVISION` | _(empty)_ | Optional revision pin for supply-chain safety. Passed to `from_pretrained`. |
| `NCE_EMBEDDING_TRUST_REMOTE_CODE` | `false` | Not needed by the default model. Required only for models shipping custom code, such as the Jina models. Must be explicit in production. |

A minimal `.env` for an airgapped NPU node:

```bash
# Hardware backend — use the pre-exported OpenVINO IR
NCE_BACKEND=openvino_npu

# Path to the exported model artifacts
NCE_OPENVINO_MODEL_DIR=/opt/nce/models/jina-v2-npu

# Sequence length must match the export-time value
NCE_OPENVINO_SEQ_LEN=512

# Local cognitive model sidecar (Ollama / llama.cpp on port 11435)
NCE_COGNITIVE_BASE_URL=http://localhost:11435

# Disable .env file loading in production; secrets come from the orchestrator
NCE_LOAD_DOTENV=false
NCE_ENV=prod
```

> **No `NCE_OFFLINE_MODE` flag exists in the shipped codebase.** Offline behaviour is achieved structurally: `NCE_BACKEND` forces local inference, `NCE_COGNITIVE_BASE_URL` points to a local sidecar, and all datastore URLs point to local instances. There is no separate runtime switch that blocks external HTTP.

## Backend Auto-Detection Order

When `NCE_BACKEND` is not set, `detect_backend()` in `nce/embeddings.py` selects a backend in this order:

1. If `NCE_COGNITIVE_BASE_URL` is set → `CognitiveRemoteBackend` (HTTP sidecar)
2. CUDA available and not ROCm → `CUDABackend`
3. ROCm (AMD) → `ROCmBackend`
4. Intel XPU → `XPUBackend`
5. OpenVINO NPU device detected, or `NCE_OPENVINO_MODEL_DIR` contains `.xml` files → `OpenVINONPUBackend`
6. Apple MPS → `MPSBackend`
7. Fallback → `CPUBackend`

## Data Sovereignty and Security

- **Zero Outbound Telemetry**: NCE has no callbacks to external analytics or licensing servers. All "telemetry" in the codebase refers to internal spreading-activation severity scores used by the knowledge graph engine.
- **On-Premise Storage**: All memory payloads and Knowledge Graph data remain within your infrastructure boundary (PostgreSQL, MongoDB, MinIO).
- **Signing Stays Active**: The cryptographic signing layer (`NCE_MASTER_KEY`, PBKDF2 key derivation, HMAC chain verification) is independent of the deployment mode. All agent interactions are signed and verifiable regardless of whether the node is online or airgapped.
