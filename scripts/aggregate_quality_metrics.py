#!/usr/bin/env python3
"""
테스트 산출물을 품질 차원별로 요약해 트리아지 백로그 초안을 만듭니다.

입력 (선택):
  artifacts/search_quality_report.json  — sync/test_search_quality.py 결과
  artifacts/qa10_chat_report.json       — scripts/qa_10_eval.py 결과 (있으면)
  artifacts/pytest_e2e.json             — 수동: pytest --json-report (있으면)

출력:
  artifacts/quality_triage_summary.md

환경:
  REPO_ROOT 는 스크립트 상위 디렉터리 기준.

로그 기반(parse 경고)은 서버 로그에서 아래 패턴으로 주간 grep 권장:
  grep -E \"\\[Chat/parse\\]|query_type=list이지만 details|log 타입인데 logs 없음\" ...
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_SEARCH = _REPO / "artifacts" / "search_quality_report.json"
_DEFAULT_QA10 = _REPO / "artifacts" / "qa10_chat_report.json"
_DEFAULT_E2E = _REPO / "artifacts" / "pytest_e2e.json"
_OUT = _REPO / "artifacts" / "quality_triage_summary.md"


def _dim_for_search_reason(reason: str) -> str:
    r = (reason or "").lower()
    if "total_count" in r or "min_total" in r:
        return "근거·검색_RAG_결과수"
    if "category" in r:
        return "근거·검색_RAG_카테고리"
    if "table_name" in r or "summary" in r:
        return "근거·검색_RAG_키워드테이블"
    if "api" in r or "http" in r or "timeout" in r:
        return "가용성_API"
    return "기타"


def _load_json(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _qa10_section(qa10: dict | None) -> list[str]:
    lines: list[str] = []
    lines.append("## 1.5 Chat QA10 (10 질의, POST /api/chat)")
    lines.append("")
    if not qa10:
        lines.append(
            "_입력 없음._ `python scripts/qa_10_eval.py --base-url http://<호스트>:8000` 실행 후 "
            "`artifacts/qa10_chat_report.json` 생성."
        )
        lines.append("")
        return lines
    meta = qa10.get("meta") or {}
    lines.append(f"- 통과: **{qa10.get('pass', 0)}** / {qa10.get('total', 0)} (실패 {qa10.get('fail', 0)})")
    lines.append(f"- API: `{meta.get('base_url', '')}`")
    lines.append(f"- 상세 Markdown: `artifacts/qa10_operational_report.md` (동일 실행에서 생성)")
    lines.append("")
    for row in qa10.get("results") or []:
        if row.get("ok"):
            continue
        q = row.get("query", "")
        err = row.get("error")
        reason = err if not isinstance(err, dict) else str(err.get("message", err))[:200]
        lines.append(f"- [ ] `{row.get('id')}` — {q[:90]} — {reason}")
    rows = qa10.get("results") or []
    if rows and all(row.get("ok") for row in rows):
        lines.append("_이번 QA10 실행에서 실패 없음._")
    lines.append("")
    return lines


def build_markdown(
    search_data: dict | None,
    e2e_data: dict | None,
    qa10_data: dict | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# 품질 트리아지 요약 (자동 생성)")
    lines.append("")
    lines.append(f"생성 시각(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## 1. RAG 검색 배치 (search_quality_report)")
    lines.append("")

    by_dim: dict[str, list[str]] = defaultdict(list)

    if not search_data:
        lines.append("_입력 파일 없음_: `artifacts/search_quality_report.json` — `python sync/test_search_quality.py` 실행 후 재실행.")
        lines.append("")
    else:
        rate = search_data.get("success_rate")
        sc = search_data.get("success_count")
        tot = search_data.get("total_tests")
        lines.append(f"- 성공률: **{rate}%** ({sc}/{tot})")
        lines.append(f"- API: `{search_data.get('api_base_url', '')}`")
        lines.append("")
        details = search_data.get("details") or []
        for row in details:
            ev = row.get("evaluation") or {}
            if ev.get("success"):
                continue
            tc = row.get("test_case") or {}
            q = tc.get("question", "")
            reason = ev.get("reason") or str(ev.get("details", {}))
            dim = _dim_for_search_reason(reason)
            by_dim[dim].append(f"- [ ] {q[:80]} — {reason[:120]}")
        if by_dim:
            lines.append("### 차원별 실패 후보 (체크리스트)")
            lines.append("")
            for dim in sorted(by_dim.keys()):
                lines.append(f"#### {dim}")
                lines.extend(by_dim[dim])
                lines.append("")
        else:
            lines.append("_이번 리포트에서 실패 케이스 없음._")
            lines.append("")

    lines.extend(_qa10_section(qa10_data))

    lines.append("## 2. Agent E2E (pytest JSON, 선택)")
    lines.append("")
    if not e2e_data:
        lines.append(
            "_입력 없음._ 스테이징에서 `RUN_AGENT_E2E=1 pytest tests/test_agent_e2e.py --run-integration "
            "--json-report --json-report-file=artifacts/pytest_e2e.json` 등으로 수집 가능."
        )
    else:
        lines.append("```")
        lines.append(json.dumps(e2e_data, ensure_ascii=False, indent=2)[:4000])
        lines.append("```")
    lines.append("")

    lines.append("## 3. parse / UI 경고 — 서버 로그 grep (수동)")
    lines.append("")
    lines.append("| 품질 차원 | grep 힌트 |")
    lines.append("|-----------|-----------|")
    lines.append("| 가독성·목록 | `query_type=list이지만 details` |")
    lines.append("| 가독성·로그 | `log 타입인데 logs 없음` |")
    lines.append("| JSON 파싱 | `\\[Agent\\] JSON 파싱 실패` |")
    lines.append("")
    lines.append("## 4. 다음 액션 (백로그)")
    lines.append("")
    lines.append("1. 위 **RAG 차원** 실패가 많으면: 임베딩 요약·동기화·`vector_search` 부스팅부터.")
    lines.append("2. E2E **tool_trace** 불일치가 많으면: `app/prompts/agent_system.txt`·도구 설명 보강.")
    lines.append("3. parse 경고가 많으면: `parse_agent_answer` 폴백·프롬프트 JSON 스키마 정합.")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="품질 트리아지 요약 생성")
    ap.add_argument("--search-report", type=Path, default=_DEFAULT_SEARCH)
    ap.add_argument("--qa10-report", type=Path, default=_DEFAULT_QA10, help="qa_10_eval JSON (없으면 생략)")
    ap.add_argument("--e2e-report", type=Path, default=_DEFAULT_E2E)
    ap.add_argument("-o", "--output", type=Path, default=_OUT)
    args = ap.parse_args()

    search_data = _load_json(args.search_report)
    qa10_data = _load_json(args.qa10_report)
    e2e_data = _load_json(args.e2e_report)

    md = build_markdown(search_data, e2e_data, qa10_data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
