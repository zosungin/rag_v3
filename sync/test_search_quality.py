#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 벡터 검색 품질 배치 테스트 — POST /api/search (현행 응답 스키마).

환경 변수:
  TEST_API_BASE_URL — 기본 http://127.0.0.1:8000
  TEST_SEARCH_DELAY_SEC — 케이스 간 대기 초 (기본 3)
  TEST_SEARCH_QUALITY_REPORT — JSON 리포트 경로 (기본 <repo>/artifacts/search_quality_report.json)

실행:
  python sync/test_search_quality.py
  python sync/test_search_quality.py --delay 0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
API_BASE_URL = os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
_DEFAULT_REPORT = _REPO_ROOT / "artifacts" / "search_quality_report.json"

# 테스트 케이스 — expect 는 현행 API(total_count, results[].category/table/table_name/data.summary) 기준
TEST_CASES = [
    # --- server / 호스트 성격 ---
    {
        "question": "정상 작동 중인 서버는?",
        "categories": ["server"],
        "expect": {
            "min_total": 1,
            "any_category_in": ["server"],
        },
    },
    {
        "question": "건강한 시스템은 어떤 것들이 있나요?",
        "categories": ["server"],
        "expect": {"min_total": 1, "any_category_in": ["server"]},
    },
    {
        "question": "서버 목록을 보여줘",
        "expect": {"min_total": 1, "any_category_in": ["server", "network", "alert", "infra"]},
    },
    {
        "question": "호스트 리스트",
        "expect": {"min_total": 1, "any_category_in": ["server", "network", "alert", "infra"]},
    },
    # --- 메트릭 / 리소스 (server 임베딩 테이블) ---
    {
        "question": "CPU 사용률이 높은 항목은?",
        "categories": ["server"],
        "expect": {
            "min_total": 1,
            "any_category_in": ["server"],
            "table_name_contains_any": ["cpu"],
        },
    },
    {
        "question": "메모리 사용량이 많은 메트릭",
        "categories": ["server"],
        "expect": {
            "min_total": 1,
            "table_name_contains_any": ["memory", "mem"],
        },
    },
    {
        "question": "디스크 공간이 부족한 항목",
        "categories": ["server"],
        "expect": {
            "min_total": 1,
            "table_name_contains_any": ["filesystem", "disk", "fs"],
        },
    },
    {
        "question": "정상 상태의 CPU 메트릭",
        "categories": ["server"],
        "expect": {"min_total": 1, "summary_keyword_any": ["cpu", "CPU", "사용"]},
    },
    {
        "question": "위험 상태의 메트릭",
        "categories": ["server"],
        "expect": {"min_total": 1, "summary_keyword_any": ["critical", "위험", "Critical"]},
    },
    {
        "question": "경고 상태의 항목",
        "categories": ["server"],
        "expect": {"min_total": 1, "summary_keyword_any": ["warning", "경고", "Warning"]},
    },
    # --- network 카테고리 ---
    {
        "question": "네트워크 스위치 인터페이스 트래픽",
        "categories": ["network"],
        "expect": {"min_total": 1, "any_category_in": ["network"]},
    },
    {
        "question": "네트워크 장비 상태",
        "categories": ["network"],
        "expect": {"min_total": 1, "any_category_in": ["network"]},
    },
    # --- alert 카테고리 ---
    {
        "question": "최근 발생한 알림이나 트리거 이벤트",
        "categories": ["alert"],
        "expect": {"min_total": 1, "any_category_in": ["alert"]},
    },
    {
        "question": "심각도 높은 알람 목록",
        "categories": ["alert"],
        "expect": {"min_total": 1, "any_category_in": ["alert"]},
    },
    # --- infra ---
    {
        "question": "인프라 자산 인벤토리 요약",
        "categories": ["infra"],
        "expect": {"min_total": 1, "any_category_in": ["infra"]},
    },
]


def test_search(
    question: str,
    top_k: int = 5,
    categories: list[str] | None = None,
    max_retries: int = 2,
    timeout: float = 60.0,
) -> dict:
    payload: dict = {"query": question, "top_k": top_k}
    if categories:
        payload["categories"] = categories
    url = f"{API_BASE_URL}/api/search"
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code == 200:
                return response.json()
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return {"error": f"HTTP {response.status_code}", "body": response.text[:500]}
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return {"error": "Timeout after retries"}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return {"error": str(e)}
    return {"error": "All retries failed"}


