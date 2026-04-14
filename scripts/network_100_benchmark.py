#!/usr/bin/env python3
"""네트워크 질의 100건 배치 (network_20과 별도) — POST /api/chat 결과 JSON."""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parent.parent


def _build_100_queries() -> list[dict]:
    out: list[dict] = []
    n = 1

    def add(q: str, exp: list[str]):
        nonlocal n
        out.append({"id": f"h{n:03d}", "query": q, "expect_tools": exp})
        n += 1

    devices = ["int_sw_1", "VSX-Secondary"]
    slots = ["1/1/5", "1/1/1", "1/1/10", "1/1/24", "1/1/36"]
    periods = ["최근 3일", "최근 7일", "이번 주", "어제", "오늘"]

    for per, dev, sl in itertools.product(periods, devices, slots):
        if n > 30:
            break
        add(
            f"{per} {dev} {sl} 포트에서 트래픽이 가장 높았던 시각은?",
            ["search_historical_data"],
        )

    gens = [
        ("네트워크 장비 전체 요약", ["get_network_device"]),
        ("모든 스위치 장비의 CPU와 메모리 사용률 비교", ["get_network_device"]),
        ("ICMP ping 실패하는 네트워크 장비 있나", ["get_network_device"]),
        ("네트워크 장비 포트 중 에러 카운트 TOP 10", ["get_network_interfaces"]),
        ("Link Down 포트 개수 세어줘", ["get_network_interfaces"]),
        ("수신 트래픽 기준 포트 순위 5개", ["get_network_interfaces"]),
        ("Gi0/1 인터페이스 트래픽 순위", ["get_network_interfaces"]),
        ("GigabitEthernet0/1 대역폭 사용량 큰 순", ["get_network_interfaces"]),
        ("네트워크 알림만 보여줘 서버 알림은 빼고", ["get_alerts"]),
        ("장비 알림 중 네트워크 장비 관련만 필터", ["get_alerts"]),
        ("스위치 SNMP 응답시간 이상 있는지", ["get_network_device", "get_network_interfaces"]),
        ("네트워크 인프라 위험 신호 3가지", ["get_network_device", "get_alerts"]),
        ("네트워크 성능 이력 특이사항", ["search_historical_data"]),
        ("포트 가동률 낮은 스위치", ["get_network_interfaces", "get_network_device"]),
        ("1/1/5 사용률 높았던 시간", ["search_historical_data"]),
        ("VSX-Secondary 1/1/5 피크 시각", ["search_historical_data"]),
        ("네트워크 청킹 데이터 기반 주간 요약", ["search_historical_data"]),
        ("int_sw_1 인터페이스 송수신", ["get_network_interfaces"]),
        ("test_network_devices 상태", ["get_network_device"]),
        ("전체 네트워크 장비 목록과 uptime", ["get_network_device"]),
    ]
    for q, exp in gens:
        add(q, exp)

    while n <= 100:
        add(
            f"네트워크 장비 트래픽 점검 변형 {n}: 최근 이력 중 포트 이상 징후",
            ["search_historical_data", "get_network_device"],
        )

    return out[:100]


QUERIES_100 = _build_100_queries()


def parse_sse(text: str):
    result_event = None
    error_event = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[6:])
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
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={"query": q["query"], "top_k": top_k},
            stream=True,
            timeout=timeout,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        r.raise_for_status()
    except Exception as e:
        return {
            "id": q["id"],
            "query": q["query"],
            "ok": False,
            "error": str(e),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    result_event, error_event = parse_sse(r.text)
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
    flags = []
    if not unique_tools:
        flags.append("no_tool_trace")
    if data.get("query_type") == "general":
        flags.append("general_type")

    return {
        "id": q["id"],
        "query": q["query"],
        "ok": hit and "no_tool_trace" not in flags,
        "expect_hit": hit,
        "query_type": data.get("query_type"),
        "tools": unique_tools,
        "expected_tools": sorted(exp),
        "quality_flags": flags,
        "answer_preview": ((data.get("answer") or data.get("summary") or "")[:320]),
        "elapsed_ms": elapsed_ms,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("-o", type=Path, default=_REPO / "artifacts" / "network_100_report.json")
    args = ap.parse_args()

    results = []
    for q in QUERIES_100:
        try:
            results.append(run_one(args.base_url, q, args.top_k, args.timeout))
        except Exception as e:
            results.append({"id": q["id"], "query": q["query"], "ok": False, "error": str(e)})
        if args.delay > 0:
            time.sleep(args.delay)

    summary = {
        "meta": {
            "base_url": args.base_url.rstrip("/"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(results),
        },
        "pass_expect": sum(1 for x in results if x.get("ok")),
        "fail": sum(1 for x in results if not x.get("ok")),
        "no_tool_trace": sum(1 for x in results if "no_tool_trace" in (x.get("quality_flags") or [])),
        "results": results,
    }
    args.o.parent.mkdir(parents=True, exist_ok=True)
    args.o.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(args.o), **{k: summary[k] for k in ("pass_expect", "fail", "no_tool_trace")}}, ensure_ascii=False))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
