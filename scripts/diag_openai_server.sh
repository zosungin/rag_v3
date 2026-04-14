#!/bin/bash
echo "=== host .env key length (value not printed) ==="
python3 << 'PY'
from pathlib import Path
p = Path("/app/rag/fastapi/.env")
if not p.exists():
    print("no .env")
else:
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("OPENAI_API_KEY="):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            print("host_file_len", len(v))
            break
    else:
        print("no OPENAI line")
PY
echo "=== container /app/.env ==="
docker exec rag-fastapi sh -c 'test -f /app/.env && echo exists || echo missing'
echo "=== container env OPENAI length ==="
docker exec rag-fastapi python3 -c "import os; print(len(os.environ.get('OPENAI_API_KEY','')))"
echo "=== app.config after import (in container) ==="
docker exec rag-fastapi python3 -c "import sys; sys.path.insert(0,'/app'); from app import config; print('config_OPENAI_len', len(config.OPENAI_API_KEY or ''))"
