"""parse_agent_answer: data.items / 최상위 items → details 폴백."""

import json
import os

os.environ.setdefault("RAG_DB_PASSWORD", "test")
os.environ.setdefault("ZABBIX_DB_PASSWORD", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")


def test_parse_list_uses_data_items_when_details_empty():
    from app.services.agent import parse_agent_answer

    payload = {
        "query_type": "목록 조회",
        "data": {
            "summary": "요약",
            "details": [],
            "items": [{"호스트": "A", "CPU": "10%"}, {"호스트": "B", "CPU": "20%"}],
        },
    }
    out = parse_agent_answer(json.dumps(payload, ensure_ascii=False))
    assert out.get("query_type") == "list"
    assert len(out.get("items") or []) == 2
    assert out.get("total_count") == 2


def test_parse_list_vmware_tool_trace_fallback():
    from app.services.agent import parse_agent_answer

    payload = {
        "query_type": "목록 조회",
        "data": {"summary": "상위 VM", "details": []},
    }
    trace = [
        {
            "tool": "get_vmware",
            "found": True,
            "count": 2,
            "vmware_list_fallback": [
                {
                    "가상머신명": "Veeam",
                    "CPU 사용률": "21.00%",
                    "IP": "10.0.0.1",
                    "호스트": "ESXi-1",
                    "순위": "1위",
                },
            ],
        },
    ]
    out = parse_agent_answer(json.dumps(payload, ensure_ascii=False), tool_trace=trace)
    assert out.get("query_type") == "list"
    assert len(out.get("items") or []) == 1
    assert (out.get("items") or [{}])[0].get("가상머신명") == "Veeam"


def test_parse_status_interface_rows_becomes_list():
    """status + 인터페이스 표 행 → metrics 빈 그리드 대신 list 테이블."""
    from app.services.agent import parse_agent_answer

    payload = {
        "query_type": "상태 조회",
        "data": {
            "summary": "트래픽",
            "details": [
                {
                    "인터페이스": "en15",
                    "수신": "3.14 Kbps",
                    "송신": "1.0 Kbps",
                },
            ],
        },
    }
    out = parse_agent_answer(json.dumps(payload, ensure_ascii=False))
    assert out.get("query_type") == "list"
    assert len(out.get("items") or []) == 1
    assert out.get("metrics") is None
    assert (out.get("items") or [{}])[0].get("인터페이스") == "en15"
