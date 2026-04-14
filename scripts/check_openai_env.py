#!/usr/bin/env python3
"""서버에서 OPENAI 키 길이만 출력 (값 노출 없음)."""
from pathlib import Path

p = Path("/app/rag/fastapi/.env")
if not p.exists():
    print("no_env_file")
    raise SystemExit(0)
for line in p.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("OPENAI_API_KEY="):
        v = line.split("=", 1)[1].strip().strip('"').strip("'")
        print("env_file_key_len", len(v))
        break
else:
    print("no_OPENAI_line")
