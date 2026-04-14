# Zabbix Agentic RAG API — 표준 배포 경로: /app/rag/fastapi
FROM python:3.11-slim

WORKDIR /app/rag/fastapi

ENV PYTHONPATH=/app/rag/fastapi
ENV APP_ROOT=/app/rag/fastapi

RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app /app/rag/fastapi/app
COPY ./templates /app/rag/fastapi/templates
COPY ./static /app/rag/fastapi/static
COPY ./sync /app/rag/fastapi/sync
COPY ./gen_chunking.py /app/rag/fastapi/
COPY ./env.example /app/rag/fastapi/

EXPOSE 8000

# 운영: --reload 제거. 개발 시 compose에서 덮어쓰기.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
