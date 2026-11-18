# Air-gapped CPU cognitive service: sentence-transformers/all-mpnet-base-v2 (768-d) + spaCy en_core_web_sm
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf_cache

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install CPU-only PyTorch wheel first (avoids ~4 GB NVIDIA CUDA layers),
# then pin exact NCE cognitive dependencies matching requirements.lock.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.6.0,<3.0.0" && \
    pip install --no-cache-dir \
      "sentence-transformers==6.3.0" \
      "transformers==5.23.0" \
      "huggingface-hub==2.4.0" \
      "tokenizers==0.23.3" \
      "safetensors==0.8.0" \
      "spacy==3.8.18" \
      "numpy==2.5.5" \
      "scipy==1.18.1" \
      "scikit-learn==1.9.1" \
      "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"

# Bake HuggingFace model weights into /opt/hf_cache and assert 768-d unit-norm inference offline.
RUN mkdir -p /opt/hf_cache && \
    python -c "import math; from sentence_transformers import SentenceTransformer; m = SentenceTransformer('sentence-transformers/all-mpnet-base-v2', device='cpu'); v = m.encode('NCE cognitive build verification', normalize_embeddings=True); assert len(v) == 768 and abs(math.sqrt(sum(float(x)**2 for x in v)) - 1.0) < 1e-4; print('Cached all-mpnet-base-v2 dim=', len(v))" && \
    chmod -R a+rX /opt/hf_cache


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf_cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    COGNITIVE_PORT=11435

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf_cache /opt/hf_cache
COPY deploy/cognitive-service/server.py /app/server.py

RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid 10001 --shell /usr/sbin/nologin --create-home appuser && \
    chown -R appuser:appuser /app

USER 10001:10001

EXPOSE 11435

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
    CMD ["python", "-c", "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:11435/readyz'));assert d.get('status')=='ok' and d.get('engine')=='sentence-transformers', d"]

CMD ["python", "/app/server.py"]
