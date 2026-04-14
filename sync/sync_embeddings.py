"""
전체 임베딩 동기화 작업을 순서대로 실행 (스케줄러용).
실행: 프로젝트 루트에서 PYTHONPATH=. python -m sync.scheduler
     또는 sync 디렉터리에서 상위를 PYTHONPATH에 포함.
"""
from __future__ import annotations

import importlib
import logging
import traceback
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)

# (모듈 경로, 진입점 함수명)
_EMBED_JOBS: List[Tuple[str, str]] = [
    ("sync.gen_embeddings_server_devices", "main"),
    ("sync.gen_embeddings_network_devices", "main"),
    ("sync.gen_embeddings_alert_events", "main"),
    ("sync.gen_embeddings_network_interfaces", "main"),
    ("sync.gen_embeddings_server_connect", "main"),
    ("sync.gen_embeddings_server_cpu", "main"),
    ("sync.gen_embeddings_server_errpt", "main"),
    ("sync.gen_embeddings_server_filesystems", "main"),
    ("sync.gen_embeddings_server_interfaces", "main"),
    ("sync.gen_embeddings_server_listen", "main"),
    ("sync.gen_embeddings_server_logs", "main"),
    ("sync.gen_embeddings_server_memory", "main"),
    ("sync.gen_embeddings_server_processes", "main"),
    ("sync.gen_embeddings_server_swap", "main"),
]


def _run_job(mod_path: str, fn_name: str) -> None:
    mod = importlib.import_module(mod_path)
    fn: Callable = getattr(mod, fn_name)
    fn()


def sync_embeddings() -> None:
    """모든 gen_embeddings_* 스크립트를 정의된 순서로 실행. 실패 시 예외 전파."""
    errors: List[str] = []
    for mod_path, fn_name in _EMBED_JOBS:
        try:
            logger.info("[sync_embeddings] 시작: %s.%s", mod_path, fn_name)
            _run_job(mod_path, fn_name)
            logger.info("[sync_embeddings] 완료: %s", mod_path)
        except Exception as e:
            msg = f"{mod_path}.{fn_name}: {e}"
            errors.append(msg)
            logger.error("[sync_embeddings] 실패: %s", msg)
            traceback.print_exc()
    if errors:
        raise RuntimeError("sync_embeddings 일부 실패: " + " | ".join(errors))
