#!/usr/bin/env python3
"""
API 방식 질의·응답 스모크: 게이트 → (선택) 검색 → 채팅(SSE) 순서.

  # 저장소 루트에서 (PYTHONPATH=. 권장)
  python scripts/api_smoke_chat.py
  python scripts/api_smoke_chat.py --base-url http://192.168.6.24:8000
  python scripts/api_smoke_chat.py --with-search
  python scripts/api_smoke_chat.py --questions scripts/api_smoke_questions.json

환경 변수:
  TEST_API_BASE_URL — 기본 http://127.0.0.1:8000 (스테이징/운영 중 하나를 수동으로 고정)
  API_SMOKE_CHAT_DELAY_SEC — 채팅 요청 간 대기(초), 기본 2

종료 코드: 0 성공, 1 게이트/HTTP/파싱 실패
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

try:
    import requests
except ImportError:
    print("pip install requests 필요", file=sys.stderr)
    sys.exit(1)


def _base_url() -> str:
    return os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _gates_ok(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    re_ok = (data.get("rag_engine") or {}).get("status") == "healthy"
    vec = (data.get("vector_db") or {}).get("status")
    vec_ok = vec in ("healthy", "optimized")
    mesh = (data.get("agent_mesh") or {}).get("status")
    mesh_ok = mesh in ("active", "healthy")
    return bool(re_ok and vec_ok and mesh_ok)


def _load_questions(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _import_sse_parser():
    sys.path.insert(0, str(_REPO))
    from tests.agent_e2e_utils import parse_chat_sse_response, tool_names_from_result_event

    return parse_chat_sse_response, tool_names_from_result_event


def main() -> int:
    ap = argparse.ArgumentParser(description="API 게이트·검색·채팅 스모크")
    ap.add_argument("--base-url", default=None, help="TEST_API_BASE_URL 보다 우선")
    ap.add_argument(
        "--questions",
        type=Path,
        default=_REPO / "scripts" / "api_smoke_questions.json",
        help="search_queries / chat_queries JSON",
    )
    ap.add_argument(
        "--with-search",
        action="store_true",
        help="POST /api/search 로 search_queries 실행",
    )
    ap.add_argument(
        "--run-gate-script",
        action="store_true",
        help="scripts/check_env_gates.py 를 서브프로세스로도 실행(중복 검증)",
    )
    ap.add_argument(
        "--run-search-suite",
        action="store_true",
        help="sync/test_search_quality.py 전체 배치 실행(시간 소요)",
    )
    ap.add_argument(
        "--soft-tools",
        action="store_true",
        help="chat_queries 의 allowed_tools 와 tool_trace 불일치여도 경고만 하고 성공 처리",
    )
    args = ap.parse_args()

    base = (args.base_url or _base_url()).rstrip("/")
    delay = float(os.environ.get("API_SMOKE_CHAT_DELAY_SEC", "2"))

    print(f"[api_smoke_chat] TEST_API_BASE_URL={base}")
    print(f"[api_smoke_chat] 질문 파일: {args.questions}")

    # --- 1) Health (게이트) ---
    try:
        hr = requests.get(f"{base}/api/health/detail", timeout=20)
        hr.raise_for_status()
        health = hr.json()
    except Exception as e:
        print(f"FAIL: health 요청 실패: {e}", file=sys.stderr)
        return 1

    if not _gates_ok(health):
        print(f"FAIL: health 상세 불충분: {json.dumps(health, ensure_ascii=False)}", file=sys.stderr)
        return 1
    print("OK   게이트(/api/health/detail): rag_engine / vector_db / agent_mesh")

    if args.run_gate_script:
        check_script = _REPO / "scripts" / "check_env_gates.py"
        rc = subprocess.call(
            [sys.executable, str(check_script), "--base-url", base],
            cwd=str(_REPO),
        )
        if rc != 0:
            print("FAIL: check_env_gates.py", file=sys.stderr)
            return 1
        print("OK   check_env_gates.py")

    # --- 2) 선택: 검색 품질 전체 배치 ---
    if args.run_search_suite:
        env = os.environ.copy()
        env["TEST_API_BASE_URL"] = base
        rc = subprocess.call(
            [sys.executable, str(_REPO / "sync" / "test_search_quality.py"), "--delay", "0"],
            cwd=str(_REPO),
            env=env,
        )
        if rc != 0:
            print("WARN: test_search_quality.py 비정상 종료", file=sys.stderr)

    data = _load_questions(args.questions)
    parse_chat_sse_response, tool_names_from_result_event = _import_sse_parser()

    # --- 3) 선택: 단건 검색 스모크 ---
    if args.with_search:
        for i, sq in enumerate(data.get("search_queries") or [], 1):
            payload = {"query": sq["query"], "top_k": int(sq.get("top_k", 5))}
            if sq.get("categories"):
                payload["categories"] = sq["categories"]
            try:
                r = requests.post(f"{base}/api/search", json=payload, timeout=120)
                r.raise_for_status()
                out = r.json()
                n = out.get("total_count", out.get("total", 0))
                print(f"OK   search [{i}] total_count={n} q={sq['query'][:60]!r}")
            except Exception as e:
                print(f"FAIL: search [{i}]: {e}", file=sys.stderr)
                return 1

    # --- 4) 채팅 SSE ---
    timeout = float(os.environ.get("API_SMOKE_CHAT_TIMEOUT_SEC", "180"))
    chats = data.get("chat_queries") or []
    for i, cq in enumerate(chats, 1):
        q = cq.get("query", "")
        cid = cq.get("id", str(i))
        body = {"query": q, "top_k": 5}
        if cq.get("model_id"):
            body["model_id"] = cq["model_id"]
        print(f"--- chat [{cid}] ---")
        try:
            r = requests.post(f"{base}/api/chat", json=body, stream=True, timeout=timeout)
            r.raise_for_status()
            text = r.text
        except Exception as e:
            print(f"FAIL: chat HTTP: {e}", file=sys.stderr)
            return 1

        parsed = parse_chat_sse_response(text)
        if parsed.get("error_event"):
            print(f"FAIL: SSE error: {parsed['error_event']}", file=sys.stderr)
            return 1
        ev = parsed.get("result_event")
        if not ev:
            print("FAIL: type=result 이벤트 없음", file=sys.stderr)
            return 1

        names = tool_names_from_result_event(ev)
        result_data = ev.get("data") or {}
        ans_preview = (
            (result_data.get("answer") or result_data.get("summary") or "")[:200]
        )
        print(f"OK   tools={names}")
        print(f"     answer_preview: {ans_preview!r}")

        allowed = set(cq.get("allowed_tools") or [])
        if allowed:
            used = set(names)
            if not (used & allowed):
                msg = f"allowed_tools 교집합 없음: allowed={allowed} used={used}"
                if args.soft_tools:
                    print(f"WARN {msg}")
                else:
                    print(f"FAIL {msg}", file=sys.stderr)
                    return 1

        if i < len(chats) and delay > 0:
            time.sleep(delay)

    print("[api_smoke_chat] 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
