FROM python:3.12-slim
WORKDIR /app

RUN groupadd -g 10001 appuser && useradd -u 10001 -g appuser -s /sbin/nologin -M appuser

COPY stub_server.py .
RUN chown -R appuser:appuser /app

USER 10001:10001

ENV COGNITIVE_PORT=11435
ENV EMBEDDING_VECTOR_DIM=768
EXPOSE 11435

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('COGNITIVE_PORT', '11435'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2)" || exit 1

CMD ["python", "stub_server.py"]