def _result_rows(result: dict) -> list:
    return result.get("results") or []


def _total_count(result: dict) -> int:
    if "total_count" in result:
        return int(result["total_count"])
    return int(result.get("total", 0))


def evaluate_result(test_case: dict, result: dict) -> dict:
    if "error" in result:
        return {"success": False, "reason": f"API 오류: {result['error']}", "details": {}}

    ex = test_case.get("expect") or {}
    details: dict = {}
    total = _total_count(result)
    rows = _result_rows(result)

    min_total = ex.get("min_total", 1)
    if total < min_total:
        return {
            "success": False,
            "reason": f"total_count {total} < min_total {min_total}",
            "details": {"total": total},
        }
    details["total"] = total

    cats = ex.get("any_category_in")
    if cats:
        found_cats = {r.get("category") for r in rows}
        if not found_cats.intersection(set(cats)):
            return {
                "success": False,
                "reason": f"결과 category 가 {cats} 중 어느 것도 없음: {found_cats}",
                "details": {**details, "found_categories": list(found_cats)},
            }
        details["category_ok"] = True

    subs = ex.get("table_name_contains_any")
    if subs:
        lowered = [
            (r.get("table_name") or r.get("table") or "").lower() for r in rows
        ]
        ok = any(any(s.lower() in cell for s in subs) for cell in lowered)
        if not ok:
            return {
                "success": False,
                "reason": f"table_name 에 {subs} 중 일치 없음",
                "details": {**details, "tables_sample": lowered[:5]},
            }
        details["table_match"] = True

    kws = ex.get("summary_keyword_any")
    if kws:
        texts: list[str] = []
        for r in rows:
            data = r.get("data") or {}
            texts.append(str(data.get("summary") or ""))
        joined = " ".join(texts)
        if not any(kw in joined for kw in kws):
            return {
                "success": False,
                "reason": f"summary 에 키워드 {kws} 없음",
                "details": {**details, "summary_sample": joined[:200]},
            }
        details["keyword_ok"] = True

    return {"success": True, "details": details}


def run_tests(delay_sec: float) -> float:
    report_path = Path(
        os.environ.get("TEST_SEARCH_QUALITY_REPORT", str(_DEFAULT_REPORT))
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("RAG 벡터 검색 품질 테스트 (POST /api/search)")
    print("=" * 80)
    print(f"API_BASE_URL={API_BASE_URL}")
    print(f"시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"케이스 수: {len(TEST_CASES)}, 케이스 간 대기: {delay_sec}s")
    print("=" * 80)

    results = []
    success_count = 0

    for i, test_case in enumerate(TEST_CASES, 1):
        q = test_case["question"]
        print(f"[{i}/{len(TEST_CASES)}] {q}")
        top_k = test_case.get("top_k", 5)
        cats = test_case.get("categories")
        raw = test_search(q, top_k=top_k, categories=cats)
        ev = evaluate_result(test_case, raw)
        results.append({"test_case": test_case, "result": raw, "evaluation": ev})
        if ev["success"]:
            success_count += 1
            print("   OK")
        else:
            print(f"   FAIL: {ev.get('reason', ev)}")

        if i < len(TEST_CASES) and delay_sec > 0:
            time.sleep(delay_sec)

    success_rate = (success_count / len(TEST_CASES)) * 100 if TEST_CASES else 0.0
    print("=" * 80)
    print(f"성공 {success_count}/{len(TEST_CASES)} ({success_rate:.1f}%)")

    out = {
        "timestamp": datetime.now().isoformat(),
        "api_base_url": API_BASE_URL,
        "total_tests": len(TEST_CASES),
        "success_count": success_count,
        "success_rate": round(success_rate, 1),
        "details": results,
    }
    report_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"리포트 저장: {report_path}")
    return success_rate


def main() -> None:
    global API_BASE_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None, help="TEST_API_BASE_URL 보다 우선")
    ap.add_argument("--delay", type=float, default=None, help="케이스 간 초 (기본 TEST_SEARCH_DELAY_SEC 또는 3)")
    args = ap.parse_args()
    if args.base_url:
        API_BASE_URL = args.base_url.rstrip("/")
    delay = args.delay
    if delay is None:
        delay = float(os.environ.get("TEST_SEARCH_DELAY_SEC", "3"))
    rate = run_tests(delay_sec=delay)
    sys.exit(0 if rate >= 70.0 else 1)


if __name__ == "__main__":
    main()
