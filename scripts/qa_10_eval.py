#!/usr/bin/env python3
"""
운영/스테이징 대상 10개 고정 질의로 POST /api/chat 품질을 점검합니다.

출력:
  - JSON: 도구 교차·품질 플래그·지연(ms) — CI/아티팩트용
  - Markdown: 운영 정기 점검용 요약 (동일 실행에서 선택 생성)

환경 (CLI가 우선):
  TEST_API_BASE_URL 또는 QA10_BASE_URL — 베이스 URL
  QA10_DELAY_SEC — 질의 간 대기(초), 기본 0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

_REPO = Path(__file__).resolve().parent.parent

_DEFAULT_QUERIES: list[dict] = [
    {"id": "q1", "query": "현재 정상 상태인 서버를 요약해줘", "expect_tools": ["get_server_status", "get_all_assets"]},
    {"id": "q2", "query": "CPU 사용률이 높은 서버 TOP 3 알려줘", "expect_tools": ["get_server_cpu", "get_server_status"]},
    {"id": "q3", "query": "메모리 사용률이 위험한 서버가 있나?", "expect_tools": ["get_server_memory", "get_server_status"]},
    {"id": "q4", "query": "디스크 여유 공간이 부족한 서버와 파일시스템을 알려줘", "expect_tools": ["get_server_filesystem"]},
    {"id": "q5", "query": "최근 활성 장애 알림을 심각도 순으로 요약해줘", "expect_tools": ["get_alerts", "correlate_events"]},
    {"id": "q6", "query": "네트워크 장비 중 트래픽이 높은 포트 TOP 5", "expect_tools": ["get_network_interfaces", "get_network_device"]},
    {"id": "q7", "query": "VSX-Secondary 1/1/5 포트 트래픽이 최근 3일 중 가장 높았던 시각은?", "expect_tools": ["search_historical_data", "get_network_interfaces"]},
    {"id": "q8", "query": "Zabbix server에서 최근 error 로그 원인과 대응을 분석해줘", "expect_tools": ["get_server_logs", "search_historical_data"]},
    {"id": "q9", "query": "지난 24시간 CPU 추세와 7일 평균 대비 이상 여부를 알려줘", "expect_tools": ["search_historical_data", "get_server_cpu"]},
    {"id": "q10", "query": "서버/네트워크/알림을 종합해서 지금 특이사항 3가지를 말해줘", "expect_tools": ["get_alerts", "get_server_status", "get_network_device", "correlate_events"]},
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


def evaluate(base_url: str, one: dict, top_k: int, timeout_sec: int) -> dict:
    t0 = time.time()
    r = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={"query": one["query"], "top_k": top_k},
        stream=True,
        timeout=timeout_sec,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    r.raise_for_status()
    text = r.text
    result_event, error_event = parse_sse(text)

    if error_event:
        return {"id": one["id"], "query": one["query"], "ok": False, "error": error_event, "elapsed_ms": elapsed_ms}
    if not result_event:
        return {"id": one["id"], "query": one["query"], "ok": False, "error": "no_result_event", "elapsed_ms": elapsed_ms}

    data = result_event.get("data") or {}
    sources = result_event.get("sources") or {}
    trace = sources.get("tool_trace") or []
    tools = [x.get("tool") for x in trace if isinstance(x, dict) and x.get("tool")]
    unique_tools = sorted(set(tools))
    expected = set(one["expect_tools"])
    hit = bool(expected.intersection(set(unique_tools)))

    answer = data.get("answer") or data.get("summary") or ""
    quality_flags: list[str] = []
    if not unique_tools:
        quality_flags.append("no_tool_trace")
    if data.get("query_type") == "general":
        quality_flags.append("general_type")
    if len(answer.strip()) < 40:
        quality_flags.append("too_short")
    if "시각" in one["query"] and ("시각" not in answer and "시간" not in answer):
        quality_flags.append("time_question_no_time_answer")

    return {
        "id": one["id"],
        "query": one["query"],
        "ok": hit and "no_tool_trace" not in quality_flags,
        "query_type": data.get("query_type"),
        "title": data.get("title"),
        "answer_preview": answer[:260],
        "tools": unique_tools,
        "expected_tools": sorted(expected),
        "quality_flags": quality_flags,
        "elapsed_ms": elapsed_ms,
    }


def build_operational_markdown(summary: dict) -> str:
    """summary: JSON 루트와 동일 구조 (meta + total/pass/fail + results)."""
    meta = summary.get("meta") or {}
    lines: list[str] = []
    lines.append("# 운영 채팅 품질 점검 (10 질의)")
    lines.append("")
    lines.append(f"- 생성 시각(UTC): `{meta.get('generated_at_utc', '')}`")
    lines.append(f"- 대상 API: `{meta.get('base_url', '')}`")
    lines.append(f"- 결과: **통과 {summary.get('pass', 0)}** / 실패 {summary.get('fail', 0)} / 전체 {summary.get('total', 0)}")
    lines.append("")
    lines.append("## 요약 테이블")
    lines.append("")
    lines.append("| ID | 통과 | 도구(실행) | 기대 교차 | 품질 플래그 | 지연(ms) |")
    lines.append("|:--:|:----:|------------|-----------|-------------|---------:|")
    for row in summary.get("results") or []:
        rid = row.get("id", "")
        ok = "예" if row.get("ok") else "아니오"
        tools = ", ".join(row.get("tools") or []) if isinstance(row.get("tools"), list) else ""
        exp = ", ".join(row.get("expected_tools") or []) if isinstance(row.get("expected_tools"), list) else ""
        flags = ", ".join(row.get("quality_flags") or []) if row.get("quality_flags") else "—"
        if row.get("error"):
            flags = f"error: {row.get('error') if not isinstance(row.get('error'), dict) else 'see_json'}"
        ms = row.get("elapsed_ms", "")
        lines.append(f"| {rid} | {ok} | {tools[:80]} | {exp[:60]} | {flags[:120]} | {ms} |")
    lines.append("")
    lines.append("## 실패·경고 케이스 (트리아지)")
    lines.append("")
    bad_fail = [r for r in (summary.get("results") or []) if not r.get("ok")]
    bad_flags = [r for r in (summary.get("results") or []) if r.get("ok") and r.get("quality_flags")]
    if not bad_fail and not bad_flags:
        lines.append("_이번 실행에서 실패 및 품질 플래그 없음._")
    else:
        for r in bad_fail + bad_flags:
            lines.append(f"- **{r.get('id')}**: {r.get('query', '')[:100]}")
            if not r.get("ok"):
                err = r.get("error")
                err_s = json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(err)
                lines.append(f"  - 실패 원인: `{err_s[:300]}`")
            if r.get("quality_flags"):
                lines.append(f"  - 플래그: {', '.join(r['quality_flags'])}")
            lines.append("")
    lines.append("## 답변 미리보기 (디버깅)")
    lines.append("")
    for r in summary.get("results") or []:
        lines.append(f"### {r.get('id')}")
        lines.append("")
        prev = (r.get("answer_preview") or "").strip() or "(없음)"
        lines.append(f"> {prev.replace(chr(10), ' ')}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*이 파일은 `python scripts/qa_10_eval.py` 로 생성됩니다.*")
    return "\n".join(lines)


def _default_base_url() -> str:
    for k in ("TEST_API_BASE_URL", "QA10_BASE_URL"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v.rstrip("/")
    return "http://127.0.0.1:8000"


def main() -> int:
    ap = argparse.ArgumentParser(description="10질의 채팅 품질 점검 (POST /api/chat)")
    ap.add_argument("--base-url", default=None, help="API 베이스 URL (기본: TEST_API_BASE_URL / QA10_BASE_URL / 127.0.0.1:8000)")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=240, help="질의당 타임아웃(초)")
    ap.add_argument(
        "--delay",
        type=float,
        default=None,
        help="질의 간 대기(초). 기본: 환경 QA10_DELAY_SEC 또는 0",
    )
    ap.add_argument("--json-out", type=Path, default=_REPO / "artifacts" / "qa10_chat_report.json")
    ap.add_argument("--md-out", type=Path, default=_REPO / "artifacts" / "qa10_operational_report.md")
    ap.add_argument("--no-md", action="store_true", help="Markdown 리포트 생성 생략")
    args = ap.parse_args()

    base_url = (args.base_url or _default_base_url()).rstrip("/")
    delay = args.delay
    if delay is None:
        try:
            delay = float(os.environ.get("QA10_DELAY_SEC", "0") or "0")
        except ValueError:
            delay = 0.0

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "script": "scripts/qa_10_eval.py",
    }

    out: list[dict] = []
    for q in _DEFAULT_QUERIES:
        try:
            out.append(evaluate(base_url, q, args.top_k, args.timeout))
        except Exception as e:
            out.append({"id": q["id"], "query": q["query"], "ok": False, "error": str(e)})
        if delay > 0:
            time.sleep(delay)

    summary = {
        "meta": meta,
        "total": len(out),
        "pass": sum(1 for x in out if x.get("ok")),
        "fail": sum(1 for x in out if not x.get("ok")),
        "results": out,
    }

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_md:
        md = build_operational_markdown(summary)
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(md, encoding="utf-8")

    print(
        json.dumps(
            {
                "total": summary["total"],
                "pass": summary["pass"],
                "fail": summary["fail"],
                "json_out": str(args.json_out),
                "md_out": None if args.no_md else str(args.md_out),
            },
            ensure_ascii=False,
        )
    )
    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
