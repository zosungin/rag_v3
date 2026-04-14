#!/usr/bin/env python3
"""네트워크 도메인 질의 20건 배치 — POST /api/chat 결과 수집 (분석용 JSON)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parent.parent

# 기대 도구: 교차 1개 이상이면 ok 후보 (qa_10_eval과 동일 허용 방식)
NETWORK_QUERIES: list[dict] = [
    {"id": "n01", "query": "네트워크 장비 전체 현황을 요약해줘", "expect_tools": ["get_network_device"]},
    {"id": "n02", "query": "스위치·라우터 장비 목록과 CPU 사용률을 알려줘", "expect_tools": ["get_network_device"]},
    {"id": "n03", "query": "네트워크 장비 중 ping이 안 되는 장비가 있나?", "expect_tools": ["get_network_device"]},
    {"id": "n04", "query": "네트워크 장비에서 트래픽이 높은 포트 TOP 5", "expect_tools": ["get_network_interfaces", "get_network_device"]},
    {"id": "n05", "query": "int_sw_1 장비의 인터페이스별 송수신 트래픽 현황", "expect_tools": ["get_network_interfaces"]},
    {"id": "n06", "query": "최근 7일 네트워크 장비 관련 장애 알림만 요약해줘", "expect_tools": ["get_alerts", "search_historical_data"]},
    {"id": "n07", "query": "VSX-Secondary 장비 1/1/5 포트 트래픽이 최근 3일 중 가장 높았던 시각은?", "expect_tools": ["search_historical_data"]},
    {"id": "n08", "query": "네트워크 포트 중 링크가 다운된 포트가 몇 개나 되나?", "expect_tools": ["get_network_interfaces"]},
    {"id": "n09", "query": "네트워크 장비 포트 에러 카운트가 높은 순으로 TOP 5", "expect_tools": ["get_network_interfaces"]},
    {"id": "n10", "query": "test_network_devices 장비 상태와 포트 요약", "expect_tools": ["get_network_device", "get_network_interfaces"]},
    {"id": "n11", "query": "지난 24시간 네트워크 인터페이스 트래픽 추이 이력을 알려줘", "expect_tools": ["search_historical_data"]},
    {"id": "n12", "query": "네트워크 장비 메모리 사용률이 80% 넘는 곳이 있나?", "expect_tools": ["get_network_device"]},
    {"id": "n13", "query": "스위치 uptime과 가동일수 상위 3대", "expect_tools": ["get_network_device"]},
    {"id": "n14", "query": "네트워크 장비 SNMP 응답이 느린 곳이 있나?", "expect_tools": ["get_network_device"]},
    {"id": "n15", "query": "1/1/5 포트 사용률이 높았던 시간은 언제야?", "expect_tools": ["search_historical_data", "get_network_interfaces"]},
    {"id": "n16", "query": "네트워크 성능 청킹 데이터 기준 이번 주 특이사항", "expect_tools": ["search_historical_data"]},
    {"id": "n17", "query": "전체 네트워크 장비 중 포트 가동률(Link Up 비율)이 낮은 장비", "expect_tools": ["get_network_interfaces", "get_network_device"]},
    {"id": "n18", "query": "네트워크 장비와 무관한 서버 알림은 제외하고 네트워크 쪽 알림만", "expect_tools": ["get_alerts"]},
    {"id": "n19", "query": "Gi0/1 또는 GigabitEthernet 트래픽이 큰 인터페이스 순위", "expect_tools": ["get_network_interfaces"]},
    {"id": "n20", "query": "네트워크 인프라 종합해서 지금 확인할 만한 위험 신호 3가지", "expect_tools": ["get_network_device", "get_alerts", "search_historical_data"]},
]


def parse_sse(text: str) -> tuple[dict | None, dict | None]:
    result_event = None
    error_event = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "result":
            result_event = obj
        elif t == "error":
            error_event = obj
    return result_event, error_event


def run_one(base_url: str, q: dict, top_k: int, timeout: int) -> dict:
    t0 = time.time()
    r = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={"query": q["query"], "top_k": top_k},
        stream=True,
        timeout=timeout,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    try:
        r.raise_for_status()
    except Exception as e:
        return {
            "id": q["id"],
            "query": q["query"],
            "ok": False,
            "error": str(e),
            "http_status": getattr(r, "status_code", None),
            "elapsed_ms": elapsed_ms,
        }
    text = r.text
    result_event, error_event = parse_sse(text)
    if error_event:
        return {"id": q["id"], "query": q["query"], "ok": False, "error": error_event, "elapsed_ms": elapsed_ms}
    if not result_event:
        return {"id": q["id"], "query": q["query"], "ok": False, "error": "no_result_event", "elapsed_ms": elapsed_ms}

    data = result_event.get("data") or {}
    sources = result_event.get("sources") or {}
    trace = sources.get("tool_trace") or []
    tools = [x.get("tool") for x in trace if isinstance(x, dict) and x.get("tool")]
    unique_tools = sorted(set(tools))
    exp = set(q.get("expect_tools") or [])
    hit = bool(exp.intersection(set(unique_tools))) if exp else bool(unique_tools)

    ans = (data.get("answer") or data.get("summary") or "")[:400]
    flags: list[str] = []
    if not unique_tools:
        flags.append("no_tool_trace")
    if data.get("query_type") == "general":
        flags.append("general_type")
    ql = q["query"].lower()
    if any(k in ql for k in ("시각", "언제", "몇 시", "높았던 시간", "피크")):
        if "시각" not in ans and "시간" not in ans and ":" not in ans[:200]:
            flags.append("time_like_q_weak_time_in_answer")

    return {
        "id": q["id"],
        "query": q["query"],
        "ok": hit and "no_tool_trace" not in flags,
        "expect_hit": hit,
        "query_type": data.get("query_type"),
        "tools": unique_tools,
        "expected_tools": sorted(exp),
        "quality_flags": flags,
        "answer_preview": ans,
        "elapsed_ms": elapsed_ms,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("-o", type=Path, default=_REPO / "artifacts" / "network_20_report.json")
    args = ap.parse_args()

    out: list[dict] = []
    for q in NETWORK_QUERIES:
        try:
            out.append(run_one(args.base_url, q, args.top_k, args.timeout))
        except Exception as e:
            out.append({"id": q["id"], "query": q["query"], "ok": False, "error": str(e)})
        if args.delay > 0:
            time.sleep(args.delay)

    summary = {
        "meta": {
            "base_url": args.base_url.rstrip("/"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "total": len(out),
        "pass_expect": sum(1 for x in out if x.get("ok")),
        "fail": sum(1 for x in out if not x.get("ok")),
        "results": out,
    }
    args.o.parent.mkdir(parents=True, exist_ok=True)
    args.o.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(args.o), "pass": summary["pass_expect"], "fail": summary["fail"], "total": summary["total"]}, ensure_ascii=False))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
