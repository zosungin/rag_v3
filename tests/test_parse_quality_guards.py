"""parse_agent_answer 품질 가드 테스트."""

import json
import os

os.environ.setdefault("RAG_DB_PASSWORD", "test")
os.environ.setdefault("ZABBIX_DB_PASSWORD", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")


def test_alert_disk_query_reclassified_to_list():
    from app.services.agent import parse_agent_answer

    payload = {
        "query_type": "장애 조회",
        "data": {
            "summary": "디스크 여유 공간이 부족한 파일시스템 목록",
            "details": [
                {"파일시스템": "/data", "사용률": "91%"},
                {"파일시스템": "/", "사용률": "88%"},
            ],
            "analysis": "디스크 사용률이 높습니다.",
        },
    }
    out = parse_agent_answer(json.dumps(payload, ensure_ascii=False))
    assert out.get("query_type") == "list"
    assert len(out.get("items") or []) >= 1


def test_network_unit_normalization_gbs_to_gbps():
    from app.services.agent import parse_agent_answer

    payload = {
        "query_type": "목록 조회",
        "data": {
            "summary": "포트 트래픽 요약",
            "details": [{"인터페이스": "1/1/5", "수신": "4.36 GB/s", "송신": "500 MB/s"}],
            "analysis": "트래픽이 높습니다.",
        },
    }
    out = parse_agent_answer(json.dumps(payload, ensure_ascii=False))
    text = json.dumps(out, ensure_ascii=False)
    assert "GB/s" not in text and "MB/s" not in text
    assert ("Gbps" in text) or ("Mbps" in text)

