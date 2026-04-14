"""agent_e2e_utils SSE 파서 단위 테스트."""

from tests.agent_e2e_utils import (
    parse_chat_sse_response,
    tool_names_from_result_event,
)


def test_parse_chat_sse_extracts_result_and_done():
    sample = """data: {"type": "meta", "query_type": "agent"}\n\ndata: {"type": "progress", "step": 1}\n\ndata: {"type": "result", "data": {"answer": "ok"}, "sources": {"tool_trace": [{"tool": "get_alerts", "count": 1}]}}\n\ndata: {"type": "done"}\n\n"""
    p = parse_chat_sse_response(sample)
    assert p["done"] is True
    assert p["result_event"] is not None
    assert p["error_event"] is None
    names = tool_names_from_result_event(p["result_event"])
    assert names == ["get_alerts"]
