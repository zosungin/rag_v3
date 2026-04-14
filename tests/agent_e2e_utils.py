"""POST /api/chat SSE 스트림에서 최종 result·tool_trace 추출."""

from __future__ import annotations

import json
from typing import Any


def iter_sse_data_lines(raw_text: str):
    """디버깅·동기 파서용: 전체 응답 문자열을 줄 단위로 파싱."""
    for line in raw_text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            yield line[6:]


def parse_chat_sse_response(text: str) -> dict[str, Any]:
    """
    SSE 본문에서 type=result 이벤트 마지막 항목과 type=done 여부 반환.
    """
    last_result: dict[str, Any] | None = None
    seen_done = False
    last_error: dict[str, Any] | None = None
    for payload in iter_sse_data_lines(text):
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "result":
            last_result = obj
        elif t == "done":
            seen_done = True
        elif t == "error":
            last_error = obj
    return {
        "result_event": last_result,
        "done": seen_done,
        "error_event": last_error,
    }


def tool_names_from_result_event(result_event: dict | None) -> list[str]:
    if not result_event:
        return []
    sources = result_event.get("sources") or {}
    trace = sources.get("tool_trace") or []
    out = []
    for step in trace:
        name = step.get("tool")
        if name:
            out.append(name)
    return out
