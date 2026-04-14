#!/usr/bin/env python3
"""서버·네트워크·VMware 통합 질의 100건 — POST /api/chat, 도메인 메타 포함."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parent.parent


def _build_100_queries() -> list[dict]:
    """정확히 100건: server 36, network 36, vmware 20, mixed 8."""
    out: list[dict] = []
    n = 1

    def add(q: str, exp: list[str], domain: str, intent: str):
        nonlocal n
        out.append(
            {
                "id": f"i{n:03d}",
                "query": q,
                "expect_tools": exp,
                "domain": domain,
                "intent": intent,
            }
        )
        n += 1

    # ── Server 36 ──
    for per in ["오늘", "어제", "최근 3일"]:
        add(
            f"{per} Zabbix server CPU 사용률이 높았던 시기가 언제야",
            ["get_server_cpu", "search_historical_data", "get_server_status"],
            "server",
            "history",
        )
    for per in ["최근 7일", "이번 주", "지난 24시간"]:
        add(
            f"{per} 전체 서버 중 메모리 사용률이 높은 호스트는",
            ["get_server_memory", "get_server_status", "search_historical_data"],
            "server",
            "compare",
        )
    for a in ["디스크", "파일시스템", "스왑"]:
        add(
            f"Zabbix server {a} 사용량과 여유 공간 알려줘",
            ["get_server_filesystem", "get_server_memory", "get_server_status"],
            "server",
            "status",
        )
    add("전체 서버 프로세스 CPU 상위 10개", ["get_processes"], "server", "status")
    add("심각도 높은 서버 알림만 요약", ["get_alerts"], "server", "alert")
    add("최근 1일 서버 장애 이벤트 목록", ["get_alerts", "search_historical_data"], "server", "alert")
    add("Linux 서버 syslog error 패턴이 많았던 구간", ["search_historical_data", "get_server_logs"], "server", "log")
    add("서버 자산 전체 목록과 OS 유형", ["get_all_assets"], "server", "inventory")
    for host in ["Zabbix server", "all"]:
        add(
            f"{host} 서버 NIC 트래픽이 큰 인터페이스 순위",
            ["get_server_interfaces"],
            "server",
            "status",
        )
    add("서버 종합 상태에서 CPU·메모리·디스크 한 번에", ["get_server_status"], "server", "status")
    add("AIX 서버 errpt 최근 심각 이벤트", ["get_errpt"], "server", "log")
    add("DB 역할 호스트 메모리 부족 징후", ["get_server_memory", "get_alerts"], "server", "analysis")
    add("지난 2일 서버 CPU 변동이 큰 호스트", ["search_historical_data", "get_server_cpu"], "server", "history")
    add("Listen 포트가 많이 열린 서버", ["get_listen_ports"], "server", "status")
    add("활성 TCP 연결이 많은 서버", ["get_connections"], "server", "status")
    add("전체 서버 스왑 사용률 요약", ["get_server_memory", "get_server_status"], "server", "status")
    add("디스크 inode 부족 가능성 있는 서버", ["get_server_filesystem"], "server", "status")
    add("최근 서버 로그에서 WARN이 많은 호스트", ["get_server_logs", "search_historical_data"], "server", "log")
    add("CPU 코어별 부하 불균형 있는 서버", ["get_server_cpu"], "server", "analysis")
    add("서버 업타임이 짧은 호스트 (재기동 추정)", ["get_server_status", "get_all_assets"], "server", "inventory")
    # pad server to 36
    while len([x for x in out if x["domain"] == "server"]) < 36:
        k = len([x for x in out if x["domain"] == "server"]) + 1
        add(
            f"서버 모니터링 점검 {k}: 리소스 병목 요약",
            ["get_server_status", "get_server_cpu", "get_alerts"],
            "server",
            "analysis",
        )

    # ── Network 36 ──
    for d in ["int_sw_1", "VSX-Secondary"]:
        add(
            f"{d} 스위치 포트 Link Up 개수와 트래픽 요약",
            ["get_network_interfaces", "get_network_device"],
            "network",
            "status",
        )
    add("모든 네트워크 장비 CPU·메모리 비교", ["get_network_device"], "network", "compare")
    add("네트워크 장비 중 ping 불가 장비", ["get_network_device"], "network", "status")
    add("스위치 포트 에러 카운트 상위 10개", ["get_network_interfaces"], "network", "status")
    add("Gi0/1 트래픽 순위 실시간", ["get_network_interfaces"], "network", "status")
    add("네트워크 알림만 서버 알림 제외하고", ["get_alerts"], "network", "alert")
    add("최근 3일 네트워크 성능 이력 특이사항", ["search_historical_data"], "network", "history")
    add("VSX-Secondary 1/1/5 포트 트래픽 피크 시각", ["search_historical_data"], "network", "history")
    add("ICMP 응답시간 이상 스위치", ["get_network_device"], "network", "status")
    add("네트워크 인프라 위험 신호 3가지", ["get_network_device", "get_alerts"], "network", "analysis")
    add("포트 가동률 낮은 스위치", ["get_network_interfaces", "get_network_device"], "network", "analysis")
    add("전체 네트워크 장비 목록과 uptime", ["get_network_device"], "network", "inventory")
    for per in ["최근 7일", "어제"]:
        add(
            f"{per} int_sw_1에서 트래픽이 가장 높았던 포트는",
            ["search_historical_data", "get_network_interfaces"],
            "network",
            "history",
        )
    for sl in ["1/1/1", "1/1/24"]:
        add(f"지난 2일 {sl} 포트 대역폭 피크 시간", ["search_historical_data"], "network", "history")
    add("네트워크 청킹 기반 주간 요약", ["search_historical_data"], "network", "history")
    add("스위치 온도 이상 징후", ["get_network_device", "search_historical_data"], "network", "analysis")
    add("Link Down 포트 개수", ["get_network_interfaces"], "network", "status")
    add("SNMP 응답 지연 이력", ["search_historical_data", "get_network_device"], "network", "history")
    add("트래픽 Top 5 물리 포트", ["get_network_interfaces"], "network", "status")
    add("백본·엣지 스위치 부하 비교", ["get_network_device"], "network", "compare")
    add("복구 안 된 네트워크 알림", ["get_alerts"], "network", "alert")
    add("LAG 구성 포트 상태", ["get_network_interfaces"], "network", "status")
    for i in range(3):
        add(
            f"네트워크 점검 시나리오 {i+1}: 포트 트래픽 이상",
            ["get_network_interfaces", "search_historical_data", "get_network_device"],
            "network",
            "analysis",
        )
    while len([x for x in out if x["domain"] == "network"]) < 36:
        k = len([x for x in out if x["domain"] == "network"]) + 1
        add(
            f"네트워크 변형 {k}: 스위치 포트 이력",
            ["get_network_device", "search_historical_data"],
            "network",
            "history",
        )

    # ── VMware 20 ──
    vm_list = [
        ("VMware 하이퍼바이저 현재 CPU 사용률 요약", ["get_vmware"]),
        ("vCenter 등록 VM 대수와 전원 상태", ["get_vmware"]),
        ("ESXi 메모리 ballooning 이상", ["get_vmware"]),
        ("VM 중 CPU 급증 게스트", ["get_vmware"]),
        ("오래된 스냅샷 VM", ["get_vmware"]),
        ("데이터스토어 용량 경고", ["get_vmware"]),
        ("최근 VMware 이상징후 이벤트", ["get_vmware", "search_historical_data"]),
        ("물리장비와 VM 구분해서 VMware CPU", ["get_vmware"]),
        ("openbase_vcenter 클러스터 리소스", ["get_vmware"]),
        ("하이퍼바이저와 VM 목록 구분 표시", ["get_vmware"]),
    ]
    for q, exp in vm_list:
        add(q, exp, "vmware", "status" if "이상" not in q and "이벤트" not in q else "anomaly")
    for i in range(10):
        add(
            f"VMware 점검 {i+1}: 하이퍼바이저 대비 VM 부하",
            ["get_vmware"],
            "vmware",
            "analysis",
        )

    # ── Mixed 8 ──
    mixed_list = [
        ("서버와 네트워크 알림 같이 보고 우선순위", ["get_alerts"], "analysis"),
        ("인프라 전체 위험 신호 3가지", ["get_alerts", "get_server_status", "get_network_device"], "analysis"),
        ("VMware CPU와 물리 서버 CPU 혼동 없이 비교", ["get_vmware", "get_server_cpu"], "compare"),
        ("모니터링 자산 서버·네트워크·VMware 개요", ["get_all_assets", "get_vmware"], "inventory"),
        ("24시간 장애를 서버/네트워크/VMware로 나눠 요약", ["get_alerts"], "alert"),
        ("스위치와 VMware 역할 혼동 없이 설명", ["get_network_device", "get_vmware"], "analysis"),
        ("CPU 높은 서버와 트래픽 높은 스위치 포트 종합", ["get_server_cpu", "get_network_interfaces"], "analysis"),
        ("심각 알림 건수와 추세", ["get_alerts", "search_historical_data"], "alert"),
    ]
    for q, exp, intent in mixed_list:
        add(q, exp, "mixed", intent)

    assert len(out) == 100, len(out)
    return out


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


def run_one(base_url: str, q: dict, top_k: int, timeout: int, model_id: str | None) -> dict:
    t0 = time.time()
    body: dict = {"query": q["query"], "top_k": top_k}
    if model_id:
        body["model_id"] = model_id
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/api/chat",
            json=body,
            stream=True,
            timeout=timeout,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        r.raise_for_status()
    except Exception as e:
        return {
            "id": q["id"],
            "query": q["query"],
            "domain": q.get("domain"),
            "intent": q.get("intent"),
            "ok": False,
            "error": str(e),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    result_event, error_event = parse_sse(r.text)
    if error_event:
        return {
            "id": q["id"],
            "query": q["query"],
            "domain": q.get("domain"),
            "intent": q.get("intent"),
            "ok": False,
            "error": error_event,
            "elapsed_ms": elapsed_ms,
        }
    if not result_event:
        return {
            "id": q["id"],
            "query": q["query"],
            "domain": q.get("domain"),
            "intent": q.get("intent"),
            "ok": False,
            "error": "no_result_event",
            "elapsed_ms": elapsed_ms,
        }

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

    dom = q.get("domain") or ""
    intent_mismatch = None
    if dom == "vmware" and unique_tools and "get_vmware" not in unique_tools:
        if any(t in unique_tools for t in ("get_network_device", "get_network_interfaces")):
            intent_mismatch = "vmware_q_used_network_tools"
    if dom == "network" and unique_tools and "get_vmware" in unique_tools and "get_network_device" not in unique_tools:
        intent_mismatch = "network_q_used_vmware_only"

    return {
        "id": q["id"],
        "query": q["query"],
        "domain": q.get("domain"),
        "intent": q.get("intent"),
        "ok": hit and "no_tool_trace" not in flags,
        "expect_hit": hit,
        "query_type": data.get("query_type"),
        "tools": unique_tools,
        "expected_tools": sorted(exp),
        "quality_flags": flags,
        "intent_mismatch": intent_mismatch,
        "answer_preview": ((data.get("answer") or data.get("summary") or "")[:400]),
        "elapsed_ms": elapsed_ms,
    }


def analyze(results: list[dict]) -> dict:
    by_domain: dict[str, dict] = {}
    for r in results:
        d = r.get("domain") or "unknown"
        by_domain.setdefault(d, {"total": 0, "ok": 0, "expect_hit": 0, "no_tool": 0, "mismatch": 0})
        by_domain[d]["total"] += 1
        if r.get("ok"):
            by_domain[d]["ok"] += 1
        if r.get("expect_hit"):
            by_domain[d]["expect_hit"] += 1
        if "no_tool_trace" in (r.get("quality_flags") or []):
            by_domain[d]["no_tool"] += 1
        if r.get("intent_mismatch"):
            by_domain[d]["mismatch"] += 1

    tool_ctr: collections.Counter[str] = collections.Counter()
    for r in results:
        for t in r.get("tools") or []:
            tool_ctr[t] += 1

    qt_ctr: collections.Counter[str] = collections.Counter()
    for r in results:
        qt_ctr[str(r.get("query_type") or "")] += 1

    errors = [r for r in results if not r.get("ok")]
    err_samples = errors[:20]

    return {
        "totals": {
            "n": len(results),
            "ok_strict": sum(1 for x in results if x.get("ok")),
            "expect_hit_any": sum(1 for x in results if x.get("expect_hit")),
            "no_tool_trace": sum(1 for x in results if "no_tool_trace" in (x.get("quality_flags") or [])),
            "general_type": sum(1 for x in results if "general_type" in (x.get("quality_flags") or [])),
            "intent_mismatch_flagged": sum(1 for x in results if x.get("intent_mismatch")),
        },
        "by_domain": by_domain,
        "tool_usage_top": tool_ctr.most_common(25),
        "query_type_dist": dict(qt_ctr),
        "failure_samples": err_samples,
        "notes": [
            "ok_strict: 기대 도구 1개 이상 호출 + tool_trace 비어 있지 않음.",
            "expect_hit_any: 기대 도구 집합과 교집합 성공.",
            "의도: 도메인(server/network/vmware/mixed)별로 적절한 도구가 쓰였는지 expect_tools로 근사.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--model-id", default="", help="미지정 시 서버 기본 모델")
    ap.add_argument("-o", type=Path, default=_REPO / "artifacts" / "integrated_100_report.json")
    args = ap.parse_args()

    assert len(QUERIES_100) == 100

    results = []
    mid = args.model_id.strip() or None
    for q in QUERIES_100:
        try:
            results.append(run_one(args.base_url, q, args.top_k, args.timeout, mid))
        except Exception as e:
            results.append(
                {
                    "id": q["id"],
                    "query": q["query"],
                    "domain": q.get("domain"),
                    "intent": q.get("intent"),
                    "ok": False,
                    "error": str(e),
                }
            )
        if args.delay > 0:
            time.sleep(args.delay)

    analysis = analyze(results)
    summary = {
        "meta": {
            "base_url": args.base_url.rstrip("/"),
            "model_id": mid or "(server default)",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(results),
        },
        "analysis": analysis,
        "pass_strict": sum(1 for x in results if x.get("ok")),
        "fail": sum(1 for x in results if not x.get("ok")),
        "results": results,
    }
    args.o.parent.mkdir(parents=True, exist_ok=True)
    args.o.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(args.o), "analysis": analysis["totals"], "by_domain": analysis["by_domain"]}, ensure_ascii=False))
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
