"""scripts/qa_10_eval.build_operational_markdown 단위 검증."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load_qa10_module():
    path = _ROOT / "scripts" / "qa_10_eval.py"
    spec = importlib.util.spec_from_file_location("qa_10_eval", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_operational_markdown_contains_headers():
    mod = _load_qa10_module()
    summary = {
        "meta": {"generated_at_utc": "2026-04-04T00:00:00+00:00", "base_url": "http://example:8000"},
        "total": 1,
        "pass": 1,
        "fail": 0,
        "results": [
            {
                "id": "q1",
                "query": "test",
                "ok": True,
                "tools": ["get_server_status"],
                "expected_tools": ["get_server_status"],
                "quality_flags": [],
                "answer_preview": "hello",
                "elapsed_ms": 100,
            }
        ],
    }
    md = mod.build_operational_markdown(summary)
    assert "운영 채팅 품질 점검" in md
    assert "http://example:8000" in md
    assert "| q1 |" in md


def test_build_operational_markdown_lists_failure():
    mod = _load_qa10_module()
    summary = {
        "meta": {},
        "total": 1,
        "pass": 0,
        "fail": 1,
        "results": [{"id": "q2", "query": "x", "ok": False, "error": "timeout", "elapsed_ms": 0}],
    }
    md = mod.build_operational_markdown(summary)
    assert "아니오" in md
    assert "실패 원인" in md
