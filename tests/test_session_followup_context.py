"""세션 컨텍스트·후속 질의 보강 (목록 → 구체 호스트 연계)."""
import asyncio
import pytest
from unittest.mock import patch, AsyncMock

from app.routers import chat as chat_mod


def test_is_concrete_host_hint_category_vs_asset():
    assert chat_mod.is_concrete_host_hint("원도우 서버") is False
    assert chat_mod.is_concrete_host_hint("윈도우 서버") is False
    assert chat_mod.is_concrete_host_hint("AIX 서버") is False
    assert chat_mod.is_concrete_host_hint("aix 서버") is False
    assert chat_mod.is_concrete_host_hint("AIX") is False
    assert chat_mod.is_concrete_host_hint("WIN_LOCAL") is True
    assert chat_mod.is_concrete_host_hint("172.30.20.76") is True
    assert chat_mod.is_concrete_host_hint("test_foo_bar") is True
    assert chat_mod.is_concrete_host_hint("zabbix") is True


def test_query_blocks_host_injection():
    assert chat_mod.query_blocks_host_injection("원도우 서버 cpu") is False
    assert chat_mod.query_blocks_host_injection("WIN_LOCAL cpu") is False
    assert chat_mod.query_mentions_concrete_host_token("WIN_LOCAL cpu") is True
    assert chat_mod.query_blocks_host_injection("전체 서버") is True


def test_extract_list_host_names_from_items():
    items = [{"ASSET_NAME": "H1", "ip": "1.1.1.1"}, {"host": "H2"}]
    assert chat_mod.extract_list_host_names_from_items(items) == ["H1", "H2"]


def test_build_session_context_list_only():
    ctx = {
        "resolved_period": None,
        "resolved_host": None,
        "list_context_hosts": ["A", "B"],
        "last_topic": None,
        "vmware_context_names": None,
    }
    block = chat_mod.build_session_context_block(ctx)
    assert "직전에 조회된 서버/자산 후보" in block
    assert "A" in block and "B" in block
    assert "한 대로 단정하지 말고" in block


def test_extract_session_context_list_single_prefers_asset():
    messages = [
        {
            "query": "원도우 서버 뭐가 있지",
            "result_data": {
                "query_type": "list",
                "items": [{"host": "WIN_LOCAL", "ip": "172.30.20.76"}],
            },
            "model_info": {},
        }
    ]

    async def _run():
        with patch.object(
            chat_mod, "db_get_recent_messages", new_callable=AsyncMock, return_value=messages
        ):
            return await chat_mod.extract_session_context("sid", "CPU 알려줘")

    ctx = asyncio.run(_run())
    assert ctx["resolved_host"] == "WIN_LOCAL"
    assert ctx.get("list_context_hosts") is None


def test_extract_session_context_list_multi():
    messages = [
        {
            "query": "서버 목록",
            "result_data": {
                "query_type": "list",
                "items": [{"host": "S1"}, {"host": "S2"}],
            },
            "model_info": {},
        }
    ]

    async def _run():
        with patch.object(
            chat_mod, "db_get_recent_messages", new_callable=AsyncMock, return_value=messages
        ):
            return await chat_mod.extract_session_context("sid", "상태 알려줘")

    ctx = asyncio.run(_run())
    assert ctx["resolved_host"] is None
    assert ctx["list_context_hosts"] == ["S1", "S2"]


def test_inject_context_after_single_row_list():
    messages = [
        {
            "query": "원도우 서버",
            "result_data": {
                "query_type": "list",
                "items": [{"host": "WIN_LOCAL"}],
            },
            "model_info": {},
        }
    ]

    async def _run():
        with patch.object(
            chat_mod, "db_get_recent_messages", new_callable=AsyncMock, return_value=messages
        ):
            return await chat_mod.inject_context_from_history("sid", "cpu 사용률 알려줘")

    out = asyncio.run(_run())
    assert out == "WIN_LOCAL의 cpu 사용률 알려줘"


def test_inject_context_skips_multi_row_list():
    messages = [
        {
            "query": "목록",
            "result_data": {
                "query_type": "list",
                "items": [{"host": "A"}, {"host": "B"}],
            },
            "model_info": {},
        }
    ]

    async def _run():
        with patch.object(
            chat_mod, "db_get_recent_messages", new_callable=AsyncMock, return_value=messages
        ):
            return await chat_mod.inject_context_from_history("sid", "cpu 알려줘")

    out = asyncio.run(_run())
    assert out == "cpu 알려줘"


def test_detect_ambiguity_category_server_not_clear_target():
    d = chat_mod.detect_ambiguity("원도우 서버 cpu 사용률")
    assert d["needs_target"] is True
