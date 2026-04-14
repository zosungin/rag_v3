"""
Agent E2E — 실 LLM·Zabbix·RAG 환경 필요.

  RUN_AGENT_E2E=1 TEST_API_BASE_URL=http://127.0.0.1:8000 \\
    pytest tests/test_agent_e2e.py --run-integration -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import requests

from tests.agent_e2e_utils import parse_chat_sse_response, tool_names_from_result_event

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "agent_golden.json"


def _base_url() -> str:
    return os.environ.get("TEST_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _golden_cases():
    if not FIXTURE.is_file():
        return []
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [c for c in data if isinstance(c, dict) and c.get("query")]


@pytest.mark.skipif(os.environ.get("RUN_AGENT_E2E") != "1", reason="RUN_AGENT_E2E=1 일 때만 실행")
@pytest.mark.parametrize("case", _golden_cases(), ids=lambda c: c.get("id", "case"))
def test_golden_query_uses_allowed_tool(case: dict):
    url = f"{_base_url()}/api/chat"
    body = {"query": case["query"], "top_k": 5}
    model_id = os.environ.get("AGENT_E2E_MODEL_ID")
    if model_id:
        body["model_id"] = model_id

    timeout = float(os.environ.get("AGENT_E2E_TIMEOUT_SEC", "180"))
    r = requests.post(url, json=body, stream=True, timeout=timeout)
    r.raise_for_status()
    text = r.text
    parsed = parse_chat_sse_response(text)
    err = parsed.get("error_event")
    assert err is None, f"SSE error: {err}"

    ev = parsed.get("result_event")
    assert ev is not None, "type=result 이벤트 없음"

    names = tool_names_from_result_event(ev)
    allowed = set(case.get("allowed_tools") or [])
    required = set(case.get("required_tools") or [])
    min_distinct = int(case.get("min_distinct_tools", 1))
    used = set(names)
    assert len(used) >= min_distinct, f"tool_trace 부족: {names}"
    assert used & allowed, f"허용 도구 {allowed} 외 사용 또는 미일치: {names}"
    if required:
        assert required.issubset(used), f"필수 도구 누락: required={required}, used={used}"
