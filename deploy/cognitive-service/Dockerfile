# Air-gapped CPU cognitive service: ONNX Runtime (FP32 + INT8) / PyTorch all-mpnet-base-v2 (768-d) + spaCy en_core_web_sm
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/tmp/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/tmp/hf_cache

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY deploy/cognitive-service/requirements.txt /tmp/requirements.txt

# Install CPU-only PyTorch wheel first (avoids ~4 GB NVIDIA CUDA layers),
# then install pinned cognitive dependencies from requirements.txt.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.6.0,<3.0.0" && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt

# Bake SentenceTransformer + optimized FP32/INT8 ONNX models under /opt/nce/models
# and verify 768-d unit-norm inference + PyTorch/ONNX cosine parity >= 0.9999 offline.
RUN printf '%s\n' \
    'import json' \
    'import os' \
    'from pathlib import Path' \
    'import numpy as np' \
    'import onnxruntime as ort' \
    'import spacy' \
    'import torch' \
    'from onnxruntime.quantization import QuantType, quantize_dynamic' \
    'from onnxruntime.quantization.shape_inference import quant_pre_process' \
    'from sentence_transformers import SentenceTransformer' \
    'from transformers import AutoTokenizer' \
    '' \
    'MODEL_ID = "sentence-transformers/all-mpnet-base-v2"' \
    'MODELS_ROOT = Path("/opt/nce/models")' \
    'ST_DIR = MODELS_ROOT / "all-mpnet-base-v2"' \
    'ONNX_DIR = MODELS_ROOT / "all-mpnet-base-v2-onnx"' \
    'RAW_ONNX = ONNX_DIR / "model_raw.onnx"' \
    'PREP_ONNX = ONNX_DIR / "model_prep.onnx"' \
    'OPT_ONNX = ONNX_DIR / "model.onnx"' \
    'QINT8_ONNX = ONNX_DIR / "model_qint8.onnx"' \
    '' \
    'ST_DIR.mkdir(parents=True, exist_ok=True)' \
    'ONNX_DIR.mkdir(parents=True, exist_ok=True)' \
    '' \
    'nlp = spacy.load("en_core_web_sm")' \
    'assert nlp("NCE cognitive service build check").ents is not None' \
    '' \
    'st = SentenceTransformer(MODEL_ID, device="cpu")' \
    'st.save(str(ST_DIR))' \
    '' \
    'max_seq_len = int(getattr(st, "max_seq_length", 384) or 384)' \
    'dim = int(st.get_sentence_embedding_dimension() or 768)' \
    'assert dim == 768, f"Expected dim=768, got {dim}"' \
    '' \
    'class _ExportWrapper(torch.nn.Module):' \
    '    def __init__(self, mod: torch.nn.Module) -> None:' \
    '        super().__init__()' \
    '        self.mod = mod' \
    '    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:' \
    '        return self.mod(input_ids=input_ids, attention_mask=attention_mask, return_dict=True).last_hidden_state' \
    '' \
    'wrapper = _ExportWrapper(st[0].auto_model.eval()).eval()' \
    'dummy_ids = torch.ones((2, 16), dtype=torch.long)' \
    'dummy_mask = torch.ones((2, 16), dtype=torch.long)' \
    'with torch.no_grad():' \
    '    torch.onnx.export(' \
    '        wrapper,' \
    '        (dummy_ids, dummy_mask),' \
    '        str(RAW_ONNX),' \
    '        dynamo=False,' \
    '        opset_version=17,' \
    '        input_names=["input_ids", "attention_mask"],' \
    '        output_names=["last_hidden_state"],' \
    '        dynamic_axes={' \
    '            "input_ids": {0: "batch", 1: "seq"},' \
    '            "attention_mask": {0: "batch", 1: "seq"},' \
    '            "last_hidden_state": {0: "batch", 1: "seq"},' \
    '        },' \
    '    )' \
    '' \
    'opt_opts = ort.SessionOptions()' \
    'opt_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED' \
    'opt_opts.optimized_model_filepath = str(OPT_ONNX)' \
    'ort.InferenceSession(str(RAW_ONNX), sess_options=opt_opts, providers=["CPUExecutionProvider"])' \
    '' \
    'quant_src = RAW_ONNX' \
    'try:' \
    '    quant_pre_process(str(RAW_ONNX), str(PREP_ONNX), skip_symbolic_shape=False, auto_merge=True)' \
    '    if PREP_ONNX.is_file():' \
    '        quant_src = PREP_ONNX' \
    'except Exception as exc:' \
    '    print(f"quant_pre_process skipped ({exc}); quantizing raw ONNX directly")' \
    'quantize_dynamic(' \
    '    model_input=str(quant_src),' \
    '    model_output=str(QINT8_ONNX),' \
    '    op_types_to_quantize=["MatMul"],' \
    '    weight_type=QuantType.QInt8,' \
    '    per_channel=True,' \
    '    reduce_range=True,' \
    '    extra_options={"MatMulConstBOnly": True},' \
    ')' \
    '' \
    'for tmp_file in (RAW_ONNX, PREP_ONNX):' \
    '    if tmp_file.exists():' \
    '        tmp_file.unlink()' \
    '' \
    'st.tokenizer.save_pretrained(str(ONNX_DIR))' \
    '(ONNX_DIR / "metadata.json").write_text(' \
    '    json.dumps({"model_id": MODEL_ID, "dimension": dim, "max_seq_length": max_seq_len, "opset_version": 17, "pooling": "mean", "normalize": True}, indent=2) + "\n",' \
    '    encoding="utf-8",' \
    ')' \
    '' \
    'tok = AutoTokenizer.from_pretrained(str(ONNX_DIR), local_files_only=True)' \
    'sentences = [' \
    '    "NCE cognitive build verification sentence.",' \
    '    "PostgreSQL row-level security enforces multi-tenant isolation across partitioned tables.",' \
    '    "async def embed_batch(texts: list[str]) -> list[list[float]]: return []",' \
    '    "Short text.",' \
    '    "A longer semantic paragraph comparing vector cosine similarity, L2 normalization, and attention-mask weighted mean pooling over padded sequences.",' \
    ']' \
    'def _pool_and_norm(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:' \
    '    h = np.asarray(hidden, dtype=np.float32)' \
    '    m = np.asarray(mask, dtype=np.float32)[:, :, None]' \
    '    summed = np.sum(h * m, axis=1, dtype=np.float32)' \
    '    counts = np.maximum(np.sum(m, axis=1, dtype=np.float32), np.float32(1e-9))' \
    '    pooled = (summed / counts).astype(np.float32)' \
    '    norms = np.maximum(np.linalg.norm(pooled, ord=2, axis=-1, keepdims=True), np.float32(1e-12))' \
    '    return (pooled / norms).astype(np.float32)' \
    '' \
    'pt_vecs = np.asarray(st.encode(sentences, normalize_embeddings=True, convert_to_numpy=True), dtype=np.float32)' \
    'assert pt_vecs.shape == (5, 768)' \
    'encoded = tok(sentences, padding=True, truncation=True, max_length=max_seq_len, return_tensors="np")' \
    'feed = {"input_ids": np.asarray(encoded["input_ids"], dtype=np.int64), "attention_mask": np.asarray(encoded["attention_mask"], dtype=np.int64)}' \
    'run_opts = ort.SessionOptions()' \
    'run_opts.intra_op_num_threads = min(4, os.cpu_count() or 4)' \
    'run_opts.inter_op_num_threads = 1' \
    'run_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL' \
    'run_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL' \
    '' \
    'fp32_sess = ort.InferenceSession(str(OPT_ONNX), sess_options=run_opts, providers=["CPUExecutionProvider"])' \
    'fp32_vecs = _pool_and_norm(fp32_sess.run(["last_hidden_state"], feed)[0], feed["attention_mask"])' \
    'assert fp32_vecs.shape == (5, 768)' \
    'fp32_min_cos = 1.0' \
    'for idx in range(5):' \
    '    norm = float(np.linalg.norm(fp32_vecs[idx]))' \
    '    assert np.all(np.isfinite(fp32_vecs[idx])), f"Non-finite FP32 vector at {idx}"' \
    '    assert abs(norm - 1.0) < 1e-5, f"FP32 norm drift at {idx}: {norm}"' \
    '    cos = float(np.dot(pt_vecs[idx], fp32_vecs[idx]))' \
    '    fp32_min_cos = min(fp32_min_cos, cos)' \
    '    assert cos >= 0.9999, f"FP32 cosine below 0.9999 at {idx}: {cos}"' \
    '' \
    'q8_sess = ort.InferenceSession(str(QINT8_ONNX), sess_options=run_opts, providers=["CPUExecutionProvider"])' \
    'q8_vecs = _pool_and_norm(q8_sess.run(["last_hidden_state"], feed)[0], feed["attention_mask"])' \
    'assert q8_vecs.shape == (5, 768)' \
    'q8_min_cos = 1.0' \
    'for idx in range(5):' \
    '    q8_norm = float(np.linalg.norm(q8_vecs[idx]))' \
    '    assert abs(q8_norm - 1.0) < 1e-5, f"QINT8 norm drift at {idx}: {q8_norm}"' \
    '    assert np.all(np.isfinite(q8_vecs[idx]))' \
    '    q8_cos = float(np.dot(pt_vecs[idx], q8_vecs[idx]))' \
    '    q8_min_cos = min(q8_min_cos, q8_cos)' \
    '    assert q8_cos >= 0.80, f"QINT8 cosine below 0.80 at {idx}: {q8_cos}"' \
    '' \
    'print(f"Verified ONNX FP32 & QINT8 exports: dim={dim}, max_seq_length={max_seq_len}, fp32_min_cos={fp32_min_cos:.6f}, q8_min_cos={q8_min_cos:.6f}")' \
    > /tmp/export_onnx.py && \
    python /tmp/export_onnx.py && \
    rm -rf /tmp/export_onnx.py /tmp/hf_cache /root/.cache && \
    find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + && \
    chmod -R a+rX /opt/nce/models


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    NCE_MODEL_DIR=/opt/nce/models \
    NCE_COGNITIVE_BACKEND=onnx \
    COGNITIVE_PORT=11435

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/nce/models /opt/nce/models
COPY deploy/cognitive-service/app.py deploy/cognitive-service/server.py /app/

RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid 10001 --shell /usr/sbin/nologin --create-home appuser && \
    chown -R appuser:appuser /app

USER 10001:10001

EXPOSE 11435

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=6 \
    CMD ["python", "-c", "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:11435/readyz'));assert d.get('status')=='ok' and d.get('engine') in ('onnxruntime','onnxruntime-int8','sentence-transformers'), d"]

CMD ["python", "/app/server.py"]
