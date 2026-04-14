"""tool_executor 디스패치·오류 경로 계약 테스트 (Zabbix DB 없음)."""

import asyncio
import os

os.environ.setdefault("RAG_DB_PASSWORD", "test")
os.environ.setdefault("ZABBIX_DB_PASSWORD", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")


def test_execute_tool_unknown_name():
    from app.services.tool_executor import execute_tool

    r = asyncio.run(execute_tool("nonexistent_tool_xyz", {}))
    assert r.get("found") is False
    assert "error" in r
    assert "nonexistent_tool_xyz" in r.get("error", "")
    assert "available_tools" in r


def test_execute_tool_dispatches_registered_probe(monkeypatch):
    from app.services import tool_executor

    async def _probe(**kwargs):
        return {
            "found": True,
            "count": 7,
            "data": [],
            "match_info": "probe_ok",
        }

    monkeypatch.setitem(tool_executor.TOOL_EXECUTORS, "_contract_probe", _probe)
    try:
        r = asyncio.run(tool_executor.execute_tool("_contract_probe", {"host_query": "all"}))
        assert r["count"] == 7
        assert r.get("found") is True
    finally:
        tool_executor.TOOL_EXECUTORS.pop("_contract_probe", None)


def test_execute_tool_typeerror_returns_error_shape(monkeypatch):
    from app.services import tool_executor

    async def _bad(**kwargs):
        raise TypeError("bad args")

    monkeypatch.setitem(tool_executor.TOOL_EXECUTORS, "_contract_bad", _bad)
    try:
        r = asyncio.run(tool_executor.execute_tool("_contract_bad", {}))
        assert r.get("found") is False
        assert "error" in r
    finally:
        tool_executor.TOOL_EXECUTORS.pop("_contract_bad", None)
