from pathlib import Path

p = Path("/data/rag/docker-compose.yml")
text = p.read_text(encoding="utf-8")
old = """  rag-fastapi:
    image: rag-system-fastapi:latest
    container_name: rag-fastapi
    ports:
      - "8000:8000"
    volumes:
      - /app/rag/fastapi:/app
    depends_on:"""
new = """  rag-fastapi:
    image: rag-system-fastapi:latest
    container_name: rag-fastapi
    ports:
      - "8000:8000"
    volumes:
      - /app/rag/fastapi:/app
    environment:
      DATABASE_URL: postgresql://rag_user:rag_password@rag-pgvector:5432/rag_db
      OLLAMA_URL: http://rag-ollama:11434
    env_file:
      - /app/rag/fastapi/.env
    depends_on:"""
if old not in text:
    raise SystemExit("pattern not found in compose")
p.write_text(text.replace(old, new, 1), encoding="utf-8")
print("ok")
