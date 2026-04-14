"""scripts/check_env_gates.py 헬퍼 단위 검증 (네트워크 없음)."""

import importlib.util
from pathlib import Path


def _load_gate_module():
    root = Path(__file__).resolve().parent.parent
    path = root / "scripts" / "check_env_gates.py"
    spec = importlib.util.spec_from_file_location("check_env_gates", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_status_ok_healthy():
    m = _load_gate_module()
    ok, msg = m._status_ok({"status": "healthy"}, "rag_engine", frozenset({"healthy"}))
    assert ok and "healthy" in msg


def test_status_ok_vector_optimized():
    m = _load_gate_module()
    ok, _ = m._status_ok({"status": "optimized"}, "vector_db", frozenset({"healthy", "optimized"}))
    assert ok


def test_status_fail_wrong_status():
    m = _load_gate_module()
    ok, msg = m._status_ok({"status": "error"}, "agent_mesh", frozenset({"active"}))
    assert not ok and "error" in msg
