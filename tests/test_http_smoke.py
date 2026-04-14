"""FastAPI 앱 기동 후 라우팅 스모크.

startup 에서 RAG·Zabbix 풀을 열므로, 기본 CI에서는 integration 마크로 제외.
"""

import os

import pytest

os.environ.setdefault("RAG_DB_PASSWORD", "test")
os.environ.setdefault("ZABBIX_DB_PASSWORD", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")


@pytest.mark.integration
def test_root_html_ok():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
