"""가벼운 import·앱 생성 스모크 테스트 (DB/외부 API 없이)."""

import os

import pytest

# config 로드 시 필수는 아니나, 일부 환경에서 경고 방지
os.environ.setdefault("RAG_DB_PASSWORD", "test")
os.environ.setdefault("ZABBIX_DB_PASSWORD", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")


def test_import_config():
    from app import config

    assert config.APP_ROOT
    assert "host" in config.RAG_DB


def test_fastapi_app_loads():
    from app.main import app

    assert app.title == "Zabbix RAG-LLM API"


def test_agent_prompt_file_exists():
    from pathlib import Path
    from app.services import agent

    assert "Zabbix" in agent.AGENT_SYSTEM_PROMPT[:200]
