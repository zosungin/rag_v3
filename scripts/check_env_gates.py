#!/usr/bin/env python3
"""
배포/스테이징에서 Agentic RAG 질의응답 전 사전 조건(게이트) 확인.

  python scripts/check_env_gates.py
  python scripts/check_env_gates.py --base-url http://192.168.6.24:8000

환경 변수:
  TEST_API_BASE_URL — 기본 http://127.0.0.1:8000
  TEST_ENV — staging | production (출력 라벨만, 검증 로직 동일)

종료 코드: 0 전부 통과, 1 하나라도 실패
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 15.0) -> tuple[int, dict | None, str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode()
            try:
                return code, json.loads(body), body
            except json.JSONDecodeError:
                return code, None, body
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, None, raw
    except Exception as e:
        return -1, None, str(e)


def _status_ok(block: dict | None, key: str, allowed: frozenset[str]) -> tuple[bool, str]:
    if not isinstance(block, dict):
        return False, f"{key}: 응답 없음"
    st = (block.get("status") or "").lower()
    if st in allowed:
        return True, f"{key}: {st} ({block.get('label', '')})"
    return False, f"{key}: status={st!r} (허용: {allowed}) — {block.get('detail', '')}"


def main() -> int:
    p = argparse.ArgumentParser(description="RAG/Agent 환경 게이트 점검")
    p.add_argument(
        "--base-url",
        default=os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        help="API 베이스 URL",
    )
    p.add_argument("--timeout", type=float, default=15.0)
    args = p.parse_args()

    test_env = os.environ.get("TEST_ENV", "staging").strip() or "staging"
    url = f"{args.base_url}/api/health/detail"
    print(f"[check_env_gates] TEST_ENV={test_env} url={url}")

    code, data, raw = _get(url, timeout=args.timeout)
    if code != 200 or not isinstance(data, dict):
        print(f"FAIL: HTTP {code} 또는 JSON 파싱 실패: {raw[:500]}")
        return 1

    checks = []

    ok_rag, msg_rag = _status_ok(data.get("rag_engine"), "rag_engine", frozenset({"healthy"}))
    checks.append((ok_rag, msg_rag))

    ok_vec, msg_vec = _status_ok(
        data.get("vector_db"), "vector_db", frozenset({"healthy", "optimized"})
    )
    checks.append((ok_vec, msg_vec))

    ok_mesh, msg_mesh = _status_ok(
        data.get("agent_mesh"), "agent_mesh", frozenset({"active", "healthy"})
    )
    checks.append((ok_mesh, msg_mesh))

    for ok, msg in checks:
        print(("OK  " if ok else "FAIL") + " " + msg)

    if not all(c[0] for c in checks):
        print(
            "\n힌트: vector_db 실패 → DATABASE_URL/RAG DB 및 rag-pgvector 연결 확인.\n"
            "      agent_mesh 실패 → OPENAI_API_KEY 또는 Ollama 경로·모델 확인.\n"
            "      동기화 신선도는 /api/chat 세션 컨텍스트의 data freshness 블록·sync 로그를 확인."
        )
        return 1

    print("모든 게이트 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
