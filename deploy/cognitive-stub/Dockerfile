FROM python:3.12-slim
WORKDIR /app
COPY stub_server.py .
ENV COGNITIVE_PORT=11435
ENV EMBEDDING_VECTOR_DIM=768
EXPOSE 11435
CMD ["python", "stub_server.py"]
